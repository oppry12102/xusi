"""agent 生命周期操作：创建/启停/暂停/续跑/重启/改参/删除/观察/投信/token。

manager 与 agent 之间只有三条通道（本模块是唯一实现处）：
1. 进程与信号 —— systemd 瞬态单元（Restart=always 掉线保护）；
2. 只读 HTTP GET —— http://127.0.0.1:<port>/v1/*（探活与观察，绝不写）；
3. 文件 —— 渲染 config.toml、追加 mailbox.jsonl、读 webui_tokens.json、tail journald。

参数唯一事实源是注册表（etc/agents.json）；config.toml 永远单向渲染。
"""
from __future__ import annotations

import json
import re
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from . import brains, ports, registry, services, systemdctl, versions
from .config import get_config


class AgentError(RuntimeError):
    """业务错误（带用户可读信息，API 层转 4xx/5xx）。"""


# ── 审计 ─────────────────────────────────────────────────────────────

def audit(action: str, **detail: Any) -> None:
    cfg = get_config()
    line = json.dumps(
        {"at": _iso(), "action": action, **detail}, ensure_ascii=False)
    with cfg.audit_file.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def _iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── 基础工具 ─────────────────────────────────────────────────────────

def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9-]+", "-", name.strip().lower()).strip("-")[:24]
    # 残根守卫：中文名常只剩零星字母（如「A股…」→ "a"），短于 2 位不成词，
    # 一律走 agent 兜底，保证 id 前缀风格统一（agent-xxxx）。
    return s if len(s) >= 2 else "agent"


def gen_id(name: str) -> str:
    return f"{slugify(name)}-{uuid.uuid4().hex[:4]}"


def _home(agent: dict) -> Path:
    return get_config().instance_home(agent["id"])


def _unit(agent: dict) -> str:
    return get_config().unit_name(agent["id"])


def _listen_host(agent: dict) -> str:
    return "0.0.0.0" if agent.get("expose") else "127.0.0.1"


def _source_for(agent: dict | None = None) -> Path:
    """该 agent 的 xuseek-v2 源码目录。

    注册表带 source_version → 实例私有副本 instances/<id>/xuseek-v2/（创建时从
    版本仓库解压，实例间完全隔离，可各跑各的版本）；不带（含全部现存 agent）→
    共享主源码 source_dir，行为与从前一字不差。
    """
    ver = str((agent or {}).get("source_version") or "").strip()
    if not ver:
        return ensure_source()
    p = _home(agent) / versions.SRC_DIR_NAME
    if not (p / "xuseek.sh").exists():
        raise AgentError(
            f"agent {agent['id']} 的私有源码副本缺失：{p}（版本 {ver}）。"
            f"实例目录可能被改动——可从版本仓库重新解压到该路径，或停机重建")
    return p


def _xuseek_sh(agent: dict | None = None) -> Path:
    src = _source_for(agent)
    p = src / "xuseek.sh"
    if not p.exists():
        raise AgentError(f"xuseek 源码目录无效：{src}")
    return p


def ensure_source() -> Path:
    """确保 xuseek-v2 源码就位：自管目录缺失时自动从 GitHub 拉取。

    https 匿名拉不动（私有/受限仓库）时回退 ssh（运维者已配 GitHub 密钥的场景）。
    只在部署后第一次发生（源码随目录常驻，.gitignore 不入库）。
    """
    cfg = get_config()
    if (cfg.source_dir / "xuseek.sh").exists():
        return cfg.source_dir
    import subprocess
    cfg.source_dir.parent.mkdir(parents=True, exist_ok=True)
    ssh_url = cfg.source_repo.replace("https://github.com/", "git@github.com:").rstrip("/")
    if not ssh_url.endswith(".git"):
        ssh_url += ".git"
    for url in (cfg.source_repo, ssh_url):
        try:
            r = subprocess.run(["git", "clone", url, str(cfg.source_dir)],
                               capture_output=True, text=True, timeout=600)
            if r.returncode == 0 and (cfg.source_dir / "xuseek.sh").exists():
                return cfg.source_dir
        except Exception:
            continue
    raise AgentError(f"xuseek-v2 源码缺失且从 {cfg.source_repo} 拉取失败"
                     f"（https 需认证时可手动：git clone {ssh_url} {cfg.source_dir}）")


