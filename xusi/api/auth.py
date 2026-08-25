"""鉴权依赖：每个路由模块按需 import。

系统只剩一种凭证：`[cluster].secret`。任何 `verify(token) == rec` 的请求
持有人都是 admin——`require_auth` / `require_admin` 是同一档依赖（保留两个
名字是给路由签名做语义占位，admin-only 路由用 `require_admin` 一眼可读）。

`require_agent` / `require_agent_or_remote` 仍返回 `(target_or_agent, rec)`
元组——rec 现在只是 `{"token": token}`，仅在路由需要回传审计上下文时使用；
跨节点转发改成直接透传 `Authorization` 头，调用方通常会忽略 rec。

之前为 invitation JWT 留的 `require_admin_or_invitation` 已彻底删除——
邀请 token 机制归零，新节点引导走 `xusi init --cluster-secret`。
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
        raise HTTPException(401, "missing or invalid admin token")
    return rec


def require_admin(rec: dict = Depends(require_auth)) -> dict:
    """require_auth 别名。所有合法 token 都是 admin——保留名字让路由签名
    一眼看出"admin-only 接口"。"""
    return rec


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
    - 远端命中：kind="remote"（pair[0].peer == peer 记录；proxy 时透传
      caller 的 Authorization 头到 peer，peer 端自己重新 verify）
    - 全 miss → 404
    """
    # 延迟 import：避免 auth.py 顶层依赖 proxy（可能循环）
    from .. import proxy
    target = proxy.resolve(agent_id, request=request)
    if target is None:
        raise HTTPException(404, f"agent 不存在: {agent_id}")
    return target, rec
