"""agent 生命周期操作：创建/启停/暂停/续跑/重启/改参/删除/观察/投信/收信。

manager 与 agent 之间只有**一条写**通道——管理邮箱：
- 投信：追加 `<home>/data/mailbox.jsonl`（sender=admin，与内核 post() 同语义，
  双写 mailbox_log.jsonl 保历史）；
- 收信：读 `<home>/data/outbox.jsonl`（内核 send_mail 工具写，sender=brain）。

只读观察收窄为两条（详情页事件流/工具统计/会话 banner 用）：
- HTTP GET /v1/events、/v1/status（observe，见下）；观察台 token 缺失时
  xusi 自动签发一枚写进 data/webui_tokens.json（内核每次校验都重读该文件，
  免重启生效）；
- 会话索引读磁盘 data/sessions.jsonl（sessions，不依赖 HTTP/token，
  agent 停机也能看历史呼吸）。

其余界面全部取消：不调 xuseek CLI（init/token/capabilities）、
不再渲染（重写）config.toml（创建时渲染一次出生配置，此后归 agent 自治）、
不反代。

进程与信号（systemd）不是"通信"——它是管理面不可削减的宿主职责：
spawn `xuseek.sh serve` / stop / SIGSTOP / SIGCONT / journalctl 日志。

参数事实源：注册表（etc/agents.json）只记簿记（name/note/port/expose 等）与
互联标注；mission/brains/budgets 在创建时渲染进 config.toml 后不再管理，
改它们走投信让 agent 自己改（内核每轮热重载）。
"""
from __future__ import annotations

import json
import re
import secrets
import shutil
import socket
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import brains, ports, registry, systemdctl, versions
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


def _source_for(agent: dict) -> Path:
    """该 agent 的 xuseek-v2 源码目录 = 实例私有副本 instances/<id>/xuseek-v2/
    （创建时从版本仓库解压，实例间完全隔离，可各跑各的版本）。"""
    ver = str(agent.get("source_version") or "").strip()
    p = _home(agent) / versions.SRC_DIR_NAME
    if not (p / "xuseek.sh").exists():
        raise AgentError(
            f"agent {agent['id']} 的私有源码副本缺失：{p}（版本 {ver}）。"
            f"实例目录可能被改动——可从版本仓库重新解压到该路径，或停机重建")
    return p


def _spawn_unit(agent: dict) -> None:
    """统一拉起入口：定位该 agent 的源码（私有副本或共享主源码）→ systemd-run
    瞬态单元（Restart=always）。"""
    cfg = get_config()
    src = _source_for(agent)
    systemdctl.spawn_agent(cfg.unit_name(agent["id"]), str(src),
                           str(_home(agent)), _listen_host(agent), agent["port"])


# ── 生命周期 ─────────────────────────────────────────────────────────

def _resolve_source_choice(src_ver: str) -> str:
    """创建时的源码抉择：显式版本 → 提前校验后直接用（失败零副作用）；
    缺省 → **版本仓库最新包**（每个 agent 自带 xuseek-v2 私有副本，
    instances/<id>/ 自洽、可单独迁移）。仓库为空 → 报错并指引投放 zip。"""
    if src_ver:
        versions.zip_for(src_ver)
        return src_ver
    avail = versions.list_versions()
    if not avail:
        raise AgentError(
            f"版本仓库（{get_config().versions_dir}）为空。"
            f"请管理员投放 xuseek-v2-<版本号>.zip（见 docs/versions.md）")
    return avail[0]["version"]   # list_versions 已按版本号新→旧排序


def _validate_brains(bl: list[str]) -> None:
    """校验大脑列表（ brains.validate_selection 的 AgentError 适配——
    create / restore 共用同一份断言，见 brains.py）。"""
    try:
        brains.validate_selection(bl)
    except ValueError as e:
        raise AgentError(str(e)) from None