def _spawn_unit(agent: dict) -> None:
    """统一拉起入口：定位该 agent 的源码（私有副本或共享主源码）→ systemd-run
    瞬态单元（Restart=always）。"""
    cfg = get_config()
    src = _source_for(agent)
    systemdctl.spawn_agent(cfg.unit_name(agent["id"]), str(src),
                           str(_home(agent)), _listen_host(agent), agent["port"])


def _run_cli(args: list[str], timeout: float = 120, *, agent: dict | None = None) -> str:
    """调 xuseek 公开 CLI（init / token）——公开接口，非内部耦合。
    带版本创建的 agent 用它自己的源码副本跑 CLI。"""
    import subprocess
    r = subprocess.run([str(_xuseek_sh(agent)), *args], capture_output=True, text=True,
                       timeout=timeout)
    if r.returncode != 0:
        err = (r.stderr or r.stdout or "").strip().splitlines()
        raise AgentError(err[-1] if err else f"xuseek CLI 失败：{' '.join(args[:2])}")
    return r.stdout


# ── token 文件读取（通道 3）─────────────────────────────────────────

def read_agent_tokens(agent: dict) -> dict[str, dict]:
    """agent 的 data/webui_tokens.json：{token: {label, created_at}}（实时）。"""
    p = _home(agent) / "data" / "webui_tokens.json"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def internal_token(agent: dict) -> str | None:
    """取一个可用的观察台 token（代理注入与内部观察用）：注册表记录优先。"""
    rec = [t["token"] for t in agent.get("tokens", [])]
    for tok in rec:
        if tok in read_agent_tokens(agent):
            return tok
    live = read_agent_tokens(agent)
    return next(iter(live), None)


# ── HTTP 观察（通道 2，只读 GET）────────────────────────────────────

def _get(agent: dict, path: str, timeout: float = 4.0) -> httpx.Response:
    url = f"http://127.0.0.1:{agent['port']}{path}"
    headers = {}
    tok = internal_token(agent)
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    return httpx.get(url, headers=headers, timeout=timeout)


def wait_health(port: int, agent_id: str, timeout: float = 90.0) -> None:
    """启动验收：轮询 /v1/health 直到 200。失败抛 AgentError（附日志尾部）。"""
    import time
    deadline = time.monotonic() + timeout
    last_err = ""
    while time.monotonic() < deadline:
        try:
            r = httpx.get(f"http://127.0.0.1:{port}/v1/health", timeout=2.0)
            if r.status_code == 200:
                return
            last_err = f"HTTP {r.status_code}"
        except Exception as e:
            last_err = type(e).__name__
        time.sleep(0.6)
    log = systemdctl.journal_tail(get_config().unit_name(agent_id), 20)
    raise AgentError(f"agent 启动后 {timeout:.0f}s 未通过健康验收（{last_err}）。日志尾部：\n{log}")


# ── 生命周期 ─────────────────────────────────────────────────────────

# source_version 保留值：显式选共享主源码（过渡期保留，逐步废弃——勿用作版本号）
MAIN_SOURCE = "main"


