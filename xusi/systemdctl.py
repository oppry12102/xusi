"""systemd 用户单元操控：agent 的进程生命周期全部交给 systemd —— 掉线保护 =
Restart=always，掉电/崩溃/误杀自动拉起，manager 重启后按期望态 reconcile。

只做子进程封装，不含业务。所有调用带超时，失败抛 SystemdError（带 stderr 摘要）。
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

MANAGER_UNIT = "xusi.service"

# 默认 PyPI 镜像：本机直连 pypi.org 不可达（DNS 通但 TCP/TLS 握手挂死），
# xuseek.sh 首次 serve 自愈安装依赖时会卡在网络层分钟级。spawn 时经
# systemd-run --setenv 注入——保证新建 agent 的 venv 装包秒级完成。
# 覆盖：env XUSI_UV_INDEX_URL（也同步设 PIP_INDEX_URL 兜底 pip 回落路径）。
# 设空串 = 关镜像，回退到 pypi.org（不保证可达）。
DEFAULT_UV_INDEX_URL = "https://pypi.tuna.tsinghua.edu.cn/simple"


class SystemdError(RuntimeError):
    pass


def _run(cmd: list[str], timeout: float = 30) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise SystemdError(f"命令超时：{' '.join(cmd[:3])} …（{e.timeout}s）") from e
    if r.returncode != 0:
        err = (r.stderr or r.stdout or "").strip().splitlines()
        raise SystemdError(err[-1] if err else f"exit {r.returncode}: {' '.join(cmd[:4])}")
    return r.stdout


def spawn_agent(unit: str, source_dir: str, home: str, host: str, port: int, *,
                version: str = "") -> None:
    """以瞬态单元拉起一个 agent（Restart=always → 崩溃自动重启）。

    version：Runtime 协议对称占位（dockerctl 用它拼镜像 tag）——systemd 不用
    镜像，忽略之。
    TimeoutStopSec=20 > xuseek daemon 的 10s 优雅停窗，保证轮边界落盘后再退。
    PyPI 镜像经 --setenv 注入（xuseek.sh 首启自愈装依赖走它，见 DEFAULT_UV_INDEX_URL）。
    呼吸面看门狗：ensure_stall_timer 建立瞬态 timer（内核 stall_check.py 判
    breath.json 停滞 → 重启单元；与 docker 的 stall_watch kill 1 同语义）。
    """
    import os
    if unit_state(unit) == "active":
        raise SystemdError(f"单元 {unit} 已在运行")
    url = os.environ.get("XUSI_UV_INDEX_URL", DEFAULT_UV_INDEX_URL)
    cmd = [
        "systemd-run", "--user", "--collect",
        "--unit", unit,
        "-p", "Restart=always",
        "-p", "RestartSec=5",
        "-p", "TimeoutStopSec=20",
    ]
    if url:
        cmd += ["--setenv", f"UV_INDEX_URL={url}", "--setenv", f"PIP_INDEX_URL={url}"]
    cmd += [f"{source_dir}/xuseek.sh", "--home", home,
            "serve", "--host", host, "--port", str(port)]
    _run(cmd)
    ensure_stall_timer(unit, source_dir, home)


# 呼吸面看门狗 timer 的巡检间隔（秒）：阈值 stall_s 内的停滞最迟该间隔后被
# 发现并重启。5 分钟是权衡——太密浪费、太疏停滞窗口拉长。
_STALL_POLL_S = 300


def ensure_stall_timer(unit: str, source_dir: str, home: str) -> None:
    """呼吸面看门狗（systemd 形态）：瞬态 timer 每 5 分钟跑一次内核
    stall_check.py——判据 = data/breath.json 的 mtime 超阈且 serve 进程在跑，
    退出 1（停滞）时重启 agent 单元（借 Restart=always 起死回生，与 docker
    的 stall_watch kill 1 同语义）；退出 0 放行、退出 2（阈值坏）不动作。

    条件：[manager] stall_s > 0 且实例内核副本带 stall_check.py（内核地板
    ≥ 2.7.79 必有）。timer PartOf=agent 单元——停 agent 自动停 timer，
    start 时 spawn 幂等重建（load-state 判存）。建立失败不阻塞 spawn：
    看门狗是增强不是硬依赖（stderr 提示）。
    """
    import sys
    import shlex
    from .config import get_config
    thr = get_config().stall_s
    if thr <= 0:
        return
    script = Path(source_dir) / "xuseek" / "stall_check.py"
    if not script.is_file():
        return
    tunit = f"{unit}-stall"
    if unit_load_state(f"{tunit}.timer") == "loaded":
        return
    py = sys.executable
    # 只有退出 1（真停滞）才重启；0（无脉搏/无 serve）与 2（阈值坏）都不动
    inner = (f'rc=$({shlex.quote(py)} {shlex.quote(str(script))} '
             f'{shlex.quote(str(home))} {thr} 2>/dev/null); '
             f'[ "$rc" = 1 ] && systemctl --user restart {shlex.quote(unit)}')
    # PartOf 值须带 .service 后缀——systemd-run 对裸名（无类型后缀）报
    # 「Invalid unit name」并拒绝创建 timer（实测 systemd 255）
    cmd = ["systemd-run", "--user", "--collect",
           "--timer-property", f"PartOf={unit}.service",
           "--on-unit-active", str(_STALL_POLL_S),
           "--unit", tunit, "/bin/sh", "-c", inner]
    try:
        _run(cmd)
    except SystemdError as e:
        print(f"[xusi] 呼吸看门狗 timer 未建立（{unit}）：{e}")


def stall_timer_remove(unit: str) -> None:
    """删除呼吸看门狗 timer（delete/回滚收尾；stop 不清——timer 对停止态
    agent 恒放行（serve 不在 → stall_check 退出 0），且 PartOf 已随停联动）。"""
    for suffix in (".timer", ".service"):
        try:
            _run(["systemctl", "--user", "stop", unit + "-stall" + suffix], timeout=15)
        except SystemdError:
            pass
    try:
        _run(["systemctl", "--user", "reset-failed",
              unit + "-stall.timer", unit + "-stall.service"], timeout=15)
    except SystemdError:
        pass


def unit_state(unit: str) -> str:
    """active / inactive / failed / not-found / unknown。"""
    try:
        out = _run(["systemctl", "--user", "show", unit,
                    "-p", "ActiveState", "--value"], timeout=10).strip()
        return out or "unknown"
    except SystemdError:
        return "not-found"


def unit_brief(unit: str) -> dict[str, Any]:
    """单元摘要：状态、主 PID、自动重启次数、最近一次退出码。not-found 也返回结构。

    用 key=value 行解析（systemd 多属性 --value 的输出顺序不可靠，不能按下标取）。
    """
    try:
        out = _run(["systemctl", "--user", "show", unit,
                    "-p", "ActiveState", "-p", "SubState", "-p", "MainPID",
                    "-p", "NRestarts", "-p", "ExecMainStatus",
                    "-p", "ActiveEnterTimestamp"], timeout=10)
        props: dict[str, str] = {}
        for line in out.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                props[k] = v
        pid = props.get("MainPID", "")
        nres = props.get("NRestarts", "")
        st = props.get("ExecMainStatus", "")
        return {
            "active": props.get("ActiveState") or "not-found",
            "sub": props.get("SubState", ""),
            "main_pid": int(pid) if pid.isdigit() else None,
            "auto_restarts": int(nres) if nres.lstrip("-").isdigit() else 0,
            "last_exit_status": int(st) if st.lstrip("-").isdigit() else None,
            "active_since": props.get("ActiveEnterTimestamp") or None,
        }
    except SystemdError as e:
        return {"active": "not-found", "sub": "", "main_pid": None,
                "auto_restarts": 0, "last_exit_status": None, "active_since": None,
                "error": str(e)}


def unit_load_state(unit: str) -> str:
    """LoadState：loaded / not-found。瞬态单元停止并回收后为 not-found。"""
    try:
        out = _run(["systemctl", "--user", "show", unit, "-p", "LoadState", "--value"],
                   timeout=10)
        return out.strip() or "not-found"
    except SystemdError:
        return "not-found"


def stop(unit: str) -> None:
    """停止单元。已消失（not-found）视为成功——stop 语义本就幂等。"""
    if unit_load_state(unit) == "not-found":
        return
    try:
        _run(["systemctl", "--user", "stop", unit], timeout=40)
    except SystemdError as e:
        # 竞态：检查后单元恰好退出回收 → 同样视为已停止
        if "not loaded" in str(e).lower():
            return
        raise


def restart(unit: str) -> None:
    _run(["systemctl", "--user", "restart", unit], timeout=60)


def main_stopped(unit: str) -> bool:
    """单元主进程是否处于 SIGSTOP 冻结态（/proc State: T）。

    暂停（pause）与备份冻结窗都表现为主进程 T 态；systemd 层完全看不出
    （ActiveState 仍 active、SubState 仍 running），只有 /proc 说真话。
    manager 崩溃可能把 agent 永久留在这个态（见 reconcile 的 sigcont-rescue）。
    只读 /proc，不改任何状态。"""
    pid = unit_brief(unit).get("main_pid")
    if not pid:
        return False
    try:
        with open(f"/proc/{pid}/status", "rb") as f:
            for line in f:
                if line.startswith(b"State:"):
                    # 形如 "State:\tT (stopped)"；T=停止，t=跟踪停止
                    state = line.split(b":", 1)[1].strip().split()[0]
                    return state in (b"T", b"t")
    except (OSError, IndexError):
        return False
    return False


def kill_signal(unit: str, sig: str) -> None:
    """给单元主进程发信号（SIGSTOP/SIGCONT 用于暂停/续跑）。"""
    _run(["systemctl", "--user", "kill", unit, "--signal", sig, "--kill-who", "main"],
         timeout=15)


def reset_failed(unit: str) -> None:
    _run(["systemctl", "--user", "reset-failed", unit], timeout=10)


def manager_running() -> bool:
    return unit_state(MANAGER_UNIT) == "active"


def journal_tail(unit: str, n: int = 200) -> str:
    """单元最近 n 行日志（journald，agent 的 stdout/stderr 都在这里）。"""
    try:
        return _run(["journalctl", "--user", "-u", unit, "-n", str(n),
                     "--no-pager", "-o", "short"], timeout=15)
    except SystemdError as e:
        return f"（日志读取失败：{e}）"
