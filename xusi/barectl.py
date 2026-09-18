"""bare 运行时操控：容器直跑（机器本身就是容器——AutoDL 等无 systemd、
不可嵌套 docker 的宿主；不在容器里再起容器）。

进程载体 = setsid supervisor 循环（内核退出 5s 后自动拉起，近似
Restart=always）+ pidfile + 日志文件：

- ssh 断开会话不死（setsid + 重定向）
- 生命周期信号按进程组广播：SIGSTOP/SIGCONT = 暂停/恢复，SIGTERM = 优雅停
- 实例重启后进程归零、需手动 spawn-all——与零管理机开机自愈缺口同类，接受

Runtime 协议与 systemdctl/dockerctl 形状对齐（spawn/state/brief/stop/
restart/main_stopped/kill_signal/reset_failed/manager_running/journal_tail）。
"""
from __future__ import annotations

import json
import os
import shlex
import signal
import subprocess
from pathlib import Path
from typing import Any

from .config import get_config
from .systemdctl import DEFAULT_UV_INDEX_URL

_RESTART_GAP_S = 5  # 内核退出 → 拉起间隔（近似 RestartSec=5）

_PID_FILE = "agent.pid"
_RUN_FILE = "agent.run.json"   # spawn 参数（restart 重放用）
_LOG_FILE = "agent.out"


class BareError(RuntimeError):
    pass


def _paths(unit: str) -> tuple[Path, Path, Path]:
    """unit → (pidfile, runfile, logfile)。unit 形如 xusi-a-<agent_id>，
    home = instances/<agent_id>/data/（实例自洽：随 home 一起迁移）。"""
    agent_id = unit[7:] if unit.startswith("xusi-a-") else unit
    data = get_config().instance_home(agent_id) / "data"
    return data / _PID_FILE, data / _RUN_FILE, data / _LOG_FILE


def _read_pid(unit: str) -> int | None:
    try:
        pid = int(_paths(unit)[0].read_text().strip())
    except Exception:
        return None
    return pid or None


def _alive(pid: int) -> bool:
    """进程存活 + 确实是我们拉起的（cmdline 含 xuseek，防 pid 复用误判）。"""
    try:
        os.kill(pid, 0)
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return b"xuseek" in f.read()
    except Exception:
        return False


def _run(cmd: str, timeout: float = 30) -> str:
    try:
        r = subprocess.run(["sh", "-c", cmd], capture_output=True, text=True,
                           timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise BareError(f"命令超时：{cmd[:60]} …（{timeout}s）") from e
    if r.returncode != 0:
        err = (r.stderr or r.stdout or "").strip().splitlines()
        raise BareError(err[-1] if err else f"exit {r.returncode}: {cmd[:60]}")
    return r.stdout


def spawn_agent(unit: str, source_dir: str, home: str, host: str, port: int, *,
                version: str = "") -> None:
    """setsid supervisor 循环拉起内核（version 与 systemdctl 同：协议占位，忽略）。

    环境注入 UV/PIP 镜像（与 systemdctl 同源），首启 venv 装包秒级。
    """
    if unit_state(unit) == "active":
        raise BareError(f"单元 {unit} 已在运行")
    pidf, runf, logf = _paths(unit)
    pidf.parent.mkdir(parents=True, exist_ok=True)
    runf.write_text(json.dumps({"source_dir": source_dir, "home": home,
                                "host": host, "port": port}, ensure_ascii=False))
    url = os.environ.get("XUSI_UV_INDEX_URL", DEFAULT_UV_INDEX_URL)
    inner = (f"export UV_INDEX_URL={shlex.quote(url)} PIP_INDEX_URL={shlex.quote(url)}; "
             f"while true; do "
             f"{shlex.quote(str(Path(source_dir) / 'xuseek.sh'))} --home {shlex.quote(home)} "
             f"serve --host {host} --port {port}; sleep {_RESTART_GAP_S}; done")
    _run(f"setsid nohup sh -c {shlex.quote(inner)} >> {shlex.quote(str(logf))} 2>&1 & "
         f"echo $! > {shlex.quote(str(pidf))}")


def unit_state(unit: str) -> str:
    """active / inactive / not-found（pidfile 有无 + 进程存活，pid 复用已防）。"""
    pid = _read_pid(unit)
    if pid is None:
        return "not-found"
    return "active" if _alive(pid) else "inactive"


def unit_brief(unit: str) -> dict[str, Any]:
    """单元摘要（字段形状对齐 systemdctl：active/sub/main_pid/auto_restarts/
    last_exit_status/active_since）。"""
    try:
        pid = _read_pid(unit)
        state = unit_state(unit)
        active_since = None
        pidf = _paths(unit)[0]
        if pidf.exists():
            from datetime import datetime, timezone
            active_since = datetime.fromtimestamp(pidf.stat().st_mtime,
                                                  tz=timezone.utc).isoformat()
        return {"active": state, "sub": "", "main_pid": pid,
                "auto_restarts": 0, "last_exit_status": None,
                "active_since": active_since}
    except Exception as e:
        return {"active": "not-found", "sub": "", "main_pid": None,
                "auto_restarts": 0, "last_exit_status": None,
                "active_since": None, "error": str(e)}


def unit_load_state(unit: str) -> str:
    return "loaded" if _paths(unit)[0].exists() else "not-found"


def stop(unit: str) -> None:
    """优雅停：进程组 SIGTERM（内核 10s 停窗落盘）→ 15s 未退 SIGKILL → 清 pidfile。"""
    pid = _read_pid(unit)
    if pid is None:
        return
    if _alive(pid):
        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        for _ in range(15):
            if not _alive(pid):
                break
            import time
            time.sleep(1)
        if _alive(pid):
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    try:
        _paths(unit)[0].unlink(missing_ok=True)
    except Exception:
        pass


def restart(unit: str) -> None:
    """stop + 按 spawn 参数重放（老实例无 run 参数 → 报错，走 stop → start 路径）。"""
    runf = _paths(unit)[1]
    if not runf.exists():
        raise BareError("无 spawn 参数（agent.run.json 缺失）——请 stop 后手动 start")
    params = json.loads(runf.read_text())
    stop(unit)
    spawn_agent(unit, params["source_dir"], params["home"],
                params["host"], params["port"])


def main_stopped(unit: str) -> bool:
    """主进程是否 T 态（暂停/备份冻结窗判据——进程组 SIGSTOP 后组长转 T）。"""
    pid = _read_pid(unit)
    if pid is None:
        return False
    try:
        stat = Path(f"/proc/{pid}/stat").read_text().split()
        return len(stat) > 2 and stat[2] == "T"
    except Exception:
        return False


def kill_signal(unit: str, sig: str) -> None:
    """信号广播到整个进程组（SIGSTOP 暂停 / SIGCONT 恢复）。"""
    pid = _read_pid(unit)
    if pid is None or not _alive(pid):
        raise BareError(f"单元 {unit} 未运行")
    try:
        os.killpg(pid, getattr(signal, sig))
    except ProcessLookupError:
        pass


def reset_failed(unit: str) -> None:
    pass  # 无失败态持久化


def manager_running() -> bool:
    return True  # bare 无管理器——恒可用


def journal_tail(unit: str, n: int = 200) -> str:
    try:
        lines = _paths(unit)[2].read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return ""
    return "\n".join(lines[-n:])
