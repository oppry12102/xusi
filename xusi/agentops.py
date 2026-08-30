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
互联标注；mission/budgets 在创建时渲染进 config.toml 后不再管理，
改它们走投信让 agent 自己改（内核每轮热重载）。**brains 例外**——
patch_agent 按密钥池手术式重渲染 [brain] + [brains.*] 段，其余段逐字节
保留（下次呼吸生效，不重启，见 _rewrite_brain_sections）。
"""
from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import socket
import threading
import time
import tomllib
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


_HEADER_RE = re.compile(r"^\s*\[([A-Za-z0-9_.\-]+)\]\s*(?:#.*)?(?:\r?\n)?$")


def _rewrite_brain_sections(agent: dict, chosen: list[str]) -> None:
    """手术式改写 <home>/config.toml：只替换 [brain] 段与全部 [brains.*]
    顶层块，其余内容（mission/display_timezone/[limits]/[agent]/
    [capabilities]/注释/agent 自加的自定义段）逐字节保留。渲染自密钥池
    （render_brain_section，与出生配置同源）。

    逐块删除（不用连续区域——[brains.*] 出现在 [limits] 等段之后时连续
    区域会误吞非大脑段）：每个大脑相关块的边界 = 段头行到下一个任意
    顶层段头之前（或文件尾）。写完先 tomllib 校验（解析成功 + brains
    键序与 default 完全一致）再 os.replace——任何一步失败原文件不动
    （内核对坏 TOML 保持旧值：写坏 = 本次修改静默失效，必须避免）。"""
    p = _home(agent) / "config.toml"
    # 1) 读原文件（utf-8 严格：TOML 源文件坏字节 = agent 写坏，报错别糊弄）
    try:
        text = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise AgentError(f"{p} 不存在（agent 首启前被删？）——无法改写大脑段，请先确认实例目录")
    except OSError as e:
        raise AgentError(f"读取 {p} 失败：{e}")
    # 2) 行游走识别顶层段头。段头 = 行首(可空白)单个方括号 [名字]，允许尾随
    #    注释；keepends 保行尾逐字节原样。[[x]] 数组表不匹配（要求 ] 后只有
    #    空白/注释/换行）。
    lines = text.splitlines(keepends=True)
    hdr: list[tuple[int, str]] = []
    for i, ln in enumerate(lines):
        m = _HEADER_RE.match(ln)
        if m:
            hdr.append((i, m.group(1)))
    # 3) 大脑相关 = [brain] 或 [brains.<name>]（[brains.x.extra] 子表会匹配
    #    startswith("brains.")——它随父块一起进入待删区；不单独放行，
    #    否则删除父块后它变孤儿段，校验会拦下）
    is_brain = lambda n: n == "brain" or (n.startswith("brains.") and len(n) > 7)
    deleted = [i for i, n in hdr if is_brain(n)]
    # 末尾补一个 \n = 与后续保留内容之间留空行分隔（被删块原本带的空行随块
    # 一起删掉了；render_brain_section 只保证块间空行，不保证块尾空行）
    rendered = "\n".join(brains.render_brain_section(chosen)) + "\n"
    if deleted:
        # 每个被删块到下一个任意顶层段头（或文件尾）；区间列表删掉，
        # 新渲染段插入第一个被删块的位置
        all_hdr = [i for i, _n in hdr]
        ranges: list[tuple[int, int]] = []
        for i in deleted:
            nxt = next((j for j in all_hdr if j > i), len(lines))
            ranges.append((i, nxt))
        merged: list[tuple[int, int]] = []
        for s, e in ranges:
            if merged and s <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], e))
            else:
                merged.append((s, e))
        parts: list[str] = []
        pos = 0
        for s, e in merged:
            parts.append("".join(lines[pos:s]))
            pos = e
        parts.append("".join(lines[pos:]))
        new_text = parts[0] + rendered + "".join(parts[1:])
    else:
        # 文件里没有任何大脑段（agent 全删了）：整段追加到文件尾
        new_text = text if text.endswith("\n") else text + "\n"
        new_text += "\n" + rendered
    # 4) 落盘前校验：解析必须成功，且 brains 键序 == chosen、default ==
    #    chosen[0]（顺序即故障转移序——tomllib 保文档序）。不符多半是撞上
    #    agent 并发编辑（或文件里藏着解析不到的大脑段变体）。
    try:
        parsed = tomllib.loads(new_text)
    except tomllib.TOMLDecodeError as e:
        raise AgentError(f"改写后的 config.toml 无法解析（原文件未动）：{e}")
    names = list((parsed.get("brains") or {}).keys())
    dflt = str((parsed.get("brain") or {}).get("default") or "")
    if names != chosen or dflt != chosen[0]:
        raise AgentError("改写后的大脑段与所选不一致（原文件未动）——可能撞上 agent 并发编辑，请重试")
    # 5) 原子落盘（同 _write_tokens：同目录临时文件 + os.replace；600 含 api_key）
    tmp = p.with_name(f"{p.name}.tmp-{uuid.uuid4().hex[:6]}")
    tmp.write_text(new_text, encoding="utf-8")
    try:
        tmp.chmod(0o600)
    except OSError:
        pass
    try:
        os.replace(tmp, p)
    except OSError as e:
        tmp.unlink(missing_ok=True)
        raise AgentError(f"改写 {p} 失败（原文件未动）：{e}")


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

# 可改字段 = 簿记层（name/note）+ 进程层（port/expose）+ 大脑（brains，
# 手术式重渲染 config.toml 的 [brain] + [brains.*] 段，下次呼吸生效）。
# mission/budgets 在创建后归 agent 自治——改它们请投信让 agent 自己
# 修改自己的 config.toml（内核每轮热重载）。
_PATCHABLE = {"name", "note", "port", "expose", "brains"}

_AGENT_OWNED = {
    "mission": "使命已由 agent 自治：请投信让它自己修改 config.toml（内核每轮热重载）",
    "budgets": "预算已由 agent 自治：请投信让它自己修改 config.toml 的 [limits] 段（v2.7.5+；旧内核为 [agent] 段）",
}


def patch_agent(agent_id: str, changes: dict, *, apply_restart: bool = False) -> dict:
    """改参。name/note 写注册表即生效；port/expose 改的是进程监听参数，
    返回 restart_required，?apply=restart 立即执行；brains 手术式重渲染
    config.toml 大脑段（下次呼吸生效，不重启），返回 brains_effective。"""
    agent = get_agent_or_404(agent_id)
    bad = set(changes) - _PATCHABLE
    owned = sorted(b for b in bad if b in _AGENT_OWNED)
    if owned:
        raise AgentError(
            f"这些字段已由 agent 自治：{', '.join(owned)}。"
            f"{_AGENT_OWNED[owned[0]]}")
    if bad:
        raise AgentError(f"不可修改的字段：{', '.join(sorted(bad))}（可改：{', '.join(sorted(_PATCHABLE))}）")

    # 大脑段手术放最前（最易失败——失败时其余字段一律未落，400 语义干净）。
    # 幂等 resync：与注册表快照相同也重渲染（轮换 brains.toml 的 key 后
    # 对 agent 做任意 PATCH 即触发重渲染，下次呼吸生效）。
    brains_new = None
    if "brains" in changes:
        bl = [str(b) for b in (changes["brains"] or [])]
        _validate_brains(bl)
        _rewrite_brain_sections(agent, bl)
        registry.update_agent(agent_id, {"brains": bl})   # 快照即真相（卡片/状态 tab）
        brains_new = bl

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
    ad: dict[str, Any] = {"fields": sorted(changes), "restarted": restarted}
    if brains_new is not None:
        ad.update(brains=brains_new, brains_effective="next_breath")
    audit("agent.patch", agent=agent_id, **ad)
    out = get_agent_or_404(agent_id)
    out = {**out, "restart_required": need_restart, "restarted": restarted}
    if brains_new is not None:
        out["brains_effective"] = "next_breath"   # 下次呼吸生效，不重启
    return out


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
_TOKEN_CAP = 3          # 补签时 xusi-observe token 封顶——401 持续时文件不随请求增长
_TOKEN_LOCK = threading.Lock()
_TAIL_WINDOW = 256 * 1024   # _tail_jsonl 首读窗口；凑不足 limit 行再放大到整文件


def _read_tokens(agent: dict) -> dict[str, dict]:
    """agent 的 data/webui_tokens.json：{token: {label, created_at}}。缺失/坏文件 → {}。"""
    p = _home(agent) / "data" / "webui_tokens.json"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_tokens(agent: dict, toks: dict[str, dict]) -> None:
    """原子落盘：同目录临时文件 + os.replace。内核每请求重读该文件，
    非原子写会让它读到半截 JSON（该请求的全部观察台凭证瞬时失效）。

    与内核 tokens.py 的并发写没有共享锁：撤销与补签同一毫秒窗时可能把刚
    撤销的 token 写回去（窗口极小、双方都是低频管理操作）——原子写至少
    保证内核永不见半截文件。"""
    p = _home(agent) / "data" / "webui_tokens.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f"{p.name}.tmp-{uuid.uuid4().hex[:6]}")
    tmp.write_text(json.dumps(toks, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)


def _observe_token(agent: dict, *, force_new: bool = False) -> str:
    """观察台 token：读文件首个；文件空/缺失或 force_new（401 后补签）时自动
    签发一枚并 merge 写回——锁内重读再写，并发签发不互相覆盖（merge 不覆盖）。

    补签时按 created_at 修剪自家旧 token（保最新 _TOKEN_CAP 枚，别人的不碰）：
    若内核是不逐请求重读 token 文件的旧版本（每 agent 自带源码快照），401 会
    持续，不修剪则文件随请求无限增长。写的是内核 tokens.py 的原始文件格式
    （json.dumps ensure_ascii=False indent=2），token 用 secrets.token_urlsafe(32)
    与内核一致；内核 require_token 每请求重读该文件，免重启生效。"""
    with _TOKEN_LOCK:
        toks = _read_tokens(agent)
        if toks and not force_new:
            return next(iter(toks))
        if not _home(agent).exists():
            raise AgentError("实例目录不存在")
        raw = secrets.token_urlsafe(32)
        toks = _read_tokens(agent) or {}   # 锁内重读：保留并发写下的其它 token
        if force_new:
            mine = sorted(
                ((t, m) for t, m in toks.items()
                 if isinstance(m, dict) and m.get("label") == _TOKEN_LABEL),
                key=lambda x: (x[1] or {}).get("created_at", ""))
            for t, _m in mine[:max(0, len(mine) - (_TOKEN_CAP - 1))]:
                del toks[t]
        toks[raw] = {"label": _TOKEN_LABEL, "created_at": _iso()}
        _write_tokens(agent, toks)
        return raw


def _get(agent: dict, path: str, token: str) -> "httpx.Response":
    """观察 GET。httpx 在函数内惰性 import：它是观察通道独有的第三方依赖，
    模块级 import 会让「venv 缺 httpx」炸掉整个 agentops（api 与 CLI 全量
    import 本模块），惰性后只废 observe 一条窄通道。"""
    import httpx
    url = f"http://127.0.0.1:{agent['port']}{path}"
    return httpx.get(url, headers={"Authorization": f"Bearer {token}"},
                     timeout=_OBSERVE_TIMEOUT)


def _tail_jsonl(path: Path, limit: int) -> list[dict]:
    """追加型 jsonl 取尾部 limit 行（坏行/非 dict 跳过，文件序返回）。

    mailbox() 与 sessions() 共用。只回读尾部窗口（默认 256KB）——长跑 agent
    的 jsonl 随呼吸无界增长，整读是 O(文件大小)/请求；窗口内凑不足 limit 行
    再放大到整文件兜底。"""
    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
    except OSError:
        return []
    window = _TAIL_WINDOW
    while True:
        start = max(0, size - window)
        with path.open("rb") as f:
            f.seek(start)
            chunk = f.read()
        lines = chunk.decode("utf-8", errors="replace").splitlines()
        if start > 0:
            lines = lines[1:]   # 窗口切在行中间：首行是残行，丢弃
        if len(lines) >= limit or start == 0:
            break
        window = size
    rows: list[dict] = []
    for line in lines[-limit:]:
        try:
            obj = json.loads(line)
            if isinstance(obj, dict):
                rows.append(obj)
        except ValueError:
            continue
    return rows


def observe(agent_id: str, what: str, limit: int = 80) -> Any:
    """只读观察：events / status 两条窄通道（详情页事件流/工具统计/会话 banner）。

    token 缺失自动签发（写 data/webui_tokens.json）；401 补签一枚重试一次，
    仍 401 才报错（文件被外部撤销/替换）。events 在 agent 进程内存
    （环形缓冲，重启即清零），status 原样透传内核 dict。"""
    agent = get_agent_or_404(agent_id)
    unit = _unit(agent)
    if systemdctl.unit_state(unit) != "active":
        raise AgentError("agent 未在运行")
    if systemdctl.main_stopped(unit):
        # 暂停（SIGSTOP）单元仍 active、端口还挂着，但进程不响应——
        # 不拦的话每个 tab 挂满 6s 超时才失败；先探 /proc（同 stop/restart）
        raise AgentError("agent 进程已暂停（SIGSTOP 冻结）——先「续跑」再观察")
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
        try:
            data = r.json()
        except ValueError:
            # 端口被非 xuseek 服务占用等：200 但响应不是 JSON
            raise AgentError("上游响应不是 JSON（端口可能被非 xuseek 服务占用）")
    except httpx.HTTPError as e:
        raise AgentError(f"无法连接 agent 内核（{type(e).__name__}）") from None
    if what == "events" and isinstance(data, dict):
        return data.get("events", [])
    return data


def sessions(agent_id: str, limit: int = 30) -> dict:
    """会话索引：读 data/sessions.jsonl 尾部 N 行，最新在前。纯磁盘读取——
    索引是每口呼吸追加的落盘事实，agent 停机也能看历史呼吸。坏行跳过
    （追加型文件尾部可能有半行）。实现与 mailbox() 同构。"""
    agent = get_agent_or_404(agent_id)
    limit = max(1, min(int(limit), 200))   # 与内核 /v1/sessions 上限一致
    rows = _tail_jsonl(_home(agent) / "data" / "sessions.jsonl", limit)
    rows.reverse()
    return {"id": agent_id, "sessions": rows}


_BOOT_CAP = 64_000   # Boot tab 展示封顶（超出截断打标；内核注入硬截 32k，展示墙放宽一档）


def boot(agent_id: str) -> dict:
    """Boot 自述全文：读 workspace/BOOT.md（磁盘事实，agent 停机也能看）。
    与内核注入同口径 errors="replace"（文件 100% 归大脑自管，坏字节 → U+FFFD
    不炸管理面）；缺失（首口呼吸前的年轻 agent）→ exists=False。"""
    agent = get_agent_or_404(agent_id)
    p = _home(agent) / "workspace" / "BOOT.md"
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {"id": agent_id, "exists": False, "text": "", "truncated": False}
    truncated = len(text) > _BOOT_CAP
    return {"id": agent_id, "exists": True,
            "text": text[:_BOOT_CAP] if truncated else text, "truncated": truncated}


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
    rows = _tail_jsonl(_home(agent) / "data" / name, limit)
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