def create_agent(name: str, mission: str, brain_list: list[str], *,
                 expose: bool = False, port: int | None = None,
                 budgets: dict | None = None, note: str = "",
                 source_version: str = "") -> dict:
    """创建并启动一个 agent：渲染出生 config.toml → 注册 → systemd 拉起 → 端口验收。

    source_version：版本号 → 该版本源码解压成实例私有副本（instances/<id>/xuseek-v2/，
    删除时随 home 进 .trash）；缺省 → 版本仓库最新包（每 agent 自带私有副本，
    实例自洽可单独迁移；仓库为空报错，见 _resolve_source_choice）。

    创建后 xusi 与该 agent 只剩邮箱通道：不再签发任何 agent 侧凭证
    （agent 自签自报）、不再改写 config.toml（mission/brains/budgets 归 agent 自治）。
    """
    cfg = get_config()
    mission = (mission or "").strip()
    if not mission:
        raise AgentError("mission 不能为空")
    _validate_brains(brain_list)
    src_ver = _resolve_source_choice((source_version or "").strip())

    agent_id = gen_id(name)
    # 端口分配 → 注册表落盘必须整体持锁（见 ports.ALLOC_LOCK）：窗口内含解压与
    # 渲染（分钟级）。锁内串行——create 本就是低频 admin 操作。
    with ports.ALLOC_LOCK:
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
        }

        def _fail(e: Exception) -> None:
            """回滚 + 统一报错——锁内落盘失败与锁外拉起失败同一条收尾。"""
            _rollback_create(unit, home, agent_id)
            raise AgentError(f"创建失败已回滚：{e}") from e

        try:
            _init_workspace(rec, src_ver)
            # 注册（期望态 running）
            registry.add_agent(rec)
        except Exception as e:
            _fail(e)

    # 锁外拉起：落盘后端口已被三重检验挡住，并发 create 不再互相等 90s 验收。
    # 失败路径同样回滚——「锁外等价」含失败语义，否则验收不过的 agent 会以
    # desired=running 赖在注册表里，靠 reconcile 反复拉起一个起不来的单元。
    # 例外：验收超时但单元仍在跑 = 首启装依赖慢于验收窗——再等一轮，别把
    # 能起来的 agent 误销毁；单元已死（spawn 失败/崩溃循环）才立即回滚。
    try:
        spawn_and_verify(rec)
    except Exception as e:
        if systemdctl.unit_state(unit) == "active":
            try:
                wait_health(rec["port"], rec["id"])
            except Exception:
                _fail(e)
        else:
            _fail(e)

    audit("agent.create", agent=agent_id, name=rec["name"], port=port,
          expose=expose, brains=brain_list, source=src_ver,
          source_defaulted=not (source_version or "").strip())
    return get_agent_or_404(agent_id)


def _init_workspace(rec: dict, src_ver: str) -> None:
    """在 agent 被注册/拉起之前，把它的 home 准备到位：

    - 从版本仓库解压源码到实例私有副本（instances/<id>/xuseek-v2/）
    - 渲染 config.toml（含所选大脑与 key）——出生配置，唯一一次
      （data/、workspace/ 由内核启动时自建，xusi 不动）

    失败抛 AgentError，由 create_agent 的统一 try/except 回滚（home 仍可能存在，
    rollback 把它挪进 .trash）。"""
    home = _home(rec)
    home.mkdir(parents=True, exist_ok=True)
    versions.extract(src_ver, home / versions.SRC_DIR_NAME)
    brains.write_agent_config(home, rec["mission"], rec["brains"], rec["budgets"],
                              source_version=src_ver)


def spawn_and_verify(rec: dict) -> None:
    """systemd 拉起 + 端口验收。失败抛 AgentError。

    公开给 backup.restore 复用（create 的私有实现提级——恢复与创建走同一条
    拉起路径，别再各自 systemdctl.spawn_agent）。"""
    _spawn_unit(rec)
    wait_health(rec["port"], rec["id"])


def wait_health(port: int, agent_id: str, timeout: float = 90.0) -> None:
    """启动验收：systemd 单元 active 且端口已进入监听（ss）。失败抛 AgentError
    （附日志尾部）。

    HTTP /v1/health 探活已取消（xusi 与 agent 只剩邮箱通道）——内核 serve 先
    跑 preflight（config 缺失则写模板、同档无可用大脑则退出）才起 uvicorn：
    端口进入监听 = preflight 已通过；preflight 失败时端口不会绑定，错误经
    单元日志尾部暴露。绑端口之后的崩溃由 systemd Restart=always 兜底。

    主机缺 ss 时降级为 loopback connect 试探——ss 缺失会让
    _kernel_listening_ports 恒空集，健康 agent 也会验收超时（被误销毁）。"""
    have_ss = shutil.which("ss") is not None
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if systemdctl.unit_state(get_config().unit_name(agent_id)) != "active":
            time.sleep(0.6)
            continue
        listening = (port in ports._kernel_listening_ports()
                     if have_ss else _loopback_listening(port))
        if listening:
            return
        time.sleep(0.6)
    log = systemdctl.journal_tail(get_config().unit_name(agent_id), 20)
    raise AgentError(f"agent 启动后 {timeout:.0f}s 未通过验收"
                     f"（单元未 active 或端口 {port} 未监听）。日志尾部：\n{log}")


