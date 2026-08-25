"""鉴权依赖：每个路由模块按需 import。

四档：
- require_auth           仅管理面 token（admin 或 user）
- require_admin          必须是 admin
- require_agent          必须是 admin OR 该 agent 范围内的 user
- require_agent_or_remote 同上但允许 agent 在 peer 上（返回 AgentTarget）
- _rec_of                软版本（不抛 401），用于 /px 等需要容错的端点
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
        raise HTTPException(401, "missing or invalid manager token")
    return rec


def require_admin(rec: dict = Depends(require_auth)) -> dict:
    if rec["role"] != "admin":
        raise HTTPException(403, "此操作需要 admin token")
    return rec


def require_agent(agent_id: str, rec: dict = Depends(require_auth)) -> tuple[dict, dict]:
    if not authtok.can_access(rec, agent_id):
        raise HTTPException(403, f"token 无权访问 agent {agent_id}")
    agent = registry.get_agent(agent_id)
    if not agent:
        raise HTTPException(404, f"agent 不存在: {agent_id}")
    return agent, rec


async def require_agent_or_remote(
    request: Request,
    agent_id: str,
    rec: dict = Depends(require_auth),
):
    """Phase 2 跨节点读端点的鉴权依赖。

    - 作用域检查先于 locality 查询（不泄露远端 agent 的存在性）
    - 本地命中：kind="local"（pair[0].agent == registry 记录）
    - 远端命中：kind="remote"（pair[0].peer == peer 记录；proxy 时由
      proxy.forward_to_peer 透传 caller JWT 到 peer，peer 端重验 + 重 enforce 作用域）
    - 全 miss → 404
    """
    if not authtok.can_access(rec, agent_id):
        raise HTTPException(403, f"token 无权访问 agent {agent_id}")
    # 延迟 import：避免 auth.py 顶层依赖 proxy（可能循环）
    from .. import proxy
    target = proxy.resolve(agent_id, rec=rec)
    if target is None:
        raise HTTPException(404, f"agent 不存在: {agent_id}")
    return target, rec


def _rec_of(request: Request) -> dict | None:
    """从 Request 提取 caller 鉴权记录（verify 失败返 None，不抛 401——上层按业务决定码）。"""
    tok = None
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        tok = auth[7:].strip()
    if not tok:
        tok = request.query_params.get("mtoken")
    return authtok.verify(tok) if tok else None