def _resolve_source_choice(src_ver: str) -> str:
    """创建时的源码抉择，返回实际使用的版本号（"" = 共享主源码）。

    缺省（未选版本）→ **版本仓库最新包**：每个 agent 自带 xuseek-v2 私有副本，
    instances/<id>/ 自洽、可单独迁移（共享主源码逐步废弃中）；
    显式版本 → 直接用它（提前校验，失败零副作用）；
    显式 "main"，或仓库为空时的缺省回落 → 共享主源码：本地已在 → 用
    （零网络）；不在 → 试 GitHub 拉取；都不可得 → 报错并给出指引。
    """
    if src_ver and src_ver != MAIN_SOURCE:
        versions.zip_for(src_ver)
        return src_ver
    want_main = src_ver == MAIN_SOURCE
    if not want_main:
        avail = versions.list_versions()
        if avail:
            return avail[0]["version"]   # list_versions 已按版本号新→旧排序
    try:
        ensure_source()
        return ""
    except AgentError as e:
        if want_main:
            raise
        raise AgentError(
            f"版本仓库（{get_config().versions_dir}）为空，共享主源码也不可得：{e}。"
            f"请管理员投放 xuseek-v2-<版本号>.zip（见 docs/versions.md），"
            f"或手动 git clone 到 {get_config().source_dir}") from None


def create_agent(name: str, mission: str, brain_list: list[str], *,
                 expose: bool = False, port: int | None = None,
                 budgets: dict | None = None, note: str = "",
                 source_version: str = "") -> dict:
    """创建并启动一个 agent：init（播种经验库）→ 渲染 config → systemd 拉起 → 健康验收 → 签发首个 token。

    source_version：版本号 → 该版本源码解压成实例私有副本（instances/<id>/xuseek-v2/，
    删除时随 home 进 .trash）；"main" → 共享主源码（过渡期保留，逐步废弃）；
    缺省 → 版本仓库最新包（每 agent 自带私有副本，实例自洽可单独迁移；
    仓库为空时回落共享主源码，见 _resolve_source_choice）。
    """
    cfg = get_config()
    mission = (mission or "").strip()
    if not mission:
        raise AgentError("mission 不能为空")
    pool = {b["name"]: b for b in brains.pool_summary()}
    unknown = [b for b in brain_list if b not in pool]
    if unknown:
        raise AgentError(f"密钥池中没有这些大脑：{', '.join(unknown)}")
    no_key = [b for b in brain_list if not pool[b]["has_key"]]
    if no_key:
        raise AgentError(f"这些大脑没配 api_key（etc/brains.toml）：{', '.join(no_key)}")
    if not brain_list:
        raise AgentError("至少选择一家大脑")
    src_ver = _resolve_source_choice((source_version or "").strip())

    agent_id = gen_id(name)
    port = ports.allocate(port)
    home = cfg.instance_home(agent_id)
    unit = cfg.unit_name(agent_id)

    rec = {
        "id": agent_id,
        "name": name.strip() or agent_id,
        "mission": mission,
        "brains": list(brain_list),
        "budgets": budgets or {},
        "expose": bool(expose),
        "port": port,
        "desired_state": "running",
        "note": note,
        "source_version": src_ver,
        "created_at": registry.now_iso(),
        "updated_at": registry.now_iso(),
        "tokens": [],
    }
    try:
        # 0) 选了版本：版本仓库 → 实例私有源码副本（各实例隔离，互不影响）
        if src_ver:
            versions.extract(src_ver, home / versions.SRC_DIR_NAME)
        # 1) init：建 home/data、workspace，播种 playbook 经验库（v2 公开 CLI；
        #    版本化实例用它自己的源码副本跑）
        _run_cli(["--home", str(home), "init", "--mission", mission, "--force"],
                 timeout=300, agent=rec)
        # 1b) 播种对外接口 playbook（workspace/EXTERNAL-API.md：管理面反代约定，
        #     agent 据此自建对外服务可获得正式外部入口；纯被动文档，已存在不动）
        services.seed_playbook(home / "workspace")
        # 2) 渲染 config.toml（含所选大脑与 key，600）
        brains.write_agent_config(home, mission, brain_list, rec["budgets"])
        # 3) 注册（期望态 running）
        registry.add_agent(rec)
        # 4) systemd 拉起（Restart=always 掉线保护）
        _spawn_unit(rec)
        # 5) 健康验收
        wait_health(port, agent_id)
        # 6) 签发首个观察台 token（代理注入 + 用户取用）
        tok, label = _mint_token(agent_id, "xusi-proxy")
    except Exception as e:
        _rollback_create(unit, home, agent_id)
        raise AgentError(f"创建失败已回滚：{e}") from e

    audit("agent.create", agent=agent_id, name=rec["name"], port=port,
          expose=expose, brains=brain_list, source=src_ver or "main",
          source_defaulted=not (source_version or "").strip())
    return get_agent_or_404(agent_id)


