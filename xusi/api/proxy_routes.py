"""反代路由：/px/{id}/*、/svc、/svc/{id}/{name}/*、/v1/*、/ui/*。

共享辅助：
- _svc_px_auth  /px 与 /svc 共用鉴权（管理面 token 或该 agent 观察台 token）
- _rec_of       软版本 extract caller rec（不抛 401）
- _forward_passthrough  远端 /px 转发（与 forward_to_peer 区别：保留 observer token 透传）
"""
import httpx
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

from .. import agentops, authtok, proxy, registry, services
from .auth import _rec_of

router = APIRouter()


def _svc_px_auth(request: Request, agent: dict) -> None:
    """/px 与 /svc 共用鉴权（二选一）：
    ① 管理面 token（admin——所有 token 都是 admin，无需范围检查）；
    ② 该 agent 自己的观察台 token（让 agent 自带页面/仅持观察台 token 的
       外部客户端如 voidhub App 也能通行）。"""
    tok = request.query_params.get("mtoken")
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        tok = auth[7:].strip() or tok
    rec = authtok.verify(tok) if tok else None
    if rec:
        return   # 管理面 token 通过——admin 通配所有 agent
    if not tok or tok not in agentops.read_agent_tokens(agent):
        raise HTTPException(401, "missing or invalid token（管理面 token 或该 agent 的观察台 token）")


