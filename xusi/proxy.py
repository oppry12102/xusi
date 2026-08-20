"""反代核心：单一对外端口（管理面端口）承载所有 agent 的观察台访问。

三种路由，同一端口：
1. 前缀路由 /px/{agent-id}/* —— 管理面 token 鉴权（admin 或该 agent 范围的
   user），转发时自动注入该 agent 的观察台 token，客户端无需持有第二层 token；
   agent 自带的 /ui/ 页面做最小 HTML 路径重写 + 自动带 token，经代理可用。
2. token 路由（根路径 /v1/*、/ui/*）—— 凭 agent 观察台 token 实时定向到
   所属 agent，原样透传。voidhub App（host+port+token 形态）零改动接入：
   host=服务器IP、port=管理面端口、token=该 agent 的观察台 token。
3. 服务路由 /svc/{agent-id}/{服务名}/* —— agent 自建服务的**全功能透明**
   反代：任意方法与请求体原样转发、响应流式回传；方法放行与否由服务自己
   决定，管理面只做鉴权、token 注入与被动审计（不替 agent 决策）。

manager 对 agent 的转发只到 127.0.0.1（无论 agent 是否对外暴露）。
"""
from __future__ import annotations

from typing import Any

import httpx
from fastapi import HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

from . import agentops, registry, services

# 请求侧不透传的头（httpx 自动解压请求体无意义，长度重算）
_HOP_HEADERS = {
    "connection", "keep-alive", "transfer-encoding", "upgrade", "te",
    "trailers", "proxy-authenticate", "proxy-authorization",
    "content-encoding", "content-length", "host",
}

# 响应侧不透传的头：逐跳 + content-length（流式重分块）+ date/server
# （uvicorn 自加，防重复）。content-encoding 流式保留（压缩直传），
# 缓冲改写路径丢弃（httpx 已解压）。其余全透传——上游 CORS 等可达客户端。
_DROP_RESP_HEADERS = {
    "connection", "keep-alive", "transfer-encoding", "upgrade", "te",
    "trailers", "proxy-authenticate", "proxy-authorization",
    "content-length", "date", "server",
}

_client: httpx.AsyncClient | None = None


def client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=5.0))
    return _client


async def close() -> None:
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


# ── token → agent 实时映射（读各 agent 的 webui_tokens.json，通道 3）──

def agent_by_agent_token(token: str) -> tuple[dict, str] | None:
    """agent 观察台 token → (agent 记录, token)。含管理员在 agent 侧自签的 token。"""
    if not token:
        return None
    for a in registry.list_agents():
        if token in agentops.read_agent_tokens(a):
            return a, token
    return None


# ── 转发 ─────────────────────────────────────────────────────────────

async def forward(request: Request, agent: dict, target_path: str, *,
                  inject_token: str | None = None,
                  keep_query: bool = True,
                  drop_params: tuple[str, ...] = ("mtoken",),
                  extra_params: dict[str, str] | None = None,
                  port: int | None = None,
                  base_path: str = "",
                  prefix: str | None = None,
                  timeout: httpx.Timeout | None = None) -> Response:
    """把请求转发到 agent 的 127.0.0.1 端口（透明管道：任意方法与请求体
    原样转发）。inject_token 时替换鉴权头。port 缺省 agent["port"]（观察台）；
    自建服务传自己的端口。base_path 用于挂在子路径的服务（前拼）；prefix
    非 None 时对返回的 HTML / 根相对 Location 做前缀重写（/svc 场景，让
    /docs 这类页面经代理可用）。timeout 缺省用客户端默认（读 30s），自建
    服务传长读超时（长任务 POST / SSE）。

    响应除需改写的 HTML（/px 的 /ui 页、/svc 前缀下的 docs 页）缓冲处理外，
    一律流式回传——SSE / 分块 / 大响应原样通过，不被整包缓冲掐断。"""
    real_port = port if port is not None else agent["port"]
    url = f"http://127.0.0.1:{real_port}{base_path}{target_path}"
    params: dict[str, str] = {}
    if keep_query:
        params = {k: v for k, v in request.query_params.items()
                  if k not in drop_params}
    if extra_params:
        params.update(extra_params)

    headers = {k: v for k, v in request.headers.items()
               if k.lower() not in _HOP_HEADERS}
    if inject_token is not None:
        headers.pop("authorization", None)
        if inject_token:
            headers["authorization"] = f"Bearer {inject_token}"

    body = await request.body()
    req = client().build_request(request.method, url, params=params or None,
                                 headers=headers, content=body, timeout=timeout)
    try:
        resp = await client().send(req, stream=True)
    except httpx.HTTPError as e:
        raise HTTPException(502, f"上游不可达（agent {agent['id']}，"
                                 f"127.0.0.1:{real_port}）：{type(e).__name__}。"
                                 f"若已暂停（SIGSTOP）属正常现象") from e

    # 响应头：multi_items 保重复头（如 set-cookie），location 做前缀重写
    out_headers = [(k, v) for k, v in resp.headers.multi_items()
                   if k.lower() not in _DROP_RESP_HEADERS]
    if not any(k.lower() == "content-type" for k, _ in out_headers):
        out_headers.append(("content-type", "application/octet-stream"))
    if prefix:
        out_headers = [(k, prefix + v if k.lower() == "location" and v.startswith("/") else v)
                       for k, v in out_headers]

    ct = resp.headers.get("content-type", "")
    if "text/html" in ct and target_path.rstrip("/").split("?")[0].endswith("/ui"):
        # agent 自带观测台页面：根绝对路径 → 前缀路径（最小重写，尽力而为）
        content = rewrite_html(await resp.aread(), agent["id"])
        await resp.aclose()
        return _buf_resp(content, resp.status_code, out_headers)
    if prefix and "text/html" in ct:
        content = rewrite_prefixed(await resp.aread(), prefix)
        await resp.aclose()
        return _buf_resp(content, resp.status_code, out_headers)

    # 流式回传（含压缩原样直传）；BackgroundTask 保证上游连接必被释放
    heads, extra = _split_headers(out_headers)
    out = StreamingResponse(resp.aiter_raw(), status_code=resp.status_code,
                            headers=heads, background=BackgroundTask(resp.aclose))
    out.raw_headers.extend(extra)
    return out


