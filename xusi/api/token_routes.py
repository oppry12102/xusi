"""api token 管理端点：admin-only。

POST   /api/tokens        签发（返明文一次 + 完整记录）
GET    /api/tokens        列（id/label/created_at，不含 hash / 明文）
DELETE /api/tokens/{id}   吊销（按 id）

被 api token 自己访问时：401（管理面写端点一律不认 api token，仅 admin）。
"""
from fastapi import APIRouter, Depends, HTTPException

from .. import agentops, apitokens
from .auth import require_admin
from .models import ApiTokenNewReq

router = APIRouter()


@router.get("/api/tokens")
def api_tokens_list(_rec: dict = Depends(require_admin)) -> list[dict]:
    return apitokens.list_tokens()


@router.post("/api/tokens", status_code=201)
def api_tokens_new(req: ApiTokenNewReq,
                   _rec: dict = Depends(require_admin)) -> dict:
    token, rec = apitokens.mint(req.label)
    agentops.audit("token.new", id=rec["id"], label=rec["label"])
    # 明文只在本次响应里出现一次——落盘记录不含 token 字面量。
    return {**rec, "token": token}


@router.delete("/api/tokens/{token_id}")
def api_tokens_revoke(token_id: str,
                      _rec: dict = Depends(require_admin)) -> dict:
    if not apitokens.revoke(token_id):
        raise HTTPException(404, f"未找到 api token: {token_id}")
    agentops.audit("token.revoke", id=token_id)
    return {"revoked": token_id}