@router.api_route("/px/{agent_id}/{sub_path:path}",
                  methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
async def px(request: Request, agent_id: str, sub_path: str = "") -> Response:
    """前缀反代。鉴权二选一（见 _svc_px_auth）：
    ① 管理面 token——转发时自动注入 agent token；
    ② 该 agent 自己的观察台 token——让 agent 自带观测台页面在新标签页里
       （拿不到管理面 token 的上下文）发出的 Bearer 请求也能通行。

    远端 agent（集群模式 + 在 peer 上）：本机无法验观察台 token
    （peer 的 agent tokens.json 不在本机），改由 peer 端自己 inject agent token
    再转给本地 agent 的 127.0.0.1。HTML 中的相对路径 `/v1/*` 由 peer 在 HTML 重写时
    改成 `/px/{id}/v1/*`——浏览器仍在 dev 页面里继续触发 `/px/...`，再被 dev 转发到 peer
    （递归）。观察台面板 JS 的 fetch 只带 `Authorization: Bearer <该 agent 的观察台 token>`
    ——这种 token 我们本地 verify 一定返 None，必须整段转给 peer 让它验（peer 有自己的
    agent tokens.json 命中）。"""
    target = proxy.resolve(agent_id, rec=_rec_of(request))
    if target is None:
        raise HTTPException(404, f"agent 不存在: {agent_id}")
    if target.kind == "local":
        # 本机：观察台 token 走 `_svc_px_auth` 的备用路径（Bearer = 观察台 token 也行）
        _svc_px_auth(request, target.agent)
        return await proxy.prefix_proxy(request, agent_id, sub_path)
    # 远端：_rec_of 拿到的是 caller 的本机验证 rec——PLAIN 时用它走 bearer_headers
    # 包装成 JWT 给 peer（peer 端 tokens.json 里没 caller 的 PLAIN）；观察台 token
    # 时 rec=None（peer 才有该 agent 的观察台 token），整段 Authorization / mtoken
    # 原样透传，peer 端 _svc_px_auth 自己验。
    return await _forward_passthrough(target.peer, request, request.url.path,
                                       rec=_rec_of(request))


async def _forward_passthrough(peer: dict, request: Request, target_path: str,
                               rec: dict | None = None) -> Response:
    """远端 /px/{id}/... 的专用转发。

    与 proxy.forward_to_peer 的区别：forward_to_peer 走 bearer_headers 重新
    构造 Authorization——caller 的 PLAIN（明文 legacy）也会被当场包装成 JWT。
    /px 这边同样需要包装：peer 端 tokens.json 里没有 caller PLAIN，直接透传
    → peer verify 返 None → 401。包装的 claims 来自 rec（=本机 verify 结果），
    不会再造"无 verify 凭据"——角色/agents 仍是 caller 在 tokens.json 里登记的
    真实范围，peer 用 JWT 验完会按 JWT 的 agents 重新 enforce，权限不放大。

    不像 /api/agents/{id}/* 那种纯 JSON 读端点能直接用 forward_to_peer——/px
    鉴权还接受「agent 自己观察台 token」，那种 token 不在 rec 里（仅 peer 有），
    也无 rec 可包装——这时候就只能原样透传，peer 自己验。

    收尾行为：
    - rec 已 verify（管理面 token 在本机 tokens.json 命中）：
      headers['Authorization'] = sign_jwt_for(rec) 或原 JWT 透传
    - rec 未 verify（agent 观察台 token 或 caller 不在本机）：
      透传 caller 的 Authorization / mtoken，peer 端 _svc_px_auth 自己验。
    """
    url = f"{peer['url'].rstrip('/')}{target_path}"
    headers = {k: v for k, v in request.headers.items() if k.lower() not in proxy._HOP_HEADERS}
    if rec is not None:
        # 用包装/透传后的 Authorization 覆盖 caller 原 header（如果有的话）
        headers.update(proxy.bearer_headers(rec))
    # query 整段透传：peer 端 require_auth / _svc_px_auth 自己读 mtoken + token。
    params = dict(request.query_params)
    body = await request.body()
    try:
        cli = proxy.client()
        req = cli.build_request(request.method, url, params=params or None,
                                headers=headers, content=body,
                                timeout=httpx.Timeout(30.0, connect=5.0))
        resp = await cli.send(req, stream=True)
    except httpx.HTTPError as e:
        raise HTTPException(502, f"peer {peer['id']} 不可达：{type(e).__name__}: {e}") from e
    out_headers = [(k, v) for k, v in resp.headers.multi_items()
                   if k.lower() not in proxy._DROP_RESP_HEADERS]
    heads, extra = proxy._split_headers(out_headers)
    out = StreamingResponse(resp.aiter_raw(), status_code=resp.status_code,
                            headers=heads, background=BackgroundTask(resp.aclose))
    out.raw_headers.extend(extra)
    return out


@router.get("/svc")
def svc_discover(request: Request, probe: bool = False) -> dict:
    """服务发现：凭 token 找到服务入口，无需预知 agent-id / 服务名
    （App 形态只有 IP+端口+token，正是这个入口的受众）。
    agent 观察台 token → 仅该 agent；管理面 token → 全部 agent（admin 通配）。
    条目脱敏（不含 token_file 路径）。"""
    tok = request.query_params.get("mtoken")
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        tok = auth[7:].strip() or tok

    def _entry(agent: dict) -> dict:
        svcs = []
        for s in services.list_services(agent, probe=probe)["services"]:
            row = {k: s[k] for k in ("name", "title", "port", "base_path", "note",
                                     "auth", "auto", "token_source",
                                     "openapi_source") if k in s}
            # openapi = 解析后的实际可用路径（声明优先、候选探测兜底）；null = 无自描述
            row["openapi"] = s.get("openapi_found")
            if "health" in s:
                row["health"] = s["health"]
            svcs.append(row)
        return {"agent": agent["id"], "name": agent.get("name") or agent["id"],
                "base": f"/svc/{agent['id']}/", "services": svcs}

    if tok:
        found = proxy.agent_by_agent_token(tok)
        if found:
            return {"agents": [_entry(found[0])]}
        rec = authtok.verify(tok)
        if rec:
            return {"agents": [_entry(a) for a in registry.list_agents()]}
    raise HTTPException(401, "missing or invalid token（管理面 token 或 agent 观察台 token）")


@router.api_route("/svc/{agent_id}/{svc_name}/{sub_path:path}",
                  methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
async def svc(request: Request, agent_id: str, svc_name: str, sub_path: str = "") -> Response:
    """agent 自建服务的**全功能透明反代**：任意方法与请求体原样转发，方法
    放行与否由服务自己决定（管理面不替 agent 决策）；非 GET/HEAD/OPTIONS 的
    调用写审计 svc.write（被动记录，不干预）。鉴权同 /px（二选一，见 _svc_px_auth）。
    客户端 Authorization 不透传：清单声明 token_file 则服务端替换注入，否则删除。
    浏览器 CORS 预检（OPTIONS + Access-Control-Request-Method）本地应答——
    预检发不出 Authorization，真实请求照常鉴权。"""
    if (request.method == "OPTIONS"
            and request.headers.get("access-control-request-method")):
        return Response(status_code=204, headers={
            "access-control-allow-origin": "*",
            "access-control-allow-methods": "GET, POST, PUT, DELETE, PATCH, HEAD, OPTIONS",
            "access-control-allow-headers": "Authorization, Content-Type",
            "access-control-max-age": "86400",
        })
    agent = registry.get_agent(agent_id)
    if not agent:
        raise HTTPException(404, f"agent 不存在: {agent_id}")
    _svc_px_auth(request, agent)
    svc_rec = services.find_service(agent, svc_name)
    if not svc_rec:
        names = ", ".join(services.service_names(agent)) or "（无）"
        raise HTTPException(404, f"服务不存在: {svc_name}（可用：{names}）")
    resp = await proxy.service_proxy(request, agent, svc_rec, sub_path)
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        agentops.audit("svc.write", agent=agent_id, service=svc_name,
                       method=request.method, path="/" + sub_path,
                       status=resp.status_code)
    return resp


@router.api_route("/v1/{sub_path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def root_v1(request: Request, sub_path: str = "") -> Response:
    return await proxy.token_routed(request, "/v1/" + sub_path)


@router.api_route("/ui/{sub_path:path}", methods=["GET", "POST"])
async def root_ui(request: Request, sub_path: str = "") -> Response:
    return await proxy.token_routed(request, "/ui/" + sub_path)
