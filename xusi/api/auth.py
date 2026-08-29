"""鉴权依赖：每个路由模块按需 import。

管理面端点只看一种凭证：admin token（etc/xusi.toml 的 [admin].secret）。
任何 `verify(token) == rec` 的请求持有人都是 admin——
`require_auth` / `require_admin` 是同一档依赖（保留两个名字是给路由签名做
语义占位，admin-only 路由用 `require_admin` 一眼可读）。

依赖家族：
- require_auth     仅 verify（读端点用）
- require_admin    require_auth 别名（语义占位：admin-only 写）
- require_agent    本地存在性检查（单 xusi：所有 agent 都在本机 registry）
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
    """require_auth 别名。所有合法 token 都是 admin——保留名字让路由签名
    一眼看出"admin-only 接口"。"""
    return rec


def require_agent(agent_id: str, rec: dict = Depends(require_auth)) -> tuple[dict, dict]:
    """管理面 token + agent 存在性检查（本机 registry）。"""
    agent = registry.get_agent(agent_id)
    if not agent:
        raise HTTPException(404, f"agent 不存在: {agent_id}")
    return agent, rec