def _rollback_create(unit: str, home: Path, agent_id: str) -> None:
    try:
        systemdctl.stop(unit)
    except Exception:
        pass
    try:
        systemdctl.reset_failed(unit)
    except Exception:
        pass
    if home.exists():
        dest = get_config().trash_dir / f"{agent_id}-{uuid.uuid4().hex[:6]}"
        shutil.move(str(home), str(dest))
    registry.remove_agent(agent_id)


def _mint_token(agent_id: str, label: str) -> tuple[str, str]:
    agent = get_agent_or_404(agent_id)
    out = _run_cli(["--home", str(_home(agent)), "token", "new", label], agent=agent)
    tok = out.strip().splitlines()[0].strip()
    registry.record_token(agent_id, tok, label)
    return tok, label


def get_agent_or_404(agent_id: str) -> dict:
    a = registry.get_agent(agent_id)
    if not a:
        raise AgentError(f"agent 不存在: {agent_id}")
    return a


def start(agent_id: str) -> dict:
    agent = get_agent_or_404(agent_id)
    unit = _unit(agent)
    state = systemdctl.unit_state(unit)
    if state != "active":
        _spawn_unit(agent)
        wait_health(agent["port"], agent_id)
    registry.update_agent(agent_id, {"desired_state": "running"})
    audit("agent.start", agent=agent_id)
    return {"id": agent_id, "desired_state": "running"}


def stop(agent_id: str) -> dict:
    """优雅停（SIGTERM → xuseek 轮边界落盘；TimeoutStopSec 兜底）。
    暂停态先解冻：SIGSTOP 中的进程收不到 SIGTERM，直接停会拖到 SIGKILL、丢会话。"""
    agent = get_agent_or_404(agent_id)
    if agent.get("desired_state") == "paused":
        try:
            systemdctl.kill_signal(_unit(agent), "SIGCONT")
        except systemdctl.SystemdError:
            pass  # 单元已不在也无妨，交给下面的幂等停止
    try:
        systemdctl.stop(_unit(agent))
    except systemdctl.SystemdError as e:
        # 单元已消失（曾经 stop 过）视为成功
        if systemdctl.unit_state(_unit(agent)) != "not-found":
            raise AgentError(str(e))
    registry.update_agent(agent_id, {"desired_state": "stopped"})
    audit("agent.stop", agent=agent_id)
    return {"id": agent_id, "desired_state": "stopped"}


def pause(agent_id: str) -> dict:
    """冻结（SIGSTOP）：进程驻留、观察台无响应、呼吸暂停。"""
    agent = get_agent_or_404(agent_id)
    unit = _unit(agent)
    if systemdctl.unit_state(unit) != "active":
        raise AgentError("agent 未在运行，无法暂停（先 start）")
    systemdctl.kill_signal(unit, "SIGSTOP")
    registry.update_agent(agent_id, {"desired_state": "paused"})
    audit("agent.pause", agent=agent_id)
    return {"id": agent_id, "desired_state": "paused"}


def resume(agent_id: str) -> dict:
    agent = get_agent_or_404(agent_id)
    unit = _unit(agent)
    if systemdctl.unit_state(unit) != "active":
        raise AgentError("agent 未在运行（先 start）")
    systemdctl.kill_signal(unit, "SIGCONT")
    registry.update_agent(agent_id, {"desired_state": "running"})
    audit("agent.resume", agent=agent_id)
    return {"id": agent_id, "desired_state": "running"}


