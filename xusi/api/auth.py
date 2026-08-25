"""鉴权依赖：每个路由模块按需 import。

管理面写端点只看一种凭证：`[cluster].secret`。任何 `verify(token) == rec`
的请求持有人都是 admin——`require_auth` / `require_admin` 是同一档依赖（保留
两个名字是给路由签名做语义占位，admin-only 路由用 `require_admin` 一眼可读）。

**api token（反代入口凭证）不能进任何 `/api/*` 端点**——它只走 `/px /svc /v1 /ui`，
由 proxy_routes._svc_px_auth 单独鉴权，不复用本文件的依赖家族。

依赖家族：
- require_auth                    仅 verify（读端点用）
- require_admin                   require_auth 别名（语义占位：admin-only 写）
- require_agent                   本地 + 任意 admin（写端点用 local-only）
- require_agent_or_remote         本地 / 远端 + 任意 admin（读端点用）
- require_agent_or_remote_admin   本地 / 远端 + admin（写端点用，远端转发时
                                  透传 caller Authorization 让 peer 端自己
                                  verify；本机不再做"是否 admin"二次校验）

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
    """管理面 token + agent 存在性检查（本地）。admin 通配所有范围——不再做 scope 检查。

    仅查本地 registry；远端 agent 在本机是 404。适合"明确只动本机"的写路径
    （如 POST /api/restore 恢复一个新 agent）。"""
    agent = registry.get_agent(agent_id)
    if not agent:
        raise HTTPException(404, f"agent 不存在: {agent_id}")
    return agent, rec


async def require_agent_or_remote(
    request: Request,
    agent_id: str,
    rec: dict = Depends(require_auth),
):
    """读端点鉴权依赖（任意 admin 都能读）。

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


async def require_agent_or_remote_admin(
    request: Request,
    agent_id: str,
    _rec: dict = Depends(require_admin),
):
    """写端点鉴权依赖：admin-only + 本地 / 远端解析。

    与 require_agent_or_remote 的差别：额外 `_rec: dict = Depends(require_admin)`
    保证只有 admin 才能进。语义上：
    - 本地命中 → local 走 agentops.*；peer 端也验同一 cluster_secret
    - 远端命中 → caller Authorization 头原样透传，peer 端再 verify 同一
      cluster_secret（两端同一密钥即同集群，admin 自动通配）"""
    from .. import proxy
    target = proxy.resolve(agent_id, request=request)
    if target is None:
        raise HTTPException(404, f"agent 不存在: {agent_id}")
    return target, _rec