def _loopback_listening(port: int) -> bool:
    """缺 ss 时的端口验收降级：TCP connect 试探 127.0.0.1（agent 监听
    0.0.0.0 或 127.0.0.1 均可达）。"""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def _rollback_create(unit: str, home: Path, agent_id: str) -> None:
    """create 失败收尾：停单元、挪 home 进 .trash、注销、记审计。

    注销是硬要求——失败必须冒泡（注册表留 desired=running 的僵尸会被
    reconcile 反复拉起一个起不来的单元）；挪 home 是尽力而为，挪不动就
    原地留着（注销之后 reconcile 看不见它，孤儿目录交管理员清）。"""
    for fn in (systemdctl.stop, systemdctl.reset_failed):
        try:
            fn(unit)
        except Exception:
            pass
    if home.exists():
        try:
            dest = get_config().trash_dir / f"{agent_id}-{uuid.uuid4().hex[:6]}"
            shutil.move(str(home), str(dest))
        except Exception:
            pass
    registry.remove_agent(agent_id)
    audit("agent.create.rollback", agent=agent_id)


def get_agent_or_404(agent_id: str) -> dict:
    a = registry.get_agent(agent_id)
    if not a:
        raise AgentError(f"agent 不存在: {agent_id}")
    return a


def start(agent_id: str) -> dict:
    agent = get_agent_or_404(agent_id)
    if systemdctl.unit_state(_unit(agent)) != "active":
        _spawn_unit(agent)
        wait_health(agent["port"], agent_id)
    return _finalize(agent_id, "running", "start")


def stop(agent_id: str) -> dict:
    """优雅停（SIGTERM → xuseek 轮边界落盘；TimeoutStopSec 兜底）。
    冻结进程收不到 SIGTERM——先探主进程实况（/proc T 态），SIGSTOP 中先
    SIGCONT 解冻再停（否则拖到 SIGKILL、丢会话）。覆盖 desired=paused 与
    「冻结孤儿」（registry 说 running、进程实际被冻）两种态。"""
    agent = get_agent_or_404(agent_id)
    if systemdctl.main_stopped(_unit(agent)):
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
    return _finalize(agent_id, "stopped", "stop")


def pause(agent_id: str) -> dict:
    """冻结（SIGSTOP）：进程驻留、端口无响应、呼吸暂停。"""
    agent = get_agent_or_404(agent_id)
    unit = _unit(agent)
    if systemdctl.unit_state(unit) != "active":
        raise AgentError("agent 未在运行，无法暂停（先 start）")
    systemdctl.kill_signal(unit, "SIGSTOP")
    return _finalize(agent_id, "paused", "pause")


def resume(agent_id: str) -> dict:
    agent = get_agent_or_404(agent_id)
    unit = _unit(agent)
    if systemdctl.unit_state(unit) != "active":
        raise AgentError("agent 未在运行（先 start）")
    systemdctl.kill_signal(unit, "SIGCONT")
    return _finalize(agent_id, "running", "resume")


def restart(agent_id: str) -> dict:
    """优雅重启：SIGTERM 落盘 → 重新拉起 → 端口验收。冻结态先解冻（同 stop——
    冻结进程收不到 SIGTERM，硬 restart 会拖到 SIGKILL、丢会话）。"""
    agent = get_agent_or_404(agent_id)
    if systemdctl.unit_state(_unit(agent)) == "active":
        if systemdctl.main_stopped(_unit(agent)):
            try:
                systemdctl.kill_signal(_unit(agent), "SIGCONT")
            except systemdctl.SystemdError:
                pass
        systemdctl.restart(_unit(agent))
    else:
        _spawn_unit(agent)
    wait_health(agent["port"], agent_id)
    return _finalize(agent_id, "running", "restart")


