"""Agent 路由：CRUD + 生命周期（start/stop/pause/resume/restart）+ 观察 + 投信。

按生命周期分：
- CRUD：list / create / get / patch / delete
- 生命周期：5 个 POST /api/agents/{id}/{action}
- 观察（只读 GET，跨节点 fan-in）：status / events / sessions / messages / outbox / logs
- capabilities / services / mail / tokens 列表

写端点（PATCH / DELETE / 5 lifecycle / mail / tokens new+revoke）走
`require_agent_or_remote_admin`：local 命中走 agentops.*；remote 命中走
`forward_to_peer`——caller 的 Authorization 头原样透传，peer 端用同一
`[cluster].secret` verify 后由该 peer 自己执行 agentops.*。两端同密钥即
同集群，admin 自动通配。
"""
import asyncio

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from .. import agentops, apitokens, authtok, capabilities, inter_agent_tokens, node, peers, proxy, registry, services
from .auth import require_admin, require_agent_or_remote, require_agent_or_remote_admin, require_auth
from .models import CreateAgentReq, MailReq, PatchAgentReq, TokenNewReq

router = APIRouter()


# ── CRUD ─────────────────────────────────────────────────────────────

@router.get("/api/agents")
async def api_agents_list(request: Request,
                          rec: dict = Depends(require_auth),
                          local_only: bool = False) -> list[dict]:
    """agent 一览：本地 + 集群模式下 fan-in peer（每行 _via=<peer-id>）。
    单 peer 挂了不影响其他 / 本地——降级展示即可。

    local_only=true 用于 fan-in 中继：只返回本地注册的 agent，不再二次 fan-out。
    关键意义：防止双边注册时的 fan-in 回环。每个节点的"集群视图"是 local +
    direct peers' local，不再递归 peers-of-peers。

    所有 token 都是 admin——不再做 can_access 过滤，列全量。

    request：注入用于把 `Authorization` 头透传给 peer（peer 端用同一
    cluster_secret verify 后 fan-in 返回）。"""
    rows = await asyncio.to_thread(agentops.list_status)
    if not local_only and peers.is_cluster():
        # 排除自己——peer 列表来自共享 etc/peers.toml，集群模式下自己的 id
        # 也可能在里头（多机器各自 git pull 同一份 toml）；fan-in 到自己 = 自递归。
        me_id = node.info()["id"]
        pls = [p for p in peers.list_peers() if p["id"] != me_id]
        if pls:
            async def _one(p: dict) -> list[dict]:
                try:
                    # 给 peer 传 local_only=1：peer 也只返回自己的 local，
                    # 不再 fan-in 它自己的 peers——这是双边注册能 work 的关键。
                    r = await proxy.fetch_json(p, "/api/agents?local_only=1",
                                                request=request, timeout=5)
                    for row in r:
                        if isinstance(row, dict):
                            row["_via"] = p["id"]
                    return [row for row in r if isinstance(row, dict)]
                except proxy.PeerUnreachable:
                    return []  # 单 peer 挂掉让 list 降级而非 502
                except proxy.PeerHttpError:
                    return []
            results = await asyncio.gather(*[_one(p) for p in pls])
            for r in results:
                rows.extend(r)
    return rows


@router.post("/api/agents", status_code=201)
def api_agents_create(req: CreateAgentReq, _rec: dict = Depends(require_admin)) -> dict:
    """新建 agent 总是落在本机（远端 peer 的注册表由 peer 自己管）——admin
    想在 peer B 上建 agent 要先登 B 的 WebUI。"""
    return agentops.create_agent(
        req.name, req.mission, req.brains, expose=req.expose, port=req.port,
        budgets=req.budgets, note=req.note, source_version=req.source_version)


@router.get("/api/agents/{agent_id}")
async def api_agent_get(request: Request,
                        pair: tuple = Depends(require_agent_or_remote)) -> Response:
    target, _rec = pair
    if target.kind == "local":
        return JSONResponse(await asyncio.to_thread(agentops.status, target.agent["id"]))
    return await proxy.forward_to_peer(target.peer, request, request.url.path)


@router.patch("/api/agents/{agent_id}")
async def api_agent_patch(request: Request, req: PatchAgentReq, apply_restart: bool = False,
                          pair: tuple = Depends(require_agent_or_remote_admin)) -> Response:
    """改 agent 字段。local → agentops.patch_agent；remote → forward。
    apply_restart=1 会触发 restart（agent 重启），远端时 query 串已透传。"""
    changes = {k: v for k, v in req.model_dump().items() if v is not None}
    if not changes:
        raise HTTPException(400, "请求体里没有任何要修改的字段")
    target, _rec = pair
    if target.kind == "local":
        return JSONResponse(agentops.patch_agent(
            target.agent["id"], changes, apply_restart=apply_restart))
    return await proxy.forward_to_peer(target.peer, request, request.url.path)


