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

进程与信号不是"通信"——它是管理面不可削减的宿主职责：
spawn `xuseek.sh serve` / stop / SIGSTOP / SIGCONT / 日志。**双运行时**：
注册表 runtime 字段（systemd 默认 / docker 容器）经 _rt() 分派到
systemdctl.py 或 dockerctl.py（Runtime 协议，函数形状对齐）——其余
代码不感知进程载体。

参数事实源：注册表（etc/agents.json）只记簿记（name/note/port/expose/roots 快照等）；
mission/budgets 在创建时渲染进 config.toml 后不再管理，
改它们走投信让 agent 自己改（内核每轮热重载）。**brains 例外**——
patch_agent 按密钥池手术式重渲染 [brain] + [brains.*] 段，其余段逐字节
保留（下次呼吸生效，不重启，见 _rewrite_brain_sections）。

互联是 xuseek 内核自家业务（根智能体 + [[roots]] 出生交割）——xusi 不参与。
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
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import brains, dockerctl, ports, registry, systemdctl, versions
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

def gen_id(_name: str = "") -> str:
    """新 agent 的 id：前缀统一 agent-<4 位随机 hex>，与显示名彻底解耦——
    名字不进 id（拼音残根/英文词不再产生奇形前缀）；辨识度归别名
    （注册表 name 字段，管理员随时改、可重复，纯显示）。已有 agent 的 id 不动。
    撞号重摇：本机注册表查重兜底（终身唯一性另由出生 config 的 instance_id
    交割保证——那是实例自己的身份事实源，注册表只是「本机住着谁」的缓存）。"""
    while True:
        aid = f"agent-{uuid.uuid4().hex[:4]}"
        if registry.get_agent(aid) is None:
            return aid


def _home(agent: dict) -> Path:
    return get_config().instance_home(agent["id"])


def _unit(agent: dict) -> str:
    return get_config().unit_name(agent["id"])


def _rt(agent: dict):
    """按注册表 runtime 分派运行时模块（Runtime 协议：systemdctl/dockerctl
    函数形状对齐）。旧记录无该字段 → systemd（零迁移，行为与从前一致）。"""
    return dockerctl if agent.get("runtime") == "docker" else systemdctl


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
    """统一拉起入口：定位该 agent 的源码（私有副本）→ 按 runtime 分派拉起
    （systemd 瞬态单元 / docker 容器，镜像 tag 随 source_version）。"""
    cfg = get_config()
    src = _source_for(agent)
    _rt(agent).spawn_agent(cfg.unit_name(agent["id"]), str(src),
                           str(_home(agent)), _listen_host(agent), agent["port"],
                           version=str(agent.get("source_version") or ""))


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


_ROOTS_CAP = 8            # [[roots]] 条目封顶（互为备份，8 个足够）
_ROOTS_FIELD_MAX = 512    # address / token 单字段长度上限


