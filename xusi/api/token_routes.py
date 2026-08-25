"""Token 路由：管理面 token + agent 观察台 token。

观察台 token 部分跨节点可读（forward_to_peer 透传）。
"""
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from .. import agentops, authtok, proxy
from .auth import require_admin, require_agent, require_agent_or_remote
from .models import TokenMgrNewReq, TokenNewReq

router = APIRouter()


# ── 管理面 token（admin / user；可视 / 签发 / 撤销）─────────────────────

@router.get("/api/tokens")
def api_tokens_list(_rec: dict = Depends(require_admin)) -> list[dict]:
    """列出管理面 token。仅 admin——含其他 admin 的 token 原文。

    `kind` 标记 token 形态：
    - `plain`：xusi 统一签发的 PLAIN（用户应持有的形态，跨集群也通——转发时
       xusi 内部自动包成短期 JWT 给 peer）；
    - `jwt`：历史上遗留 JWT 的形态标记，当前不会再签发（见 authtok.py）。"""
    rows = []
    for t in authtok.list_tokens():
        is_jwt = authtok.is_jwt(t["token"])
        rows.append({
            "token": t["token"],
            "label": t["label"],
            "role": t["role"],
            "agents": t["agents"],
            "created_at": t["created_at"],
            "kind": "jwt" if is_jwt else "plain",
        })
    return rows


@router.post("/api/tokens", status_code=201)
def api_tokens_new(req: TokenMgrNewReq,
                   _rec: dict = Depends(require_admin)) -> dict:
    """签发新的管理面 token。仅 admin 可调——admin / user 都得 admin 来签。"""
    if req.role not in ("admin", "user"):
        raise HTTPException(400, "role 须为 admin 或 user")
    try:
        rec = authtok.new_token(req.label, role=req.role,
                                agents=req.agents, rotate=req.rotate)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {
        "token": rec["token"],
        "label": rec["label"],
        "role": rec["role"],
        "agents": rec["agents"],
        "created_at": rec["created_at"],
        "rotated": req.rotate,
    }


@router.delete("/api/tokens/{prefix}")
def api_tokens_revoke(prefix: str,
                      _rec: dict = Depends(require_admin)) -> dict:
    """按前缀撤销管理面 token。仅 admin 可调。"""
    if len(prefix) < 8:
        raise HTTPException(400, "请提供至少 8 位 token 前缀")
    n = authtok.revoke_token(prefix)
    return {"revoked": n, "prefix": prefix}


# ── agent 观察台 token ───────────────────────────────────────────────

@router.get("/api/agents/{agent_id}/tokens")
async def api_agent_tokens_list(request: Request,
                                pair: tuple = Depends(require_agent_or_remote)) -> Response:
    target, rec = pair
    if target.kind == "local":
        return JSONResponse(agentops.tokens_list(target.agent["id"]))
    return await proxy.forward_to_peer(target.peer, request, request.url.path, rec=rec)


@router.post("/api/agents/{agent_id}/tokens", status_code=201)
def api_agent_token_new(req: TokenNewReq, pair: tuple = Depends(require_agent),
                        _rec: dict = Depends(require_admin)) -> dict:
    return agentops.token_new(pair[0]["id"], req.label)


@router.delete("/api/agents/{agent_id}/tokens/{prefix}")
def api_agent_token_revoke(prefix: str, pair: tuple = Depends(require_agent),
                           _rec: dict = Depends(require_admin)) -> dict:
    return agentops.token_revoke(pair[0]["id"], prefix)