@router.delete("/api/agents/{agent_id}")
async def api_agent_delete(request: Request,
                           pair: tuple = Depends(require_agent_or_remote_admin)) -> Response:
    target, _rec = pair
    if target.kind == "local":
        return JSONResponse(agentops.delete(target.agent["id"]))
    return await proxy.forward_to_peer(target.peer, request, request.url.path)


# ── 生命周期（5 个 POST）────────────────────────────────────────────

_LIFECYCLE_ACTIONS = ("start", "stop", "pause", "resume", "restart")


def _lifecycle(agent_id: str, action: str) -> dict:
    fn = {"start": agentops.start, "stop": agentops.stop, "pause": agentops.pause,
          "resume": agentops.resume, "restart": agentops.restart}[action]
    return fn(agent_id)


def _make_lifecycle_handler(action: str):
    async def _h(request: Request,
                 pair: tuple = Depends(require_agent_or_remote_admin)) -> Response:
        target, _rec = pair
        if target.kind == "local":
            return JSONResponse(_lifecycle(target.agent["id"], action))
        return await proxy.forward_to_peer(target.peer, request, request.url.path)
    _h.__name__ = f"api_agent_{action}"
    return _h


for _action in _LIFECYCLE_ACTIONS:
    router.post(f"/api/agents/{{agent_id}}/{_action}")(
        _make_lifecycle_handler(_action))


# ── capabilities / services（只读 GET，跨节点 fan-in）──────────────

@router.get("/api/agents/{agent_id}/capabilities")
async def api_agent_capabilities(request: Request,
                                pair: tuple = Depends(require_agent_or_remote)) -> Response:
    """agent 的能力包清单（只读）。enabled 反映其 config [capabilities] 的
    实况（通常全 false——墟司不写该段；若大脑自行写入亦如实显示）。"""
    target, _rec = pair
    if target.kind == "local":
        return JSONResponse(capabilities.list_for_agent(target.agent))
    return await proxy.forward_to_peer(target.peer, request, request.url.path)


@router.get("/api/agents/{agent_id}/services")
async def api_services_list(request: Request, probe: bool = True,
                            pair: tuple = Depends(require_agent_or_remote)) -> Response:
    target, _rec = pair
    if target.kind == "local":
        return JSONResponse(await asyncio.to_thread(
            services.list_services, target.agent, probe=probe))
    return await proxy.forward_to_peer(target.peer, request, request.url.path)


# ── 观察（GET 6 个）与投信 ─────────────────────────────────────────

_OBSERVE_KINDS = ("status", "events", "sessions", "messages", "outbox", "logs")


def _make_observe_handler(what: str):
    async def _h(request: Request, limit: int = 50,
                 pair: tuple = Depends(require_agent_or_remote)) -> Response:
        target, _rec = pair
        if target.kind == "local":
            if what == "logs":
                return JSONResponse(await asyncio.to_thread(
                    agentops.logs, target.agent["id"], limit))
            return JSONResponse({"id": target.agent["id"], "what": what,
                                 "data": await asyncio.to_thread(
                                     agentops.observe, target.agent["id"], what, limit)})
        # 远程：把 ?limit= 一并透传，peer 端 handler 自己解析
        return await proxy.forward_to_peer(target.peer, request,
                                            request.url.path)
    _h.__name__ = f"api_agent_observe_{what}"
    return _h


for _what in _OBSERVE_KINDS:
    router.get(f"/api/agents/{{agent_id}}/{_what}")(
        _make_observe_handler(_what))


@router.post("/api/agents/{agent_id}/mail")
async def api_agent_mail(request: Request, req: MailReq,
                         pair: tuple = Depends(require_agent_or_remote_admin)) -> Response:
    """投信：admin 把一条文字塞进 agent 的 inbox。远端 agent 走 forward。"""
    target, _rec = pair
    if target.kind == "local":
        return JSONResponse(agentops.mail(target.agent["id"], req.text))
    return await proxy.forward_to_peer(target.peer, request, request.url.path)


# ── 智能体互发现：懒查询，按需拿 peer 列表（最小信息）───────────────────