def _validate_roots(roots: list | None, src_ver: str) -> list[dict]:
    """校验创建时的根智能体列表，返回规范化条目（非空 address/token、去重保序）。

    非空时要求内核 ≥ 2.7.12——[[roots]] 出生交割自该版起；旧核不识此段，
    渲染了也不会交割（静默失效），直接拒绝。创建后接入的路径是投信
    （内核 docs/interconnect.md：大脑 send_mail 向管理员索取地址与 token）。"""
    if not roots:
        return []
    if not versions.at_least(src_ver, "2.7.12"):
        raise AgentError(
            f"所选内核版本 {src_ver} 不支持 [[roots]]（v2.7.12 起才有根智能体出生交割）。"
            f"换新版本，或去掉根智能体——创建后接入走投信，见内核 docs/interconnect.md")
    if len(roots) > _ROOTS_CAP:
        raise AgentError(f"根智能体最多 {_ROOTS_CAP} 个（当前 {len(roots)}）")
    out: list[dict] = []
    for r in roots:
        if not isinstance(r, dict):
            raise AgentError("根智能体条目须为 {address, token}")
        addr = str(r.get("address") or "").strip()
        tok = str(r.get("token") or "").strip()
        if not addr or not tok:
            raise AgentError("根智能体条目须同时填 address 与 token（缺一不会交割）")
        if len(addr) > _ROOTS_FIELD_MAX or len(tok) > _ROOTS_FIELD_MAX:
            raise AgentError(f"根智能体 address/token 超长（各 ≤{_ROOTS_FIELD_MAX}）")
        out.append({"address": addr, "token": tok})
    # 同地址去重保序（同 brains 渲染的 dict.fromkeys 手法）
    seen: set[str] = set()
    uniq: list[dict] = []
    for r in out:
        if r["address"] not in seen:
            seen.add(r["address"])
            uniq.append(r)
    return uniq


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
                 source_version: str = "",
                 roots: list | None = None,
                 extra_config: str = "",
                 runtime: str = "") -> dict:
    """创建并启动一个 agent：渲染出生 config.toml → 注册 → 按 runtime 拉起
    （systemd 直跑 / docker 容器）→ 端口验收。

    runtime：systemd（默认，系统进程）或 docker（容器，host 网络）——
    缺省取 [manager].default_runtime。docker 要求内核 ≥ v2.7.19（Dockerfile
    自该版本起才有）与本机 docker 环境，创建前早校验（失败零副作用，
    不拖到验收超时）。创建后仍可切换（停止 → 改参 → 启动，见 patch_agent）。

    source_version：版本号 → 该版本源码解压成实例私有副本（instances/<id>/xuseek-v2/，
    删除时随 home 进 .trash）；缺省 → 版本仓库最新包（每 agent 自带私有副本，
    实例自洽可单独迁移；仓库为空报错，见 _resolve_source_choice）。

    roots（可选）：根智能体列表 [{address, token}]——渲染进出生 config 的
    [[roots]] 段，内核首次启动交割到 workspace/playbook/根智能体.json
    （v2.7.12+，见 _validate_roots）。extra_config（可选）：管理员自由 TOML
    原样追加（落盘前整体校验，见 brains.render_agent_config）。

    创建后 xusi 与该 agent 只剩邮箱通道：不再签发任何 agent 侧凭证
    （agent 自签自报）、不再改写 config.toml（mission/brains/budgets 归 agent 自治）。
    """
    cfg = get_config()
    mission = (mission or "").strip()
    if not mission:
        raise AgentError("mission 不能为空")
    runtime = (runtime or "").strip() or cfg.default_runtime
    if runtime not in ("systemd", "docker"):
        raise AgentError(f"runtime 只能是 systemd 或 docker：{runtime!r}")
    _validate_brains(brain_list)
    src_ver = _resolve_source_choice((source_version or "").strip())
    # docker 前置早校验（在持锁/解压之前失败——零副作用）：
    # ① 内核版本门槛：Dockerfile 自 v2.7.19 起才有（源仓库旧版解压不出它）
    # ② 本机 docker 环境可用（daemon + compose 插件；权限不足给可行动提示）
    if runtime == "docker" and not versions.at_least(src_ver, "2.7.19"):
        raise AgentError(
            f"容器运行时需要 xuseek-v2 ≥ v2.7.19（当前 {src_ver}）——"
            f"升级内核版本或改用 systemd 运行时")
    if runtime == "docker":
        ok, hint = dockerctl.docker_available()
        if not ok:
            raise AgentError(f"docker 不可用：{hint}")
    # roots 校验在持端口锁之前——失败零副作用（版本门槛/条目形状，见 _validate_roots）
    roots_norm = _validate_roots(roots, src_ver)

    agent_id = gen_id()
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
            "roots": roots_norm,
            "runtime": runtime,
            "created_at": registry.now_iso(),
            "updated_at": registry.now_iso(),
        }

        def _fail(e: Exception) -> None:
            """回滚 + 统一报错——锁内落盘失败与锁外拉起失败同一条收尾。"""
            _rollback_create(rec)
            raise AgentError(f"创建失败已回滚：{e}") from e

        try:
            _init_workspace(rec, src_ver, roots_norm, extra_config)
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
        if _rt(rec).unit_state(unit) == "active":
            try:
                wait_health(rec["port"], rec["id"], rt=_rt(rec))
            except Exception:
                _fail(e)
        else:
            _fail(e)

    audit("agent.create", agent=agent_id, name=rec["name"], port=port,
          expose=expose, brains=brain_list, source=src_ver,
          source_defaulted=not (source_version or "").strip(),
          roots=len(roots_norm), runtime=runtime)
    return get_agent_or_404(agent_id)


def _init_workspace(rec: dict, src_ver: str, roots: list | None = None,
                    extra_config: str = "") -> None:
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
                              source_version=src_ver, instance_id=rec["id"],
                              roots=roots, extra_config=extra_config)


