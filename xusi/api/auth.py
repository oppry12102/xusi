"""鉴权依赖：每个路由模块按需 import。

管理面凭证两档（见 authtok.verify）：admin token 通吃；智能体专属 token
属地受限——只能访问绑定的那一个 agent。权限语义全部由本依赖家族表达。

依赖家族：
- require_auth         仅 verify（读端点用；rec 带 role）
- require_admin        仅 admin（role != admin → 403）
- require_agent        属地访问：专属 token 只能碰绑定的那个（越界 → 404，
                       与「不存在」同文案同状态码，防枚举探测）+ 存在性检查
- require_admin_agent  admin + agent 存在（PATCH / DELETE / 备份等管理动作）
"""
from fastapi import Depends, HTTPException, Request

from .. import authtok, registry


def require_auth(request: Request) -> dict:
    """从 Authorization: Bearer / ?mtoken= 读 token，verify 返 rec。无 token / 无效 → 401。"""
    tok = None
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        tok = auth[7:].strip()
    if not tok:
        tok = request.query_params.get("mtoken")
    rec = authtok.verify(tok) if tok else None
    if not rec:
        raise HTTPException(401, "missing or invalid admin token")
    return rec


def require_admin(rec: dict = Depends(require_auth)) -> dict:
    """仅 admin token。专属 token 走到这就是 403（能力边界不是秘密——
    对应按钮在前端本就隐藏，文案直说）。"""
    if rec.get("role") != "admin":
        raise HTTPException(403, "此操作需要 admin token")
    return rec


def require_agent(agent_id: str, rec: dict = Depends(require_auth)) -> tuple[dict, dict]:
    """属地访问 + agent 存在性检查（本机 registry）。

    绑定检查在前：专属 token 访问别人的 agent 与「不存在」同形（404 防探测）。"""
    if rec.get("role") == "agent" and rec.get("agent_id") != agent_id:
        raise HTTPException(404, f"agent 不存在: {agent_id}")
    agent = registry.get_agent(agent_id)
    if not agent:
        raise HTTPException(404, f"agent 不存在: {agent_id}")
    return agent, rec


def require_admin_agent(agent_id: str, rec: dict = Depends(require_auth)) -> tuple[dict, dict]:
    """admin token + agent 存在性检查（改参/删除/备份等管理动作用）。"""
    if rec.get("role") != "admin":
        raise HTTPException(403, "此操作需要 admin token")
    agent = registry.get_agent(agent_id)
    if not agent:
        raise HTTPException(404, f"agent 不存在: {agent_id}")
    return agent, rec