def _finalize(agent_id: str, desired: str, action: str) -> dict:
    """生命周期动作收尾：写 desired_state → 审计 → 返回统一形态。

    5 个 start/stop/pause/resume/restart 共用——成功路径必以这一行收尾。"""
    registry.update_agent(agent_id, {"desired_state": desired})
    audit(f"agent.{action}", agent=agent_id)
    return {"id": agent_id, "desired_state": desired}


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
    from . import mailroom
    mailroom.forget(agent_id)
    audit("agent.delete", agent=agent_id, port=agent["port"], trash=str(dest))
    return {"id": agent_id, "deleted": True, "moved_to": str(dest)}


# ── 改参 ─────────────────────────────────────────────────────────────

# 可改字段 = 簿记层（name/note）+ 进程层（port/expose）。
# mission/brains/budgets 在创建后归 agent 自治——改它们请投信让 agent 自己
# 修改自己的 config.toml（内核每轮热重载）。
_PATCHABLE = {"name", "note", "port", "expose"}

_AGENT_OWNED = {
    "mission": "使命已由 agent 自治：请投信让它自己修改 config.toml（内核每轮热重载）",
    "brains": "大脑已由 agent 自治：请投信让它自己修改 config.toml 的 [brains.*]（新 api_key 可向管理员索取）",
    "budgets": "预算已由 agent 自治：请投信让它自己修改 config.toml 的 [limits] 段（v2.7.5+；旧内核为 [agent] 段）",
}


def patch_agent(agent_id: str, changes: dict, *, apply_restart: bool = False) -> dict:
    """改参。name/note 写注册表即生效；port/expose 改的是进程监听参数，
    返回 restart_required，?apply=restart 立即执行。"""
    agent = get_agent_or_404(agent_id)
    bad = set(changes) - _PATCHABLE
    owned = sorted(b for b in bad if b in _AGENT_OWNED)
    if owned:
        raise AgentError(
            f"这些字段已由 agent 自治：{', '.join(owned)}。"
            f"{_AGENT_OWNED[owned[0]]}")
    if bad:
        raise AgentError(f"不可修改的字段：{', '.join(sorted(bad))}（可改：{', '.join(sorted(_PATCHABLE))}）")

    hot = {}       # 写注册表即生效
    need_restart = False

    if "name" in changes:
        hot["name"] = str(changes["name"]).strip() or agent["name"]
    if "note" in changes:
        hot["note"] = str(changes["note"])

    next_rec = {**agent, **hot}
    # 换端口时「检验可用 → 注册表落盘」与 create 的分配窗口互斥（ports.ALLOC_LOCK）
    with ports.ALLOC_LOCK:
        if "port" in changes and int(changes["port"]) != int(agent["port"]):
            ports.allocate(int(changes["port"]))   # 检验可用（含 not-in-use）
            next_rec["port"] = int(changes["port"])
            need_restart = True
        if "expose" in changes and bool(changes["expose"]) != bool(agent.get("expose")):
            next_rec["expose"] = bool(changes["expose"])
            need_restart = True

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
    """换监听参数的重启：stop 旧瞬态单元 → 以新 host/port 重新拉起。
    冻结态先解冻（同 stop——冻结进程收不到 SIGTERM）。"""
    unit = _unit(agent)
    if systemdctl.unit_state(unit) == "active":
        if systemdctl.main_stopped(unit):
            try:
                systemdctl.kill_signal(unit, "SIGCONT")
            except systemdctl.SystemdError:
                pass
        systemdctl.stop(unit)
    _spawn_unit(agent)
    wait_health(agent["port"], agent["id"])


# ── 状态（systemd + 注册表；只读观察另见 observe）────────────────────