def restart(agent_id: str) -> dict:
    """优雅重启：SIGTERM 落盘 → 重新拉起 → 健康验收。"""
    agent = get_agent_or_404(agent_id)
    unit = _unit(agent)
    state = systemdctl.unit_state(unit)
    if state == "active":
        systemdctl.restart(unit)
    else:
        _spawn_unit(agent)
    wait_health(agent["port"], agent_id)
    registry.update_agent(agent_id, {"desired_state": "running"})
    audit("agent.restart", agent=agent_id)
    return {"id": agent_id, "desired_state": "running"}


def delete(agent_id: str) -> dict:
    """删除：**必须先显式停止**（运行/暂停态一律拒绝——多步操作防误删）；
    除名后实例目录移入 .trash（遗留清理归管理员）。"""
    agent = get_agent_or_404(agent_id)
    unit = _unit(agent)
    state = systemdctl.unit_state(unit)
    if state in ("active", "activating"):
        raise AgentError("agent 正在运行（暂停也算运行），不允许删除。请先点「停止」，再删除——两步操作防误删")
    stop(agent_id)
    try:
        systemdctl.reset_failed(unit)
    except Exception:
        pass
    home = _home(agent)
    dest = None
    if home.exists():
        dest = get_config().trash_dir / f"{agent_id}-{uuid.uuid4().hex[:6]}"
        shutil.move(str(home), str(dest))
    registry.remove_agent(agent_id)
    audit("agent.delete", agent=agent_id, port=agent["port"], trash=str(dest))
    return {"id": agent_id, "deleted": True, "moved_to": str(dest)}


# ── 改参 ─────────────────────────────────────────────────────────────

_PATCHABLE = {"name", "mission", "brains", "budgets", "expose", "port", "note"}


def patch_agent(agent_id: str, changes: dict, *, apply_restart: bool = False) -> dict:
    """改参。mission/brains/budgets 热重载（下一口呼吸生效）；
    port/expose 需要进程重启，返回 restart_required，?apply=restart 立即执行。"""
    agent = get_agent_or_404(agent_id)
    bad = set(changes) - _PATCHABLE
    if bad:
        raise AgentError(f"不可修改的字段：{', '.join(sorted(bad))}（可改：{', '.join(sorted(_PATCHABLE))}）")

    hot = {}       # 写注册表即生效（渲染 config）
    need_restart = False

    if "brains" in changes:
        pool = {b["name"]: b for b in brains.pool_summary()}
        bl = list(changes["brains"])
        if not bl:
            raise AgentError("至少选择一家大脑")
        unknown = [b for b in bl if b not in pool]
        if unknown:
            raise AgentError(f"密钥池中没有这些大脑：{', '.join(unknown)}")
        no_key = [b for b in bl if not pool[b]["has_key"]]
        if no_key:
            raise AgentError(f"这些大脑没配 api_key：{', '.join(no_key)}")
        hot["brains"] = bl
    if "mission" in changes:
        m = str(changes["mission"]).strip()
        if not m:
            raise AgentError("mission 不能为空")
        hot["mission"] = m
    if "budgets" in changes:
        hot["budgets"] = dict(changes["budgets"])
    if "name" in changes:
        hot["name"] = str(changes["name"]).strip() or agent["name"]
    if "note" in changes:
        hot["note"] = str(changes["note"])

    next_rec = {**agent, **hot}
    if "port" in changes and int(changes["port"]) != int(agent["port"]):
        ports.allocate(int(changes["port"]))   # 检验可用（含 not-in-use）
        next_rec["port"] = int(changes["port"])
        need_restart = True
    if "expose" in changes and bool(changes["expose"]) != bool(agent.get("expose")):
        next_rec["expose"] = bool(changes["expose"])
        need_restart = True

    if hot:
        brains.write_agent_config(_home(next_rec), next_rec["mission"],
                                  next_rec["brains"], next_rec.get("budgets"))
    updates = dict(hot)
    if next_rec.get("port") != agent.get("port"):
        updates["port"] = next_rec["port"]
    if next_rec.get("expose") != agent.get("expose"):
        updates["expose"] = next_rec["expose"]
    if updates:
        registry.update_agent(agent_id, updates)

    restarted = False
    if need_restart and apply_restart:
        _respawn(next_rec)
        restarted = True
    audit("agent.patch", agent=agent_id, fields=sorted(changes), restarted=restarted)
    out = get_agent_or_404(agent_id)
    return {**out, "restart_required": need_restart, "restarted": restarted}