def _split_headers(pairs: list[tuple[str, str]]
                   ) -> tuple[dict[str, str], list[tuple[bytes, bytes]]]:
    """starlette Response 的 headers 参数只收 Mapping（重复键会被 dict 吞）——
    首个键值进 dict，重复键（如多个 set-cookie）以 raw 对返回、事后追加。"""
    first: dict[str, str] = {}
    extra: list[tuple[bytes, bytes]] = []
    for k, v in pairs:
        if k in first:
            extra.append((k.lower().encode("latin-1"), v.encode("latin-1")))
        else:
            first[k] = v
    return first, extra


def _buf_resp(content: bytes, status: int, pairs: list[tuple[str, str]]) -> Response:
    """缓冲路径的响应：去掉 content-encoding（httpx aread 已解压）。"""
    heads, extra = _split_headers(
        [(k, v) for k, v in pairs if k.lower() != "content-encoding"])
    out = Response(content=content, status_code=status, headers=heads)
    out.raw_headers.extend(extra)
    return out


def rewrite_html(html: bytes, agent_id: str) -> bytes:
    """把页面里的根绝对路径 /v1/、/ui/ 改写到 /px/{id}/ 下（带引号的字面量）。"""
    p = f"/px/{agent_id}".encode()
    for q in (b"'", b'"'):
        html = html.replace(q + b"/v1/", q + p + b"/v1/")
        html = html.replace(q + b"/ui/", q + p + b"/ui/")
    return html


def rewrite_prefixed(html: bytes, prefix: str) -> bytes:
    """FastAPI /docs、/redoc 内联 JS 里引用的根路径 "/openapi.json" 字面量 → 前缀路径。
    与 rewrite_html 同一手法（带引号替换，尽力而为）。"""
    p = prefix.encode()
    for q in (b"'", b'"'):
        html = html.replace(q + b"/openapi.json", q + p + b"/openapi.json")
    return html


async def prefix_proxy(request: Request, agent_id: str, sub_path: str) -> Response:
    """/px/{id}/xxx → agent /xxx（鉴权已在 api.px 完成；转发时注入观察台 token）。"""
    agent = registry.get_agent(agent_id)
    if not agent:
        raise HTTPException(404, f"agent 不存在: {agent_id}")
    target = "/" + (sub_path or "")
    inject = agentops.internal_token(agent)
    # 观测台页面加载：页面从浏览器地址栏读 ?token= 存凭证（localStorage），
    # 而注入只作用于后端转发、到不了浏览器 URL —— 所以对没带 token 的页面访问
    # 发 302，把 agent token 补进浏览器地址栏（保留原有 mtoken 等参数）
    if (request.method == "GET" and target.rstrip("/").endswith("/ui")
            and "token" not in request.query_params and inject):
        from urllib.parse import urlencode
        params = dict(request.query_params)
        params["token"] = inject
        from fastapi.responses import RedirectResponse
        return RedirectResponse(str(request.url.path) + "?" + urlencode(params))
    return await forward(request, agent, target, inject_token=inject)


async def service_proxy(request: Request, agent: dict, svc: dict, sub_path: str) -> Response:
    """/svc/{id}/{name}/xxx → 127.0.0.1:{svc.port}{base_path}/xxx（鉴权已在 api.svc 完成）。

    全功能透明转发：任意方法与请求体原样过，方法放行与否由服务自己决定。
    Authorization 一律 replace-or-drop：manifest 声明 token_file 则服务端读取替换
    注入（每次请求实时读，agent 轮换 token 自动跟随），否则删除——客户端的管理面
    token 绝不透传给 agent 自建服务。读超时放宽到 600s（长任务 POST / SSE）。"""
    target = "/" + (sub_path or "")
    if ".." in target.split("/"):
        raise HTTPException(400, "路径不允许包含 ..")
    tok = services.service_token(agent, svc)
    return await forward(request, agent, target,
                         port=svc["port"], base_path=svc.get("base_path") or "",
                         inject_token=tok if tok is not None else "",
                         prefix=f"/svc/{agent['id']}/{svc['name']}",
                         timeout=httpx.Timeout(600.0, connect=5.0))


async def token_routed(request: Request, target_path: str) -> Response:
    """根路径 /v1/*、/ui/*：凭 agent token 定向转发（voidhub 形态，原样透传）。"""
    tok = request.query_params.get("token")
    if not tok:
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            tok = auth[7:].strip()
    if not tok:
        # 无 token：仅放行探活（App 的离线探测语义 = 管理面存活）
        if target_path.rstrip("/") == "/v1/health" and request.method == "GET":
            import json as _json
            from . import __version__, registry as _reg
            body = _json.dumps({"ok": True, "service": "xusi", "version": __version__,
                                "agents": len(_reg.list_agents())}, ensure_ascii=False)
            return Response(content=body, media_type="application/json")
        raise HTTPException(401, "missing token（此入口凭 agent 观察台 token 路由）")
    found = agent_by_agent_token(tok)
    if not found:
        raise HTTPException(401, "unknown token（非本管理面任何 agent 的观察台 token）")
    agent, _ = found
    # 原样透传（含 Authorization 与 ?token=，agent 自行校验）
    return await forward(request, agent, target_path, inject_token=None,
                         drop_params=(), keep_query=True)