def status(agent_id: str) -> dict:
    """状态聚合：注册表 + systemd 单元 + 互联标注。不再探 agent 的 HTTP。"""
    agent = get_agent_or_404(agent_id)
    out: dict[str, Any] = {
        "id": agent["id"],
        "name": agent["name"],
        "mission": agent["mission"],
        "brains": agent["brains"],
        "budgets": agent.get("budgets", {}),
        "port": agent["port"],
        "expose": agent.get("expose", False),
        "note": agent.get("note", ""),
        "source_version": agent.get("source_version", ""),
        "desired_state": agent.get("desired_state", "running"),
        "listen_host": _listen_host(agent),
        "interconnect": agent.get("interconnect"),
        "created_at": agent.get("created_at"),
        "updated_at": agent.get("updated_at"),
        "fetched_at": _iso(),
    }
    out["process"] = systemdctl.unit_brief(_unit(agent))
    return out


def logs(agent_id: str, n: int = 200) -> dict:
    agent = get_agent_or_404(agent_id)
    n = max(1, min(int(n), 1000))
    text = systemdctl.journal_tail(_unit(agent), n)
    return {"id": agent_id, "lines": text.splitlines()[-n:]}


# ── 观察（只读 HTTP 两条：events/status）与会话（磁盘）────────────────

_OBSERVE_TIMEOUT = 6.0
_TOKEN_LABEL = "xusi-observe"
_TOKEN_LOCK = threading.Lock()


def _read_tokens(agent: dict) -> dict[str, dict]:
    """agent 的 data/webui_tokens.json：{token: {label, created_at}}。缺失/坏文件 → {}。"""
    p = _home(agent) / "data" / "webui_tokens.json"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _observe_token(agent: dict, *, force_new: bool = False) -> str:
    """观察台 token：读文件首个；文件空/缺失或 force_new（401 后补签）时自动
    签发一枚并 merge 写回——锁内重读再写，并发签发不互相覆盖（merge 不覆盖）。

    写的是内核 tokens.py 的原始文件格式（json.dumps ensure_ascii=False indent=2），
    token 用 secrets.token_urlsafe(32) 与内核一致；内核 require_token 每请求重读
    该文件，免重启生效。"""
    with _TOKEN_LOCK:
        toks = _read_tokens(agent)
        if toks and not force_new:
            return next(iter(toks))
        home = _home(agent)
        if not home.exists():
            raise AgentError("实例目录不存在")
        raw = secrets.token_urlsafe(32)
        toks = _read_tokens(agent) or {}   # 锁内重读：保留并发写下的其它 token
        toks[raw] = {"label": _TOKEN_LABEL, "created_at": _iso()}
        p = home / "data" / "webui_tokens.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(toks, ensure_ascii=False, indent=2),
                     encoding="utf-8")
        return raw


def _get(agent: dict, path: str, token: str) -> "httpx.Response":
    """观察 GET。httpx 在函数内惰性 import：它是观察通道独有的第三方依赖，
    模块级 import 会让「venv 缺 httpx」炸掉整个 agentops（api 与 CLI 全量
    import 本模块），惰性后只废 observe 一条窄通道。"""
    import httpx
    url = f"http://127.0.0.1:{agent['port']}{path}"
    return httpx.get(url, headers={"Authorization": f"Bearer {token}"},
                     timeout=_OBSERVE_TIMEOUT)


def observe(agent_id: str, what: str, limit: int = 80) -> Any:
    """只读观察：events / status 两条窄通道（详情页事件流/工具统计/会话 banner）。

    token 缺失自动签发（写 data/webui_tokens.json）；401 补签一枚重试一次，
    仍 401 才报错（文件被外部撤销/替换）。events 在 agent 进程内存
    （环形缓冲，重启即清零），status 原样透传内核 dict。"""
    agent = get_agent_or_404(agent_id)
    if systemdctl.unit_state(_unit(agent)) != "active":
        raise AgentError("agent 未在运行")
    if what not in ("events", "status"):
        raise AgentError("观察项须为 events/status 之一")
    limit = max(1, min(int(limit), 500))
    path = f"/v1/{what}" if what == "status" else f"/v1/events?limit={limit}"
    try:
        import httpx
    except ImportError:
        raise AgentError(
            "缺少 httpx 依赖（pip install httpx）——只读观察不可用，"
            "管理面其余功能不受影响") from None
    try:
        r = _get(agent, path, _observe_token(agent))
        if r.status_code == 401:
            # 内核判定手里 token 失效（文件被改/撤销）——补签一枚再试一次
            r = _get(agent, path, _observe_token(agent, force_new=True))
        if r.status_code == 401:
            raise AgentError("观察台 token 全部失效（重新签发一个）")
        if r.status_code != 200:
            raise AgentError(f"上游 HTTP {r.status_code}")
    except httpx.HTTPError as e:
        raise AgentError(f"无法连接 agent 内核（{type(e).__name__}）") from None
    data = r.json()
    if what == "events" and isinstance(data, dict):
        return data.get("events", [])
    return data


