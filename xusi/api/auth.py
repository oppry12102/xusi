"""鉴权依赖：每个路由模块按需 import。

系统统一只签 admin token——所有 token 都是 admin，无角色/范围检查。

四档：
- require_auth                管理面 token（admin）—— 唯一鉴权档
- require_admin               同 require_auth（admin-only 接口的语义占位）
- require_admin_or_invitation  管理面 token 或邀请 JWT（redeem 专用）
- require_agent               管理面 token + agent 存在性检查（admin 通配，不再 scope 检查）
- require_agent_or_remote     同上但允许 agent 在 peer 上（返回 AgentTarget）
- _rec_of                     软版本（不抛 401），用于 /px 等需要容错的端点
"""
from fastapi import Depends, HTTPException, Request

from .. import authtok, peers, registry


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
    """所有 token 都是 admin——保留作语义占位，让路由签名显式表达"admin-only 接口"。

    历史上这里会判 rec["role"] != "admin"；现在 authtok 不再签 user，去掉即可。"""
    return rec


async def require_admin_or_invitation(request: Request) -> dict:
    """redeem 端点专用：接受管理面 token 或邀请 JWT。

    邀请 JWT 用 cluster_secret 签名、内嵌 cluster_secret——本身就是集群成员资格
    证明；bootstrap 脚本只能凭它调 redeem，没法要求新机器再持一份 admin token。
    返回 rec["role"] 是 "admin"（普通流程）或 "invitation"（bootstrap 流程）。"""
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        raise HTTPException(401, "需要管理面 token 或有效的邀请 token")
    tok = auth[7:].strip()
    if not tok:
        raise HTTPException(401, "需要管理面 token 或有效的邀请 token")
    rec = authtok.verify(tok)
    if rec:   # 所有 admin token 都是 admin——直接通过
        return rec
    if authtok.is_jwt(tok):
        payload = peers.verify_invitation(tok)
        if payload and payload.get("kind") == "invitation":
            return {"role": "invitation", "invitation": payload, "token": tok}
    raise HTTPException(401, "需要管理面 token 或有效的邀请 token")


def require_agent(agent_id: str, rec: dict = Depends(require_auth)) -> tuple[dict, dict]:
    """管理面 token + agent 存在性检查。admin 通配所有范围——不再做 scope 检查。"""
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

    - 本地命中：kind="local"（pair[0].agent == registry 记录）
    - 远端命中：kind="remote"（pair[0].peer == peer 记录；proxy 时由
      proxy.forward_to_peer 透传 caller JWT 到 peer，peer 端重验 + 重 enforce 作用域）
    - 全 miss → 404
    """
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