def spawn_and_verify(rec: dict) -> None:
    """systemd 拉起 + 端口验收。失败抛 AgentError。

    公开给 backup.restore 复用（create 的私有实现提级——恢复与创建走同一条
    拉起路径，别再各自 systemdctl.spawn_agent）。"""
    _spawn_unit(rec)
    wait_health(rec["port"], rec["id"], rt=_rt(rec))


def wait_health(port: int, agent_id: str, timeout: float = 90.0, *, rt=None) -> None:
    """启动验收：进程载体 active（systemd 单元 / docker 容器，按 runtime 分派）
    且端口已进入监听（ss；host 网络下容器监听同样出现在宿主 ss 表）。失败抛
    AgentError（附日志尾部）。

    HTTP /v1/health 探活已取消（xusi 与 agent 只剩邮箱通道）——内核 serve 先
    跑 preflight（config 缺失则写模板、同档无可用大脑则退出）才起 uvicorn：
    端口进入监听 = preflight 已通过；preflight 失败时端口不会绑定，错误经
    日志尾部暴露。绑端口之后的崩溃由 Restart=always / unless-stopped 兜底。

    主机缺 ss 时降级为 loopback connect 试探——ss 缺失会让
    _kernel_listening_ports 恒空集，健康 agent 也会验收超时（被误销毁）。

    rt：Runtime 模块；调用方几乎都已有 agent dict（_rt(agent) 即得），传入
    免多读一次注册表。None 时回退注册表查询（兼容旧调用）。"""
    if rt is None:
        rt = _rt(registry.get_agent(agent_id) or {})
    unit = get_config().unit_name(agent_id)
    have_ss = shutil.which("ss") is not None
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if rt.unit_state(unit) != "active":
            time.sleep(0.6)
            continue
        listening = (port in ports._kernel_listening_ports()
                     if have_ss else _loopback_listening(port))
        if listening:
            return
        time.sleep(0.6)
    log = rt.journal_tail(unit, 20)
    raise AgentError(f"agent 启动后 {timeout:.0f}s 未通过验收"
                     f"（进程载体未 active 或端口 {port} 未监听）。日志尾部：\n{log}")


def _loopback_listening(port: int) -> bool:
    """缺 ss 时的端口验收降级：TCP connect 试探 127.0.0.1（agent 监听
    0.0.0.0 或 127.0.0.1 均可达）。"""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def _rollback_create(agent: dict) -> None:
    """create 失败收尾：停进程载体、挪 home 进 .trash、注销、记审计。

    注销是硬要求——失败必须冒泡（注册表留 desired=running 的僵尸会被
    reconcile 反复拉起一个起不来的载体）；挪 home 是尽力而为，挪不动就
    原地留着（注销之后 reconcile 看不见它，孤儿目录交管理员清）。
    docker 载体追加清理 compose 渲染目录（镜像保留，prune 交管理员）。"""
    unit = _unit(agent)
    rt = _rt(agent)
    for fn in (rt.stop, rt.reset_failed):
        try:
            fn(unit)
        except Exception:
            pass
    if rt is dockerctl:
        try:
            dockerctl.cleanup(unit)
        except Exception:
            pass
    home = _home(agent)
    if home.exists():
        try:
            dest = get_config().trash_dir / f"{agent['id']}-{uuid.uuid4().hex[:6]}"
            shutil.move(str(home), str(dest))
        except Exception:
            pass
    registry.remove_agent(agent["id"])
    audit("agent.create.rollback", agent=agent["id"])


def get_agent_or_404(agent_id: str) -> dict:
    a = registry.get_agent(agent_id)
    if not a:
        raise AgentError(f"agent 不存在: {agent_id}")
    return a


def start(agent_id: str) -> dict:
    agent = get_agent_or_404(agent_id)
    if _rt(agent).unit_state(_unit(agent)) != "active":
        _spawn_unit(agent)
        wait_health(agent["port"], agent_id, rt=_rt(agent))
    return _finalize(agent_id, "running", "start")


