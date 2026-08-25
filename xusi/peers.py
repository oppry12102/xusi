"""peer 名册：etc/peers.toml 持久化 + 探活缓存。

加 peer = 探活拿 id 写 toml；列 peer = 读 toml；探活 = hit
{peer.url}/api/peer/id（不鉴权，握手用），5s TTL 缓存避免前端轮询
打爆 peer。

Phase 2 v1.1 起加**邀请 token**：dev 节点签发短期 JWT，内含 cluster_secret
+ 自身 URL + 一次性 sid；新机器一行 `curl ... | bash` 跑完自动装好 xusi、
写入 secret、回链注册。详见 issue_invitation / redeem_invitation。

fail-soft 原则：所有错误都从函数签名表达（返回 dict 含 ok/error），不抛
HTTPException——上层（api.py）按上下文决定码（404/400/502）。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
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
            data = tomllib.load(fh)
    except Exception as e:
        print(f"警告：{f} 解析失败（{e}），按空名册起服务")
        return {"peers": []}
    # tomllib.load 对 0 字节 / 全注释文件返回 {}——补默认键
    data.setdefault("peers", [])
    return data


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


# ═════════════════════════════════════════════════════════════════════
# 邀请 token（Phase 2 v1.1：一行命令从零引导新 xusi 节点）
# ═════════════════════════════════════════════════════════════════════

_INV_TTL = 300.0   # 邀请 token 5 分钟过期——一行命令足够；过长会被偷
_invitations: dict[str, tuple[float, dict]] = {}   # sid → (exp_ts, payload)
_inv_lock = threading.Lock()


def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _b64u_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def _sign(payload: dict, secret: str) -> str:
    """HS256 签 JWT。peers 邀请 token 与 authtok 的 JWT 同算法不同上下文（这里
    payload 是邀请信息，含 cluster_secret 一次性传给新节点）。"""
    h = _b64u(json.dumps({"alg": "HS256", "typ": "JWT"},
                         separators=(",", ":")).encode())
    p = _b64u(json.dumps(payload, separators=(",", ":"),
                        ensure_ascii=False).encode())
    sig = hmac.new(secret.encode("utf-8"),
                   f"{h}.{p}".encode("ascii"),
                   hashlib.sha256).digest()
    return f"{h}.{p}.{_b64u(sig)}"


def _verify(token: str, secret: str) -> dict | None:
    parts = token.split(".")
    if len(parts) != 3:
        return None
    h, p, s = parts
    expected = _b64u(hmac.new(secret.encode("utf-8"),
                              f"{h}.{p}".encode("ascii"),
                              hashlib.sha256).digest())
    if not hmac.compare_digest(expected, s):
        return None
    try:
        payload = json.loads(_b64u_decode(p).decode("utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict) or payload.get("kind") != "invitation":
        return None
    exp = payload.get("exp")
    try:
        if exp is None or int(time.time()) > int(exp):
            return None
    except (ValueError, TypeError):
        return None
    return payload


def _purge_inv(now: float) -> None:
    """惰性清过期 sid（每次访问时顺手做，O(n)；n 通常很小）。"""
    with _inv_lock:
        dead = [k for k, (ts, _) in _invitations.items() if ts <= now]
        for k in dead:
            _invitations.pop(k, None)


def issue_invitation(suggested_name: str = "", ttl: int = 300) -> dict | None:
    """签发一行引导用的 JWT。返回 {token, expires_at, install_cmd}；非集群模式返 None。

    内嵌 cluster_secret：让新机器装好后无需手改 etc/xusi.toml。
    sid：新机器 redeem 时凭此消费（一次性，防重放）。
    issuer：本机 public_url——新机器回链的地址。"""
    cfg = get_config()
    if not cfg.cluster_secret:
        return None
    sid = secrets.token_urlsafe(16)
    exp = int(time.time()) + ttl
    payload = {
        "kind": "invitation",
        "sid": sid,
        "secret": cfg.cluster_secret,
        "issuer": cfg.public_url.rstrip("/"),
        "name": (suggested_name or "").strip()[:64],
        "exp": exp,
    }
    token = _sign(payload, cfg.cluster_secret)
    with _inv_lock:
        _invitations[sid] = (time.monotonic() + ttl, payload)
    cmd = f'curl -sSL "{cfg.public_url.rstrip("/")}/api/peers/join.sh?token={token}" | bash -s'
    return {"token": token, "expires_at": exp, "install_cmd": cmd}


def redeem_invitation(token: str, peer_url: str) -> dict:
    """新机器 redeem：验签 + 消费 sid + 把它加入本地 peer 名册。

    失败抛 PeerRefused（验签/过期/sid 已用）；peer 不可达 → PeerUnreachable。
    peer_url：新机器自己的公开 URL（必须从外部可访问，否则反向调不通）。"""
    cfg = get_config()
    if not cfg.cluster_secret:
        raise PeerRefused("单节点模式：邀请 token 需要集群模式")
    payload = _verify(token, cfg.cluster_secret)
    if payload is None:
        raise PeerRefused("邀请 token 无效或签名错误")
    sid = payload.get("sid")
    _purge_inv(time.monotonic())
    with _inv_lock:
        rec = _invitations.pop(sid, None)   # 一次性消费：pop 不存在 = 已用 / 已过期
    if rec is None:
        raise PeerRefused("邀请 token 已使用或已过期")
    # 验签通过、sid 也活着 → 信任 payload 内的 issuer（= 签发者）
    issuer = str(payload.get("issuer", "")).rstrip("/")
    if not issuer:
        raise PeerRefused("邀请 token 缺 issuer 字段")
    # 注册新机器到本机的 peer 名册（走现有 add_peer，URL 格式 / 探活由它把关）
    new_rec = add_peer(peer_url, name=payload.get("name", ""))
    return {**new_rec, "issuer": issuer}