def sessions(agent_id: str, limit: int = 30) -> dict:
    """会话索引：读 data/sessions.jsonl 尾部 N 行，最新在前。纯磁盘读取——
    索引是每口呼吸追加的落盘事实，agent 停机也能看历史呼吸。坏行跳过
    （追加型文件尾部可能有半行）。实现与 mailbox() 同构。"""
    agent = get_agent_or_404(agent_id)
    limit = max(1, min(int(limit), 200))   # 与内核 /v1/sessions 上限一致
    p = _home(agent) / "data" / "sessions.jsonl"
    try:
        text = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        text = ""
    rows: list[dict] = []
    for line in text.splitlines()[-limit:]:
        try:
            obj = json.loads(line)
            if isinstance(obj, dict):
                rows.append(obj)
        except ValueError:
            continue
    rows.reverse()
    return {"id": agent_id, "sessions": rows}


# ── 投信 / 收信（唯一的写通道）──────────────────────────────────────

_MAIL_FIELDS = ("id", "sender", "text", "at")


def _append_mail_line(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(line)


def mail(agent_id: str, text: str) -> dict:
    """给大脑投信：追加 data/mailbox.jsonl（与内核 post() 完全同语义——双写
    mailbox_log.jsonl 保历史）。休眠中 5s 内被轮询唤醒。"""
    agent = get_agent_or_404(agent_id)
    text = (text or "").strip()
    if not text:
        raise AgentError("信件内容不能为空")
    home = _home(agent)
    if not home.exists():
        raise AgentError("实例目录不存在")
    msg = {"id": uuid.uuid4().hex[:12], "sender": "admin", "text": text, "at": _iso()}
    line = json.dumps(msg, ensure_ascii=False) + "\n"
    # 与内核 mailbox.post() 同语义：mailbox.jsonl 给 daemon 收信；
    # mailbox_log.jsonl 是观测历史（agent drain 后 pending 清空，历史保留，
    # 详情页"来信历史"就走它读——漏写会被 agent 拿走再清掉，看起来"丢了"）
    for name in ("mailbox.jsonl", "mailbox_log.jsonl"):
        _append_mail_line(home / "data" / name, line)
    audit("agent.mail", agent=agent_id, chars=len(text))
    return {"posted": True, "id": msg["id"], "at": msg["at"]}


def mailbox(agent_id: str, limit: int = 50, *, box: str = "outbox") -> dict:
    """读邮箱文件尾部 N 行（只读展示，不推进 mailroom 的扫描偏移）。

    box="outbox"：来信（内核 send_mail 写，sender=brain）；
    box="inbox"： 投信历史（mailbox_log.jsonl，sender=admin 为主——管理邮箱
    的观测日志，投信时双写，语义与内核 post() 一致）。
    """
    agent = get_agent_or_404(agent_id)
    limit = max(1, min(int(limit), 500))
    name = {"outbox": "outbox.jsonl", "inbox": "mailbox_log.jsonl"}.get(box)
    if not name:
        raise AgentError(f"未知邮箱文件：{box}（可选 outbox/inbox）")
    p = _home(agent) / "data" / name
    try:
        text = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        text = ""
    rows: list[dict] = []
    for line in text.splitlines()[-limit:]:
        try:
            obj = json.loads(line)
            if isinstance(obj, dict):
                rows.append(obj)
        except ValueError:
            continue
    return {"id": agent_id, "box": box, "messages": rows}


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
                elif systemdctl.main_stopped(unit):
                    # 冻结孤儿：manager 在备份冻结窗 / pause 中途崩掉留下的态
                    # （期望 running 却被 SIGSTOP——systemd 层 state 仍 active，
                    # 单纯比对期望态发现不了）。SIGCONT 幂等，恢复呼吸。
                    systemdctl.kill_signal(unit, "SIGCONT")
                    action = "sigcont-rescue"
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
