"""智能体互联 token 管理端点：admin-only。

POST   /api/inter-agent-tokens        签发（若已存在则返现有那条；不重发）
GET    /api/inter-agent-tokens        列（id/label/created_at + 明文，admin 视角）
DELETE /api/inter-agent-tokens/{id}   吊销（按 id）

被 api token / 互联 token / webui token 自己访问时：401（管理面写端点一律
不认这三档，仅 admin token 通过）。
"""
from fastapi import APIRouter, Depends, HTTPException

from .. import agentops, inter_agent_tokens
from .auth import require_admin

router = APIRouter()


@router.get("/api/inter-agent-tokens")
def api_inter_agent_tokens_list(_rec: dict = Depends(require_admin)) -> list[dict]:
    """列本 xusi 的互联 token：admin 视角，含明文（落盘文件 600）。"""
    return inter_agent_tokens.list_tokens()


@router.post("/api/inter-agent-tokens", status_code=201)
def api_inter_agent_tokens_new(_rec: dict = Depends(require_admin)) -> dict:
    """签发本 xusi 的互联 token。

    若已存在则**返现有那条**（不重发）—— 互联 token 是集群共享的入口凭证，
    重复签发会让所有已下发的副本失效。轮换路径：先 DELETE，再 POST。"""
    token, rec = inter_agent_tokens.mint()
    # 首次签发记 audit（已存在的不记——避免 admin 误以为发生了变更）
    rows = inter_agent_tokens.list_tokens()
    if len(rows) == 1:
        agentops.audit("inter_agent_token.new", id=rec["id"],
                       label=rec.get("label", ""))
    return rec


@router.delete("/api/inter-agent-tokens/{token_id}")
def api_inter_agent_tokens_revoke(token_id: str,
                                  _rec: dict = Depends(require_admin)) -> dict:
    if not inter_agent_tokens.revoke(token_id):
        raise HTTPException(404, f"未找到互联 token: {token_id}")
    agentops.audit("inter_agent_token.revoke", id=token_id)
    return {"revoked": token_id}
