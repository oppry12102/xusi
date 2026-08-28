"""反代路由：/px/{id}/*、/svc、/svc/{id}/{name}/*、/v1/*、/ui/*。

共享辅助：
- _svc_px_auth       /px 与 /svc 共用鉴权（管理面 token 或该 agent 观察台 token）
- _forward_passthrough 远端 /px 转发（透传 caller Authorization；与
                        forward_to_peer 的差别：原 Authorization 透传，
                        不重新构造，便于 agent 观察台 token 走 peer 自验）
"""
import asyncio

import httpx
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

from .. import agentops, apitokens, authtok, inter_agent_tokens, proxy, registry, services
from .auth import require_auth
from fastapi import Depends

router = APIRouter()


def _svc_px_auth(request: Request, agent: dict) -> None:
    """/px 与 /svc 共用鉴权（四档，任一通过即放行）：
    ① 管理面 token（cluster_secret——admin 通配所有 agent）；
    ② 反代入口 api token（etc/tokens.json——admin 签发、签给外部反代服务用，
       仅这一档能跨过 /px /svc（/v1 仅 health 探活），/api/* 一律不认）；
    ③ 智能体互联 token（etc/inter_agent_tokens.json——本 xusi 一把，集群内
       agent 互调 /svc 时用；不验目标，与 api token 同语义"持票即登机"）；
    ④ 该 agent 自己的观察台 token（让 agent 自带页面/仅持观察台 token 的
       外部客户端如 voidhub App 也能通行）。"""
    tok = request.query_params.get("mtoken")
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        tok = auth[7:].strip() or tok
    rec = authtok.verify(tok) if tok else None
    if rec:
        return   # 管理面 token 通过——admin 通配所有 agent
    if tok and apitokens.verify(tok):
        return   # api token 通过——仅进反代入口，与 admin 完全隔离
    if tok and inter_agent_tokens.verify(tok):
        return   # 互联 token 通过——同集群 agent 互调 /svc 的入场券
    if not tok or tok not in agentops.read_agent_tokens(agent):
        raise HTTPException(401, "missing or invalid token（管理面 token、api token、互联 token 或该 agent 的观察台 token）")


@router.api_route("/px/{agent_id}/{sub_path:path}",
                  methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
async def px(request: Request, agent_id: str, sub_path: str = "") -> Response:
    """前缀反代。鉴权二选一（见 _svc_px_auth）：
    ① 管理面 token——转发时自动注入 agent token；
    ② 该 agent 自己的观察台 token——让 agent 自带观测台页面在新标签页里
       （拿不到管理面 token 的上下文）发出的 Bearer 请求也能通行。

    远端 agent（集群模式 + 在 peer 上）：本机无法验观察台 token
    （peer 的 agent tokens.json 不在本机），整段 Authorization / mtoken 原样
    透传给 peer，peer 端 _svc_px_auth 自己验。观察台 token 走 peer 是
    "voidhub 形态"的零改动接入前提。"""
    # resolve 的 fan-out 是线程池 + 网络探查（硬墙 10s）——线程池跑，别冻事件循环
    target = await asyncio.to_thread(proxy.resolve, agent_id, request=request)
    if target is None:
        raise HTTPException(404, f"agent 不存在: {agent_id}")
    if target.kind == "local":
        # 本机：观察台 token 走 `_svc_px_auth` 的备用路径（Bearer = 观察台 token 也行）
        _svc_px_auth(request, target.agent)
        return await proxy.prefix_proxy(request, agent_id, sub_path)
    # 远端：原样透传全部 Authorization / mtoken，由 peer 端 _svc_px_auth 处理
    return await _forward_passthrough(target.peer, request, request.url.path)


async def _forward_passthrough(peer: dict, request: Request, target_path: str) -> Response:
    """远端 /px/{id}/... 的专用转发。

    与 `proxy.forward_to_peer` 在 header 处理上一致（都做 hop header 过滤）
    之所以单独存在是因为 /px 的鉴权接受 agent 自己的观察台 token，而这种
    token 仅 peer 端可见——peer 端 `_svc_px_auth` 必须自己验。本机只负责把
    caller 的 Authorization / mtoken 不动声色地穿过去。

    收尾行为：hop header 过滤 + body/method 全透传 + 流式回传。
    """
    url = f"{peer['url'].rstrip('/')}{target_path}"
    headers = {k: v for k, v in request.headers.items() if k.lower() not in proxy._HOP_HEADERS}
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
    agent 观察台 token → 仅该 agent；管理面 token / api token / 互联 token → 全部 agent
    （admin 通配；api token 不绑 agent，给外部反代服务发现用；互联 token
    不绑 agent，给同集群其他 agent 发现用）。
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
        if apitokens.verify(tok):
            return {"agents": [_entry(a) for a in registry.list_agents()]}
        if inter_agent_tokens.verify(tok):
            return {"agents": [_entry(a) for a in registry.list_agents()]}
    raise HTTPException(401, "missing or invalid token（管理面 token、api token、互联 token 或 agent 观察台 token）")


@router.api_route("/svc/{agent_id}/{svc_name}/{sub_path:path}",
                  methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
async def svc(request: Request, agent_id: str, svc_name: str, sub_path: str = "") -> Response:
    """agent 自建服务的**全功能透明反代**：任意方法与请求体原样转发，方法
    放行与否由服务自己决定（管理面不替 agent 决策）；非 GET/HEAD/OPTIONS 的
    调用写审计 svc.write（被动记录，不干预）。鉴权同 /px（四档，见 _svc_px_auth）。
    客户端 Authorization 不透传：清单声明 token_file 则服务端替换注入，否则删除。
    浏览器 CORS 预检（OPTIONS + Access-Control-Request-Method）本地应答——
    预检发不出 Authorization，真实请求照常鉴权。

    集群模式：本机 /svc **只服务本机 agent**（跨节点 id 一律 404「agent 不
    存在」，≠ 它挂了）。跨节点调用直连对端节点的 /svc——地址与凭证都在发现
    接口里（/api/agent-peers 行内 node_url + inter_agent_token，qwen-api 同款
    形态）。不做本机中转：peer 本就是唯一鉴权点（转发分支本机不验票，不增加
    安全，只增加跳数、双计审计与未认证者的 agent-id 存在性探测面）。
    """
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
    # find_service / service_names 走清单文件 + cgroup/proc 扫描（自动发现）——
    # 线程池跑；这是 /svc 反代热路径，每个请求都要过
    svc_rec = await asyncio.to_thread(services.find_service, agent, svc_name)
    if not svc_rec:
        names = ", ".join(await asyncio.to_thread(
            services.service_names, agent)) or "（无）"
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