def stop(agent_id: str) -> dict:
    """优雅停（SIGTERM → xuseek 轮边界落盘；TimeoutStopSec 兜底）。
    冻结进程收不到 SIGTERM——先探主进程实况（/proc T 态），SIGSTOP 中先
    SIGCONT 解冻再停（否则拖到 SIGKILL、丢会话）。覆盖 desired=paused 与
    「冻结孤儿」（registry 说 running、进程实际被冻）两种态。"""
    agent = get_agent_or_404(agent_id)
    unit = _unit(agent)
    rt = _rt(agent)
    if rt.main_stopped(unit):
        try:
            rt.kill_signal(unit, "SIGCONT")
        except (systemdctl.SystemdError, dockerctl.DockerError):
            pass  # 载体已不在也无妨，交给下面的幂等停止
    try:
        rt.stop(unit)
    except (systemdctl.SystemdError, dockerctl.DockerError) as e:
        # 载体已消失（曾经 stop 过）视为成功
        if rt.unit_state(unit) != "not-found":
            raise AgentError(str(e))
    return _finalize(agent_id, "stopped", "stop")


def pause(agent_id: str) -> dict:
    """冻结（SIGSTOP）：进程驻留、端口无响应、呼吸暂停。"""
    agent = get_agent_or_404(agent_id)
    unit = _unit(agent)
    rt = _rt(agent)
    if rt.unit_state(unit) != "active":
        raise AgentError("agent 未在运行，无法暂停（先 start）")
    rt.kill_signal(unit, "SIGSTOP")
    return _finalize(agent_id, "paused", "pause")


def resume(agent_id: str) -> dict:
    agent = get_agent_or_404(agent_id)
    unit = _unit(agent)
    rt = _rt(agent)
    if rt.unit_state(unit) != "active":
        raise AgentError("agent 未在运行（先 start）")
    rt.kill_signal(unit, "SIGCONT")
    return _finalize(agent_id, "running", "resume")


def restart(agent_id: str) -> dict:
    """优雅重启：SIGTERM 落盘 → 重新拉起 → 端口验收。冻结态先解冻（同 stop——
    冻结进程收不到 SIGTERM，硬 restart 会拖到 SIGKILL、丢会话）。"""
    agent = get_agent_or_404(agent_id)
    unit = _unit(agent)
    rt = _rt(agent)
    if rt.unit_state(unit) == "active":
        if rt.main_stopped(unit):
            try:
                rt.kill_signal(unit, "SIGCONT")
            except (systemdctl.SystemdError, dockerctl.DockerError):
                pass
        rt.restart(unit)
    else:
        _spawn_unit(agent)
    wait_health(agent["port"], agent_id, rt=_rt(agent))
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
    rt = _rt(agent)
    state = rt.unit_state(unit)
    if state in ("active", "activating"):
        raise AgentError("agent 正在运行（暂停也算运行），不允许删除。请先点「停止」，再删除——两步操作防误删")
    stop(agent_id)
    try:
        rt.reset_failed(unit)
    except Exception:
        pass
    home = _home(agent)
    dest = None
    if home.exists():
        dest = get_config().trash_dir / f"{agent_id}-{uuid.uuid4().hex[:6]}"
        shutil.move(str(home), str(dest))
    if rt is dockerctl:
        try:
            dockerctl.cleanup(unit)   # 渲染目录清掉（镜像保留，prune 交管理员）
        except Exception:
            pass
    registry.remove_agent(agent_id)
    audit("agent.delete", agent=agent_id, port=agent["port"], trash=str(dest),
          runtime=agent.get("runtime") or "systemd")
    return {"id": agent_id, "deleted": True, "moved_to": str(dest)}


# ── 改参 ─────────────────────────────────────────────────────────────

# 可改字段 = 簿记层（name/note）+ 进程层（expose）+ 大脑（brains，
# 手术式重渲染 config.toml 的 [brain] + [brains.*] 段，下次呼吸生效）。
# port 创建后固定——agent 对外联络 = ip+port，改端口等于换地址，断的是
# 已建立的互联与观测台入口；要换端口只能删了重建（或克隆到新端口）。
# mission/budgets 在创建后归 agent 自治——改它们请投信让 agent 自己
# 修改自己的 config.toml（内核每轮热重载）。
_PATCHABLE = {"name", "note", "expose", "brains", "runtime"}

_AGENT_OWNED = {
    "mission": "使命已由 agent 自治：请投信让它自己修改 config.toml（内核每轮热重载）",
    "budgets": "预算已由 agent 自治：请投信让它自己修改 config.toml 的 [limits] 段（v2.7.5+；旧内核为 [agent] 段）",
}

_IMMUTABLE = {
    "port": "端口创建后固定（agent 对外联络 = ip+port，改端口等于换地址）",
}


