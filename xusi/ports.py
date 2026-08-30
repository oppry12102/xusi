"""端口盘点与分配：三重检验，宁严勿漏。

1. 注册表已占端口（含 manager 自己的 8601）；
2. 内核实际监听（ss -tlnH 解析，覆盖非本管理面起的进程）；
3. bind 试探（0.0.0.0 与 127.0.0.1 双试，防 ss 权限盲区）。

agent 启动后以「单元 active + 端口进入监听」验收（见 agentops.wait_health）。
"""
from __future__ import annotations

import socket
import subprocess
import threading

from . import registry
from .config import get_config

# 分配互斥：create / patch / restore 的「allocate → 注册表落盘」窗口必须持锁。
# 三重检验挡不住本进程内的 TOCTOU——create 的窗口隔着 init（分钟级），两个并发
# create 会拿到同一端口；内核监听检验要在进程真正起来后才看得见。
ALLOC_LOCK = threading.Lock()


def _kernel_listening_ports() -> set[int]:
    """ss -tlnH 抓内核里所有 TCP 监听端口（listen 状态）。"""
    out: set[int] = set()
    try:
        r = subprocess.run(["ss", "-tlnH"], capture_output=True, text=True, timeout=5)
        for line in r.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 4:
                try:
                    out.add(int(parts[3].rsplit(":", 1)[1].split("%")[0]))
                except (ValueError, IndexError):
                    continue
    except Exception:
        pass
    return out


def _can_bind(port: int, host: str) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
            s.bind((host, port))
        return True
    except OSError:
        return False


def port_free(port: int) -> bool:
    """三重检验某端口是否可用。"""
    cfg = get_config()
    if port == cfg.port:
        return False
    if port in registry.used_ports():
        return False
    if port in _kernel_listening_ports():
        return False
    return _can_bind(port, "0.0.0.0") and _can_bind(port, "127.0.0.1")


def port_host_state(port: int) -> str:
    """主机级端口三态（跳过注册表腿——doctor 查管理面端口用；分配层保留
    管理面端口，port_free 恒 False，其余场景一律走 port_free 别另抄检验）：

    - "free"：无内核监听、双 bind 试探通过（可直接起服务）
    - "listening"：内核已有进程监听（ss -tlnH 可见）
    - "blocked"：无可见监听但 bind 被拒（刚停止的 TIME_WAIT 窗口或 ss 盲区）"""
    if port in _kernel_listening_ports():
        return "listening"
    if _can_bind(port, "0.0.0.0") and _can_bind(port, "127.0.0.1"):
        return "free"
    return "blocked"


def in_range(port: int) -> bool:
    cfg = get_config()
    return cfg.port_lo <= port <= cfg.port_hi


def available_ports(count: int = 10) -> list[int]:
    """从 port_lo 起的前 count 个可用端口（界面下拉用）。"""
    cfg = get_config()
    out: list[int] = []
    for p in range(cfg.port_lo, cfg.port_hi + 1):
        if port_free(p):
            out.append(p)
            if len(out) >= count:
                break
    return out


def allocate(preferred: int | None = None) -> int:
    """分配一个端口：优先 preferred（须检验通过），否则顺序找。"""
    if preferred is not None:
        if not in_range(preferred):
            raise ValueError(f"端口须在 {get_config().port_lo}-{get_config().port_hi} 范围内")
        if not port_free(preferred):
            raise ValueError(f"端口 {preferred} 不可用（被占用或未通过检验）")
        return preferred
    cfg = get_config()
    for p in range(cfg.port_lo, cfg.port_hi + 1):
        if port_free(p):
            return p
    raise RuntimeError(f"端口段 {cfg.port_lo}-{cfg.port_hi} 已耗尽")