def _respawn(agent: dict) -> None:
    """换监听参数的重启：stop 旧瞬态单元 → 以新 host/port 重新拉起。"""
    unit = _unit(agent)
    if systemdctl.unit_state(unit) == "active":
        systemdctl.stop(unit)
    _spawn_unit(agent)
    wait_health(agent["port"], agent["id"])


# ── 观察（只读）─────────────────────────────────────────────────────

def status(agent_id: str) -> dict:
    """状态聚合：systemd 单元 + /v1/health + /v1/status。全程只读 GET。"""
    agent = get_agent_or_404(agent_id)
    unit = _unit(agent)
    out: dict[str, Any] = {
        "id": agent["id"],
        "name": agent["name"],
        "mission": agent["mission"],
        "brains": agent["brains"],
        "port": agent["port"],
        "expose": agent.get("expose", False),
        "note": agent.get("note", ""),
        "source_version": agent.get("source_version", ""),
        "desired_state": agent.get("desired_state", "running"),
        "listen_host": _listen_host(agent),
        "created_at": agent.get("created_at"),
        "tokens_count": len(read_agent_tokens(agent)),
        "fetched_at": _iso(),
    }
    brief = systemdctl.unit_brief(unit)
    out["process"] = brief
    if brief["active"] != "active":
        out["health"] = {"ok": False, "note": f"单元 {brief['active']}"}
        return out
    try:
        h = httpx.get(f"http://127.0.0.1:{agent['port']}/v1/health", timeout=2.5)
        out["health"] = {"ok": h.status_code == 200}
    except Exception as e:
        out["health"] = {"ok": False, "note": type(e).__name__}
        return out
    try:
        r = _get(agent, "/v1/status", timeout=4.0)
        out["agent_status"] = r.json() if r.status_code == 200 else {"error": f"HTTP {r.status_code}"}
    except Exception as e:
        out["agent_status"] = {"error": type(e).__name__}
    return out


def observe(agent_id: str, what: str, limit: int = 50) -> Any:
    """只读观察转发：status/sessions/events/messages/outbox/whoami。

    返回值已解开 agent 的标准包装（{"events":[…]} 之类）——列表项直接给前端。
    注意 outbox 的键名也是 "messages"（agent 侧如此定义）。
    """
    agent = get_agent_or_404(agent_id)
    if systemdctl.unit_state(_unit(agent)) != "active":
        raise AgentError("agent 未在运行")
    allowed = {"status", "sessions", "events", "messages", "outbox", "whoami"}
    if what not in allowed:
        raise AgentError(f"观察项须为 {sorted(allowed)}")
    limit = max(1, min(int(limit), 500))
    path = f"/v1/{what}" if what in {"status", "whoami"} else f"/v1/{what}?limit={limit}"
    r = _get(agent, path, timeout=6.0)
    if r.status_code == 401:
        raise AgentError("观察台 token 全部失效（重新签发一个）")
    if r.status_code != 200:
        raise AgentError(f"上游 HTTP {r.status_code}")
    data = r.json()
    wrap = {"events": "events", "sessions": "sessions",
            "messages": "messages", "outbox": "messages"}
    if what in wrap and isinstance(data, dict):
        return data.get(wrap[what], [])
    return data


def logs(agent_id: str, n: int = 200) -> dict:
    agent = get_agent_or_404(agent_id)
    n = max(1, min(int(n), 1000))
    text = systemdctl.journal_tail(_unit(agent), n)
    return {"id": agent_id, "lines": text.splitlines()[-n:]}


# ── 投信（通道 3：纯文件写）─────────────────────────────────────────