def patch_agent(agent_id: str, changes: dict, *, apply_restart: bool = False) -> dict:
    """改参。name/note 写注册表即生效；expose 改的是进程监听参数，
    返回 restart_required，?apply=restart 立即执行；brains 手术式重渲染
    config.toml 大脑段（下次呼吸生效，不重启），返回 brains_effective。
    runtime 切换进程载体（systemd/docker），须停止态、切换后不自动启动。"""
    agent = get_agent_or_404(agent_id)
    bad = set(changes) - _PATCHABLE
    owned = sorted(b for b in bad if b in _AGENT_OWNED)
    if owned:
        raise AgentError(
            f"这些字段已由 agent 自治：{', '.join(owned)}。"
            f"{_AGENT_OWNED[owned[0]]}")
    fixed = sorted(b for b in bad if b in _IMMUTABLE)
    if fixed:
        raise AgentError(
            f"这些字段创建后不可改：{', '.join(fixed)}。{_IMMUTABLE[fixed[0]]}")
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

    # runtime 切换：换进程载体（状态全在实例目录，只换载体）。
    # 门控：必须停止态——运行中切换会让新旧两个载体抢同一端口；
    # 切换后不自动启动（用户流程：停止 → 改参切运行时 → 启动）。
    runtime_new = None
    if "runtime" in changes:
        rt_new = str(changes["runtime"] or "").strip()
        if rt_new not in ("systemd", "docker"):
            raise AgentError(f"runtime 只能是 systemd 或 docker：{rt_new!r}")
        cur = agent.get("runtime") or "systemd"
        if rt_new != cur:
            unit = _unit(agent)
            rt = _rt(agent)
            state = rt.unit_state(unit)
            if state in ("active", "activating") or agent.get("desired_state") != "stopped":
                raise AgentError(
                    "切换运行时须先停止 agent：停止 → 改参切运行时 → 启动"
                    f"（当前状态：{state} / {agent.get('desired_state')}）")
            if rt_new == "docker":
                ok, hint = dockerctl.docker_available()
                if not ok:
                    raise AgentError(f"docker 不可用：{hint}")
                if not (Path(_home(agent)) / versions.SRC_DIR_NAME / "Dockerfile").is_file():
                    raise AgentError(
                        "该内核版本不含 Dockerfile：容器运行时需 xuseek-v2 ≥ v2.7.19，"
                        "升级内核走 docs/kernel-upgrade.md")
            # 旧载体防御性清理（幂等）：docker → compose down 回收容器防残留
            # 占端口；涉 docker 一侧顺手清 compose 渲染目录（spawn 会重渲染）
            try:
                rt.stop(unit)
            except (systemdctl.SystemdError, dockerctl.DockerError):
                pass
            if rt is dockerctl or rt_new == "docker":
                try:
                    dockerctl.cleanup(unit)
                except Exception:
                    pass
            runtime_new = rt_new

    hot = {}       # 写注册表即生效
    need_restart = False

    if "name" in changes:
        hot["name"] = str(changes["name"]).strip() or agent["name"]
    if "note" in changes:
        hot["note"] = str(changes["note"])
    # expose 是剩下的进程层可改参数（port 已固定，见 _IMMUTABLE）
    if "expose" in changes and bool(changes["expose"]) != bool(agent.get("expose")):
        hot["expose"] = bool(changes["expose"])
        need_restart = True
    if runtime_new is not None:
        hot["runtime"] = runtime_new

    next_rec = {**agent, **hot}
    if hot:
        registry.update_agent(agent_id, hot)

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
    """换监听 host 的重启（expose 开关；port 创建后固定）：stop 旧载体 →
    重新拉起（docker 侧 down 旧容器，spawn 重渲染含新 --host）。冻结态先
    解冻（同 stop——冻结进程收不到 SIGTERM）。"""
    unit = _unit(agent)
    rt = _rt(agent)
    if rt.unit_state(unit) == "active":
        if rt.main_stopped(unit):
            try:
                rt.kill_signal(unit, "SIGCONT")
            except (systemdctl.SystemdError, dockerctl.DockerError):
                pass
        rt.stop(unit)
    _spawn_unit(agent)
    wait_health(agent["port"], agent["id"], rt=_rt(agent))


# ── 状态（systemd + 注册表；只读观察另见 observe）────────────────────

