"""能力包（capability packs）—— 墟司侧只读观察。

分工裁决（proposal-amem 讨论后收敛）：**墟司只负责种子，剩下的事情交给智能体
自己做**——种子由内核 init 无条件播入 workspace（墟司连播种动作都不需要做）；
启用与否（register_skill）、依赖安装（run_shell 后台 pip）、一切使用决策归大脑。
墟司不写 [capabilities]、不碰 pip、不重启 agent——本模块只回答「它世界里有什么
能力包、当前开关实况」供观察台展示。

清单走内核公开 CLI `xuseek.sh capabilities list --json`（契约三）——零硬编码、
无版本知识：问哪份源码，得到哪份答案。按版本维护**探测副本**
（instances/.probe/<版本>/）：解压一次、venv 一次，之后查询秒回；结果按
(版本, zip mtime, size) 内存缓存。同版本首查按版本加锁串行，防并发在一份
副本上跑 xuseek.sh 建 venv（pip 无锁会互踩）。

存量 agent 的清单**不跑子进程**：包元数据来自其版本探测缓存（私有副本本就是
从同一 zip 解压的，能力包目录相同），enabled 直读它 config 的 [capabilities]
段（与内核 CLI 读的是同一个文件；该段通常不存在=全 false，若大脑自行写入亦
如实显示）——快（详情页秒开），也避开装依赖期间并发跑 CLI 撞 pip 的竞态。
"""
from __future__ import annotations

import json
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any

from . import versions
from .config import get_config

# 首查可能要建 venv + 装主依赖（extras 不装：探测副本无 [capabilities]）
_PROBE_TIMEOUT = 300

_cache: dict[tuple, list[dict]] = {}
_main_cache: tuple[int, list[dict]] | None = None       # (xuseek.sh mtime, packs)
_extract_lock = threading.Lock()                        # 探测副本解压互斥
_ver_locks: dict[str, threading.Lock] = {}              # 同版本首查（建venv+CLI）串行
_ver_locks_guard = threading.Lock()


class CapabilityError(RuntimeError):
    """能力包业务错误（用户可读，API 层转 400）。"""


def probe_root() -> Path:
    return get_config().instances_dir / ".probe"


def _zip_stamp(version: str) -> tuple[int, int]:
    st = versions.zip_for(version).stat()
    return (int(st.st_mtime), int(st.st_size))


def _ver_lock(version: str) -> threading.Lock:
    with _ver_locks_guard:
        return _ver_locks.setdefault(version, threading.Lock())


def _ensure_probe(version: str) -> Path:
    """确保该版本的探测副本就位且与 zip 同版（zip 被重新投放则重建）。"""
    d = probe_root() / version
    stamp = _zip_stamp(version)
    want = f"{stamp[0]} {stamp[1]}"
    marker = d / ".probe_stamp"
    if (d / "xuseek.sh").exists() and marker.exists():
        try:
            if marker.read_text(encoding="utf-8").strip() == want:
                return d
        except Exception:
            pass
    with _extract_lock:
        if not (d / "xuseek.sh").exists() or not marker.exists() \
                or marker.read_text(encoding="utf-8").strip() != want:
            shutil.rmtree(d, ignore_errors=True)
            versions.extract(version, d)
            (d / ".probe_stamp").write_text(want, encoding="utf-8")
    return d


def _run_list(sh: Path, home: Path | None = None) -> tuple[int, str, str]:
    cmd = [str(sh)]
    if home is not None:
        cmd += ["--home", str(home)]
    cmd += ["capabilities", "list", "--json"]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=_PROBE_TIMEOUT)
    return r.returncode, r.stdout, r.stderr


def _parse_json(stdout: str, stderr: str = "") -> list[dict]:
    """解析 CLI 的 JSON 输出。启动器自愈（建 venv/装依赖）的进度行可能混在
    stdout 前后（部分版本启动器的 echo 未重定向 stderr——首查必带、之后干净），
    先整段解析，失败则切最外层 [ … ] 再试；切不出才算契约破坏。"""
    text = stdout.strip()
    if not text:
        last = stderr.strip().splitlines()
        raise CapabilityError(
            "capabilities list --json 无输出" + (f"：{last[-1]}" if last else ""))
    try:
        data = json.loads(text)
    except ValueError:
        data = None
        dec = json.JSONDecoder()
        for i in (k for k, c in enumerate(text) if c == "["):   # 从每个 [ 起试解析
            try:
                obj, _ = dec.raw_decode(text, i)
            except ValueError:
                continue
            if isinstance(obj, list):
                data = obj                                      # 首个合法数组 = 内核输出
                break
        if data is None:
            head = text.splitlines()[0][:80]
            raise CapabilityError(
                f"capabilities list --json 输出无法解析（内核契约变化？）：{head}") from None
    if not isinstance(data, list):
        raise CapabilityError("capabilities list --json 输出不是列表（内核契约变化？）")
    return [dict(p) for p in data if isinstance(p, dict)]


