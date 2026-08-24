"""跨节点反代（Phase 2）：把"远端 agent 的读"做得跟本地一样。

三个职责：
1. locality 解析——给 agent_id 看它在本地还是哪个 peer。TTL 缓存；
   本地优先；都不命中就 fan-out 给所有 peer 探 `/api/agents`，找到就记下。
2. fetch_json——对 peer 发带 JWT 的 HTTP 请求，返回 parsed JSON 或抛 PeerUnreachable。
3. forward_to_peer——流式转发（用于 `/api/agents/{id}/*` 这类纯 JSON 读端点）。

不复用 `proxy.forward()` 的原因：那个函数为 `/px/{id}/ui` 的 HTML 重写
和 `/svc/{id}/...` 的 Location 前缀而生，对纯 JSON 读端点是负担。共享的
是 httpx 客户端与 header 列表，从 `proxy` 模块导入。
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Literal

import httpx
from fastapi import HTTPException, Request, Response

from . import authtok, peers, proxy, registry

# 复用 proxy 的资源
_client_factory = proxy.client
_HOP = proxy._HOP_HEADERS
_DROP = proxy._DROP_RESP_HEADERS

# exceptions 在 peers.py 里定义；这里 re-export 方便上层 import
PeerUnreachable = peers.PeerUnreachable
PeerRefused = peers.PeerRefused

# locality 缓存 TTL：30s。太短穿透打 peer；太长 peer 加了新 agent 也看不见
_LOCALITY_TTL = 30.0
_NEG_TTL = 5.0  # 未命中短缓存，防穿透打爆 peer

_loc_cache: dict[str, tuple[float, "AgentTarget | None"]] = {}
_loc_lock = threading.Lock()


# ── 目标 ────────────────────────────────────────────────────────────

@dataclass
class AgentTarget:
    """agent 的实际所在地（解析结果）。"""
    kind: Literal["local", "remote"]
    agent_id: str
    # local 时：本地 registry 记录；remote 时：远端 peer 记录（id, url, name）
    agent: dict | None = None
    peer: dict | None = None


# ── locality 解析 ───────────────────────────────────────────────────

def resolve(agent_id: str, rec: dict | None = None) -> AgentTarget | None:
    """找 agent 在哪。本地优先；都不命中返回 None（404）。

    缓存策略：30s 命中 / 5s 未命中，避免 agent 重命名后短暂不一致。
    cluster 模式未启用时永远走本地分支（peer 列表为空）。

    rec：caller 的 token 记录（用于 fan-out 时给 peer 鉴权用）；本地命中时
    不需要，但签名上保留一致让调用方统一传。"""
    now = time.monotonic()
    with _loc_lock:
        cached = _loc_cache.get(agent_id)
        if cached:
            ts, target = cached
            ttl = _LOCALITY_TTL if target else _NEG_TTL
            if (now - ts) < ttl:
                return target

    # 本地查
    a = registry.get_agent(agent_id)
    target: AgentTarget | None
    if a:
        target = AgentTarget(kind="local", agent_id=agent_id, agent=a)
        _put_cache(agent_id, target)
        return target

    # cluster 模式才 fan-out
    if not peers.is_cluster():
        _put_cache(agent_id, None)
        return None

    # fan-out：并发问每个 peer 的 /api/agents（传 caller JWT 让 peer 鉴权通过）
    found_peer = _fanout_locate(agent_id, rec)
    if found_peer:
        target = AgentTarget(kind="remote", agent_id=agent_id, peer=found_peer)
    else:
        target = None
    _put_cache(agent_id, target)
    return target


def _put_cache(agent_id: str, target: AgentTarget | None) -> None:
    with _loc_lock:
        _loc_cache[agent_id] = (time.monotonic(), target)


def _fanout_locate(agent_id: str, rec: dict | None = None) -> dict | None:
    """并发打所有 peer 的 /api/agents，返回含目标 agent_id 的第一个 peer 记录。

    排除自己：peer 列表来自共享 etc/peers.toml，集群模式下自己的 id
    也可能在里头（多机器各自 git pull 同一份 toml）；fan-out 到自己 = 自递归。

    rec：调用方的 token record（带 token 原文），用于向 peer 鉴权——peer 端
    require_auth 拒无 token 请求，401 → 我们会误以为 peer 没该 agent。"""
    from . import node
    me_id = node.info()["id"]
    pls = [p for p in peers.list_peers() if p["id"] != me_id]
    if not pls:
        return None

    headers = _bearer_headers(rec) if rec else {}
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(pls))) as ex:
        futs = {ex.submit(_peer_has_agent, p, agent_id, headers): p for p in pls}
        for fut in concurrent.futures.as_completed(futs, timeout=10):
            p = futs[fut]
            try:
                if fut.result():
                    return p
            except Exception:
                continue
    return None


def _peer_has_agent(peer: dict, agent_id: str, headers: dict) -> bool:
    """同步探——查 peer 的 agent 列表里有没有目标 id。不关心返回内容，只要 hit。

    必须用 ?local_only=1：peer 端 handler 见此标志只返本地，不再 fan-in 它自己的
    peers。否则双边注册时我们探 peer → peer fan-in 我们 → 我们 fan-in peer →
    5s 超时挂掉，locality 误判"peer 没这个 agent"。"""
    try:
        with httpx.Client(timeout=httpx.Timeout(5.0, connect=3.0)) as c:
            r = c.get(f"{peer['url'].rstrip('/')}/api/agents?local_only=1",
                      headers=headers)
        if r.status_code != 200:
            return False
        rows = r.json()
        return any(isinstance(row, dict) and row.get("id") == agent_id
                   for row in rows)
    except Exception:
        return False


def invalidate_cache(agent_id: str | None = None) -> None:
    """lifecycle 写操作完成后清缓存（Phase 2 v1 不触发，但 v2 写路径需要）。"""
    with _loc_lock:
        if agent_id is None:
            _loc_cache.clear()
        else:
            _loc_cache.pop(agent_id, None)


# ── 跨节点转发 ─────────────────────────────────────────────────────

async def fetch_json(peer: dict, path: str, rec: dict,
                     *, timeout: float = 5.0) -> Any:
    """带调用方 JWT 向 peer 发 GET，返回 parsed JSON。失败抛 PeerUnreachable。

    用于 /api/agents 列表合并——读短超时即可（只取 metadata）。"""
    url = f"{peer['url'].rstrip('/')}{path}"
    headers = _bearer_headers(rec)
    try:
        r = await _client_factory().get(url, headers=headers,
                                        timeout=timeout)
    except httpx.HTTPError as e:
        raise PeerUnreachable(f"peer {peer['id']}：{type(e).__name__}: {e}") from e
    if r.status_code >= 500:
        raise PeerUnreachable(f"peer {peer['id']} HTTP {r.status_code}")
    # 4xx 不抛——上层按业务处理（如 user 无权访问远端 agent 由 user 角色过滤处理）
    if r.status_code >= 400:
        try:
            body = r.json()
        except Exception:
            body = {"detail": r.text[:200]}
        raise PeerHttpError(r.status_code, body)
    return r.json()


class PeerHttpError(Exception):
    """peer 返了 4xx——跟 PeerUnreachable 区分；按 HTTP 码透传给 caller。"""
    def __init__(self, status: int, body: Any):
        self.status = status
        self.body = body
        super().__init__(f"peer HTTP {status}")


async def forward_to_peer(peer: dict, request: Request,
                         target_path: str, *, rec: dict) -> Response:
    """把 FastAPI Request 原样转发到 peer 的同 path，返回 peer 的响应。

    行为：
    - 鉴权头自治：caller 的 token 是 JWT → 透传；是 PLAIN（明文 legacy）→
      当场用 cluster_secret 签短期 JWT 给 peer（用户 PLAIN 也能跨集群透明工作）。
    - 查询串保留（除 mtoken）
    - body / method 全透传
    - 响应除 hop 头外原样回传（流式，避免把 peer 的响应体整包缓冲）
    - peer 不通 → 502 PeerUnreachable
    - peer 4xx/5xx → 透传同码同 body"""
    url = f"{peer['url'].rstrip('/')}{target_path}"
    headers = {k: v for k, v in request.headers.items() if k.lower() not in _HOP}
    # 鉴权头：JWT 透传 / PLAIN 自动包装（与 _bearer_headers 同形）
    headers.update(_bearer_headers(rec))
    params = {k: v for k, v in request.query_params.items() if k != "mtoken"}
    body = await request.body()
    try:
        cli = _client_factory()
        req = cli.build_request(request.method, url, params=params or None,
                                headers=headers, content=body,
                                timeout=httpx.Timeout(30.0, connect=5.0))
        resp = await cli.send(req, stream=True)
    except httpx.HTTPError as e:
        raise HTTPException(502, f"peer {peer['id']} 不可达：{type(e).__name__}: {e}") from e

    out_headers = [(k, v) for k, v in resp.headers.multi_items()
                   if k.lower() not in _DROP]
    heads, extra = proxy._split_headers(out_headers)
    from fastapi.responses import StreamingResponse
    from starlette.background import BackgroundTask
    out = StreamingResponse(resp.aiter_raw(),
                            status_code=resp.status_code,
                            headers=heads,
                            background=BackgroundTask(resp.aclose))
    out.raw_headers.extend(extra)
    return out


def _bearer_headers(rec: dict) -> dict:
    """把 caller 的 token 还原成 Authorization 头。

    PLAIN → 当场签短期 JWT 给 peer（peer 端 tokens.json 里没有 caller PLAIN）；
    JWT 透传（cluster trust，peer 用同密钥重验）。"""
    tok = _token_of(rec)
    if not tok:
        return {}
    if tok.count(".") == 2:
        return {"authorization": f"Bearer {tok}"}
    wrapped = authtok.sign_jwt_for(rec)
    if wrapped:
        return {"authorization": f"Bearer {wrapped}"}
    return {"authorization": f"Bearer {tok}"}


def _token_of(rec: dict) -> str:
    """从 caller 记录里取回 token 原文（JWT 模式 = token 字段即 JWT）。"""
    return rec.get("token", "") or ""