def status(agent_id: str) -> dict:
    """状态聚合：注册表 + systemd 单元 + 内核呼吸状态（只读观察）。"""
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
        "roots": agent.get("roots", []),
        "desired_state": agent.get("desired_state", "running"),
        "runtime": agent.get("runtime") or "systemd",
        "listen_host": _listen_host(agent),
        "created_at": agent.get("created_at"),
        "updated_at": agent.get("updated_at"),
        "fetched_at": _iso(),
    }
    out["process"] = _rt(agent).unit_brief(_unit(agent))
    out["daemon"] = _daemon_probe(agent)
    return out


def _daemon_probe(agent: dict) -> dict | None:
    """内核自报呼吸状态（/v1/status 的 daemon 段，只读观察）。

    任何失败 → None——卡片/详情回退「进程存活」语义，不因观察不可达报错
    （观察失败的原因已由事件 tab 的错误路径各自呈现）。"""
    try:
        st = observe(agent["id"], "status")
    except Exception:
        return None
    d = (st or {}).get("daemon") if isinstance(st, dict) else None
    return d if isinstance(d, dict) else None


def logs(agent_id: str, n: int = 200) -> dict:
    agent = get_agent_or_404(agent_id)
    n = max(1, min(int(n), 1000))
    text = _rt(agent).journal_tail(_unit(agent), n)
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


def ui_url(agent_id: str) -> dict:
    """观测台直连入口：<host>:<port>/ui/?token=<观察 token>。

    不再走管理面反代（/px 已删）——浏览器直连 agent 端口；token 复用
    observe 的自动签发（缺失写 data/webui_tokens.json，内核每请求重读）。
    host 由前端按浏览器视角拼（location.hostname）——管理面可能被远程
    浏览器访问；agent 未运行 → active=false（前端置灰）。"""
    agent = get_agent_or_404(agent_id)
    tok = _observe_token(agent)
    return {
        "id": agent_id,
        "port": agent["port"],
        "token": tok,
        "expose": bool(agent.get("expose")),
        "active": _rt(agent).unit_state(_unit(agent)) == "active",
    }


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
    rt = _rt(agent)
    if rt.unit_state(unit) != "active":
        raise AgentError("agent 未在运行")
    if rt.main_stopped(unit):
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
    """读邮箱文件尾部 N 行（只读展示，无后台处理）。

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
        rt = _rt(agent)
        state = rt.unit_state(unit)
        action = "none"
        err = None
        try:
            if desired == "running":
                if state != "active":
                    if state == "not-found" or state == "inactive" or state == "failed":
                        _spawn_unit(agent)
                        wait_health(agent["port"], agent["id"], timeout=60, rt=rt)
                        action = "respawned"
                    # state == "unknown"（docker daemon 挂掉等）：spawn 会抛
                    # DockerError → 记入 report.error，不动注册表，等 daemon
                    # 恢复后容器 unless-stopped 自动回活
                elif rt.main_stopped(unit):
                    # 冻结孤儿：manager 在备份冻结窗 / pause 中途崩掉留下的态
                    # （期望 running 却被 SIGSTOP——载体层 state 仍 active，
                    # 单纯比对期望态发现不了）。SIGCONT 幂等，恢复呼吸。
                    rt.kill_signal(unit, "SIGCONT")
                    action = "sigcont-rescue"
            elif desired == "paused":
                if state != "active":
                    _spawn_unit(agent)
                    wait_health(agent["port"], agent["id"], timeout=60, rt=rt)
                    action = "respawned"
                rt.kill_signal(unit, "SIGSTOP")
                action = (action + "+sigstop") if action != "none" else "sigstop"
            elif desired == "stopped":
                if state == "active":
                    rt.stop(unit)
                    action = "stopped"
        except Exception as e:
            err = str(e)
        report.append({"id": agent["id"], "desired": desired, "was": state,
                       "runtime": agent.get("runtime") or "systemd",
                       "action": action, "error": err})
    if any(r["action"] != "none" for r in report):
        audit("reconcile", report=report)
    return report


def list_status() -> list[dict]:
    ids = [a["id"] for a in registry.list_agents()]
    if not ids:
        return []
    # 每 agent 一次 systemd 子进程 + 一次只读观察 HTTP（最长 6s 超时）——
    # 串行会把看板 15s 轮询拖成 N×6s，小线程池并行
    with ThreadPoolExecutor(max_workers=min(8, len(ids))) as ex:
        return list(ex.map(status, ids))