def mail(agent_id: str, text: str) -> dict:
    """给大脑投信：追加 data/mailbox.jsonl（与 xuseek CLI mail / 观测台表单完全同语义）。
    休眠中 5s 内被轮询唤醒。"""
    agent = get_agent_or_404(agent_id)
    text = (text or "").strip()
    if not text:
        raise AgentError("信件内容不能为空")
    home = _home(agent)
    if not home.exists():
        raise AgentError("实例目录不存在")
    msg = {"id": uuid.uuid4().hex[:12], "sender": "admin", "text": text, "at": _iso()}
    line = json.dumps(msg, ensure_ascii=False) + "\n"
    for name in ("mailbox.jsonl", "mailbox_log.jsonl"):
        p = home / "data" / name
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(line)
    audit("agent.mail", agent=agent_id, chars=len(text))
    return {"posted": True, "id": msg["id"], "at": msg["at"]}


# ── agent 观察台 token ──────────────────────────────────────────────

def tokens_list(agent_id: str) -> list[dict]:
    """实时 token 清单（含完整 token —— 供经管理面认证的用户取用）。"""
    agent = get_agent_or_404(agent_id)
    live = read_agent_tokens(agent)
    labels = {t["token"]: t for t in agent.get("tokens", [])}
    out = []
    for tok, meta in live.items():
        out.append({
            "token": tok,
            "label": meta.get("label") or labels.get(tok, {}).get("label", ""),
            "created_at": meta.get("created_at", ""),
            "recorded_by_xusi": tok in labels,
        })
    return out


def token_new(agent_id: str, label: str = "") -> dict:
    agent = get_agent_or_404(agent_id)
    label = (label or "").strip() or f"tok-{len(agent.get('tokens', [])) + 1}"
    tok, _ = _mint_token(agent_id, label)
    audit("agent.token_new", agent=agent_id, label=label)
    return {"token": tok, "label": label, "created_at": registry.now_iso()}


def token_revoke(agent_id: str, token_prefix: str) -> dict:
    agent = get_agent_or_404(agent_id)
    prefix = token_prefix.strip()
    if len(prefix) < 8:
        raise AgentError("请提供至少 8 位 token 前缀")
    full = None
    for tok in read_agent_tokens(agent):
        if tok.startswith(prefix):
            full = tok
            break
    if not full:
        raise AgentError("没有匹配该前缀的 token")
    _run_cli(["--home", str(_home(agent)), "token", "revoke", full], agent=agent)
    registry.drop_token(agent_id, prefix)
    audit("agent.token_revoke", agent=agent_id, prefix=prefix)
    return {"revoked": full[:8] + "…"}


# ── reconcile（掉线保护第二层：manager 重启/机器重启后按期望态拉齐）──

def reconcile() -> list[dict]:
    report = []
    for agent in registry.list_agents():
        unit = _unit(agent)
        desired = agent.get("desired_state", "running")
        state = systemdctl.unit_state(unit)
        action = "none"
        err = None
        try:
            if desired == "running":
                if state != "active":
                    if state == "not-found" or state == "inactive" or state == "failed":
                        _spawn_unit(agent)
                        wait_health(agent["port"], agent["id"], timeout=60)
                        action = "respawned"
            elif desired == "paused":
                if state != "active":
                    _spawn_unit(agent)
                    wait_health(agent["port"], agent["id"], timeout=60)
                    action = "respawned"
                systemdctl.kill_signal(unit, "SIGSTOP")
                action = (action + "+sigstop") if action != "none" else "sigstop"
            elif desired == "stopped":
                if state == "active":
                    systemdctl.stop(unit)
                    action = "stopped"
        except Exception as e:
            err = str(e)
        report.append({"id": agent["id"], "desired": desired, "was": state,
                       "action": action, "error": err})
    if any(r["action"] != "none" for r in report):
        audit("reconcile", report=report)
    return report


def list_status() -> list[dict]:
    return [status(a["id"]) for a in registry.list_agents()]