def _unsupported(err: str) -> bool:
    low = (err or "").lower()
    return "invalid choice" in low or "unknown command" in low or "unrecognized" in low


def list_for_source(source_version: str = "") -> dict:
    """问某份源码有哪些能力包（观察用；list_for_agent 的内部依赖）。

    source_version：版本号 → 该版本探测副本；"" → 版本仓库最新；"main" →
    共享主源码。旧版本不认识该子命令 → capabilities 为空并附 note。
    """
    from . import agentops   # 懒加载：agentops 也引用本模块，避免环导

    ver = (source_version or "").strip()
    if ver and ver != agentops.MAIN_SOURCE:
        versions.zip_for(ver)          # 提前校验，非法/不存在 → VersionError(400)
    else:
        avail = versions.list_versions()
        if ver != agentops.MAIN_SOURCE and avail:
            ver = avail[0]["version"]
        else:
            ver = ""                    # 共享主源码（或仓库为空回落）

    if not ver:
        return {"source_version": "", "capabilities": _packs_of_main()}

    stamp = _zip_stamp(ver)
    key = (ver, stamp)
    hit = _cache.get(key)
    if hit is not None:
        return {"source_version": ver, "capabilities": hit}
    note = None
    with _ver_lock(ver):
        hit = _cache.get(key)           # 双检：排队等锁期间别人已查好
        if hit is None:
            sh = _ensure_probe(ver) / "xuseek.sh"
            rc, out, err = _run_list(sh)
            if rc != 0:
                if _unsupported(err or out):
                    hit, note = [], "该版本内核早于能力包架构（无能力包）"
                else:
                    last = (err or out).strip().splitlines()
                    raise CapabilityError(
                        f"查询版本 {ver} 的能力包失败：{last[-1] if last else '未知错误'}")
            else:
                hit = _parse_json(out, err)
            _cache[key] = hit
    out_d: dict[str, Any] = {"source_version": ver, "capabilities": hit}
    if note:
        out_d["note"] = note
    return out_d


def _packs_of_main() -> list[dict]:
    """共享主源码的能力包清单（按 xuseek.sh mtime 进程内缓存；venv 已在被
    spawn 用着，查询只是指纹校验 + CLI，无安装动作）。"""
    global _main_cache
    from . import agentops
    try:
        sh = agentops.ensure_source() / "xuseek.sh"
    except agentops.AgentError as e:
        raise CapabilityError(str(e)) from None
    mtime = int(sh.stat().st_mtime)
    if _main_cache and _main_cache[0] == mtime:
        return _main_cache[1]
    rc, out, err = _run_list(sh)
    packs = [] if rc != 0 else _parse_json(out, err)
    _main_cache = (mtime, packs)
    return packs


def list_for_agent(agent: dict) -> dict:
    """存量 agent 的能力包清单（enabled = 其 config [capabilities] 的真实状态）。

    不跑子进程：包元数据来自其版本的探测缓存（私有副本与探测副本出自同一 zip，
    能力包目录相同）；enabled 直读 config 文件（与内核 CLI 读的是同一文件）。
    共享主源码的旧 agent：问一次主源码（缓存），无能力包子命令则优雅降级。
    """
    from . import brains

    ver = str(agent.get("source_version") or "").strip()
    try:
        if ver:
            packs = [dict(p) for p in list_for_source(ver).get("capabilities", [])]
        else:
            packs = [dict(p) for p in _packs_of_main()]
    except CapabilityError:
        raise
    caps = brains.read_capabilities(get_config().instance_home(agent["id"]))
    known = set()
    for p in packs:
        known.add(p.get("name"))
        p["enabled"] = bool(caps.get(str(p.get("name"))))
    out: dict[str, Any] = {"capabilities": packs}
    unknown = [c for c in caps if c not in known]
    if unknown:
        out["note"] = f"config 开启了该源码版本没有的能力包：{'、'.join(sorted(unknown))}（忽略）"
    if not packs and not unknown:
        out["note"] = "该源码版本无能力包"
    return out
