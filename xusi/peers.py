"""peer 名册：etc/peers.toml 持久化 + 探活缓存。

加 peer = 探活拿 id 写 toml；列 peer = 读 toml；探活 = hit
{peer.url}/api/peer/id（不鉴权，握手用），5s TTL 缓存避免前端轮询
打爆 peer。

fail-soft 原则：所有错误都从函数签名表达（返回 dict 含 ok/error），不抛
HTTPException——上层（api.py）按上下文决定码（404/400/502）。
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

import httpx
import tomllib

from .config import get_config


class PeerUnreachable(Exception):
    """peer 不可达——网络层失败（add_peer / 探活超时 / 连接拒绝）。"""


class PeerRefused(Exception):
    """本地拒绝——单节点模式、url 格式坏、id 重名等不是网络问题。"""


_PROBE_TTL = 5.0  # 探活缓存秒数；前端轮询通常 5s，不会穿透

_probe_cache: dict[str, tuple[float, dict]] = {}
_probe_lock = threading.Lock()


def is_cluster() -> bool:
    """集群模式开关：cluster_secret 设值 = 开，留空 = 单节点。"""
    return bool(get_config().cluster_secret)


# ── 存储 ───────────────────────────────────────────────────────────

def _load() -> dict:
    f = get_config().peers_file
    if not f.exists():
        return {"peers": []}
    try:
        with f.open("rb") as fh:
            return tomllib.load(fh)
    except Exception as e:
        print(f"警告：{f} 解析失败（{e}），按空名册起服务")
        return {"peers": []}


def _save(data: dict) -> None:
    f = get_config().peers_file
    f.parent.mkdir(parents=True, exist_ok=True)
    # 手工 toml 写出（无 tomli_w 依赖；schema 简单）
    lines: list[str] = []
    for p in data["peers"]:
        lines.append("[[peers]]")
        lines.append(f'id = "{_toml_escape(p["id"])}"')
        lines.append(f'url = "{_toml_escape(p["url"])}"')
        if p.get("name"):
            lines.append(f'name = "{_toml_escape(p["name"])}"')
        lines.append("")
    tmp = f.with_suffix(".toml.tmp")
    tmp.write_text("\n".join(lines), encoding="utf-8")
    tmp.replace(f)


def _toml_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


# ── 名册 CRUD ───────────────────────────────────────────────────────

def list_peers() -> list[dict]:
    """读 etc/peers.toml，返回 [[peers]] 列表（深拷贝）。"""
    return list(_load()["peers"])


def get_peer(peer_id: str) -> dict | None:
    for p in list_peers():
        if p["id"] == peer_id:
            return p
    return None


def find_by_url(url: str) -> dict | None:
    """按 url 查 peer（用于 location 反查，避免重名 url）。"""
    target = url.rstrip("/")
    for p in list_peers():
        if p["url"].rstrip("/") == target:
            return p
    return None


def add_peer(url: str, name: str = "") -> dict:
    """加 peer。先探活（拿 id），落 toml。失败抛 PeerUnreachable / PeerRefused。"""
    if not is_cluster():
        raise PeerRefused("单节点模式（[cluster].secret 未设）；peer 注册需要集群模式")
    url = url.rstrip("/")
    if not (url.startswith("http://") or url.startswith("https://")):
        raise PeerRefused(f"peer url 必须 http(s)://；got: {url}")
    info = _probe_url(url)
    if not info["ok"]:
        raise PeerUnreachable(f"peer 不可达：{info['error']}")
    pid = info["info"]["id"]
    data = _load()
    for p in data["peers"]:
        if p["id"] == pid:
            raise PeerRefused(f"peer {pid} 已存在")
        if p["url"].rstrip("/") == url:
            raise PeerRefused(f"peer url {url} 已存在")
    rec = {"id": pid, "url": url,
           "name": (name or info["info"].get("name", "")).strip()}
    data["peers"].append(rec)
    _save(data)
    with _probe_lock:
        _probe_cache.pop(pid, None)
    return rec


def remove_peer(peer_id: str) -> bool:
    data = _load()
    before = len(data["peers"])
    data["peers"] = [p for p in data["peers"] if p["id"] != peer_id]
    if len(data["peers"]) == before:
        return False
    _save(data)
    with _probe_lock:
        _probe_cache.pop(peer_id, None)
    return True


# ── 探活 ───────────────────────────────────────────────────────────

def _probe_url(url: str) -> dict:
    """裸 url 探活（给 add_peer 还没 id 的情形用）。"""
    start = time.monotonic()
    try:
        with httpx.Client(timeout=httpx.Timeout(5.0, connect=3.0)) as c:
            r = c.get(f"{url.rstrip('/')}/api/peer/id")
        latency = int((time.monotonic() - start) * 1000)
        if r.status_code == 200:
            info = r.json()
            if not isinstance(info, dict) or "id" not in info:
                return {"ok": False, "error": "响应缺 id 字段", "latency_ms": latency}
            return {"ok": True, "info": info, "latency_ms": latency}
        return {"ok": False, "error": f"HTTP {r.status_code}", "latency_ms": latency}
    except Exception as e:
        latency = int((time.monotonic() - start) * 1000)
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "latency_ms": latency}


def probe_peer(peer: dict) -> dict:
    """peer 记录（带 id）的探活——5s TTL 缓存。"""
    pid = peer["id"]
    now = time.monotonic()
    with _probe_lock:
        cached = _probe_cache.get(pid)
        if cached and (now - cached[0]) < _PROBE_TTL:
            return cached[1]
    result = _probe_url(peer["url"])
    with _probe_lock:
        _probe_cache[pid] = (now, result)
    return result


def clear_probe_cache() -> None:
    """CLI 强制重探时清缓存。"""
    with _probe_lock:
        _probe_cache.clear()