@router.get("/api/agent-peers")
async def api_agent_peers(request: Request, local_only: bool = False) -> dict:
    """智能体互发现：列出当前可联系的其他 agent 的最小信息
    （id / name / node_id / inter_agent_token）。

    四档 token 任一通过：admin / api token / 互联 token / 任意 agent 观察台 token。
    - admin / api / 互联 token：不返回 self 字段（caller 没有"自己"的概念）
    - agent 观察台 token：返回除自己外的全部 peer + self 字段

    互联 token（每 xusi 一把）同集群 agent ↔ agent 互调 /svc 时用——
    本机这枚对本地 peer 行有效，远端 xusi 那枚随 fan-in 一并返回。peer
    行里缺这字段表示对方 xusi 还没签发互联 token。

    cluster 模式 fan-in：跨节点 agent 一并聚合（轻量版——不探活，只读
    id+name+token）。fan-out 路径传 local_only=1 防止递归（peer 端看到该
    标志停止 fan-in）。鉴权用本机 cluster_secret 代替 caller token——后者
    是 agent webui token，跨节点验不了。
    """
    tok = request.query_params.get("mtoken")
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        tok = auth[7:].strip() or tok
    if not tok:
        raise HTTPException(401, "missing token（管理面 token / api token / 互联 token / agent 观察台 token）")

    src_id: str | None = None  # caller 的 agent id；admin/api/互联 token → None
    if authtok.verify(tok):
        src_id = None
    elif apitokens.verify(tok):
        src_id = None
    elif inter_agent_tokens.verify(tok):
        src_id = None  # 互联 token 不绑 agent 身份，caller 不可识别
    else:
        pair = proxy.agent_by_agent_token(tok)
        if pair:
            src_id = pair[0]["id"]
        else:
            raise HTTPException(401, "invalid token")

    me_node_id = node.info()["id"]
    my_inter_token = inter_agent_tokens.get_token()  # None if not minted

    # 本节点 agent
    rows: list[dict] = []
    for a in registry.list_agents():
        if a["id"] == src_id:
            continue
        row = {
            "id": a["id"],
            "name": (a.get("name") or a["id"]).strip() or a["id"],
            "node_id": me_node_id,
        }
        if my_inter_token:
            row["inter_agent_token"] = my_inter_token
        rows.append(row)

    # cluster fan-in
    if not local_only and peers.is_cluster():
        from ..config import get_config
        admin_tok = get_config().cluster_secret
        pls = [p for p in peers.list_peers() if p["id"] != me_node_id]
        if pls and admin_tok:
            async def _one(p: dict) -> None:
                try:
                    r = await proxy.fetch_json(
                        p, "/api/agent-peers?local_only=1",
                        token=admin_tok, timeout=5)
                    peer_rows = r.get("peers", []) if isinstance(r, dict) else []
                    for row in peer_rows:
                        if isinstance(row, dict) and row.get("id") != src_id:
                            new_row = {
                                "id": row["id"],
                                "name": (row.get("name") or row["id"]).strip() or row["id"],
                                "node_id": p["id"],
                            }
                            # 远端 xusi 自带它那把互联 token（若未签发则无字段）
                            remote_tok = row.get("inter_agent_token")
                            if remote_tok:
                                new_row["inter_agent_token"] = remote_tok
                            rows.append(new_row)
                except proxy.PeerUnreachable:
                    pass
                except proxy.PeerHttpError:
                    pass
            await asyncio.gather(*[_one(p) for p in pls])

    out: dict = {
        "access_pattern": "/svc/{peer_id}/{service_name}/*",
        "cluster": {
            "node_id": me_node_id,
            "is_cluster": peers.is_cluster(),
            "peers_known": len(peers.list_peers()),
        },
        "peers": rows,
    }
    if src_id is not None:
        out["self"] = {"id": src_id}
    return out


# ── 观察台 token（agent 自家事，与管理面 cluster_secret 无关）────────────

@router.get("/api/agents/{agent_id}/tokens")
async def api_agent_tokens_list(request: Request,
                                pair: tuple = Depends(require_agent_or_remote)) -> Response:
    target, _rec = pair
    if target.kind == "local":
        return JSONResponse(agentops.tokens_list(target.agent["id"]))
    return await proxy.forward_to_peer(target.peer, request, request.url.path)


@router.post("/api/agents/{agent_id}/tokens", status_code=201)
async def api_agent_token_new(request: Request, req: TokenNewReq,
                              pair: tuple = Depends(require_agent_or_remote_admin)) -> Response:
    """签发该 agent 自己的观察台 token。远端走 forward——peer 端自己读
    自己的 webui_tokens.json 写新 token。"""
    target, _rec = pair
    if target.kind == "local":
        return JSONResponse(agentops.token_new(target.agent["id"], req.label))
    return await proxy.forward_to_peer(target.peer, request, request.url.path)


@router.delete("/api/agents/{agent_id}/tokens/{prefix}")
async def api_agent_token_revoke(request: Request, prefix: str,
                                 pair: tuple = Depends(require_agent_or_remote_admin)) -> Response:
    target, _rec = pair
    if target.kind == "local":
        return JSONResponse(agentops.token_revoke(target.agent["id"], prefix))
    return await proxy.forward_to_peer(target.peer, request, request.url.path)
