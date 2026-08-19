"""反代核心：单一对外端口（管理面端口）承载所有 agent 的观察台访问。

两种路由，同一端口：
1. 前缀路由 /px/{agent-id}/* —— 管理面 token 鉴权（admin 或该 agent 范围的
   user），转发时自动注入该 agent 的观察台 token，客户端无需持有第二层 token；
   agent 自带的 /ui/ 页面做最小 HTML 路径重写 + 自动带 token，经代理可用。
2. token 路由（根路径 /v1/*、/ui/*）—— 凭 agent 观察台 token 实时定向到
   所属 agent，原样透传。voidhub App（host+port+token 形态）零改动接入：
   host=服务器IP、port=管理面端口、token=该 agent 的观察台 token。

manager 对 agent 的转发只到 127.0.0.1（无论 agent 是否对外暴露）。
"""
from __future__ import annotations

from typing import Any

import httpx
from fastapi import HTTPException, Request, Response

from . import agentops, registry, services

# 逐跳头：不透传（httpx 自动解压，长度重算）
_HOP_HEADERS = {
    "connection", "keep-alive", "transfer-encoding", "upgrade", "te",
    "trailers", "proxy-authenticate", "proxy-authorization",
    "content-encoding", "content-length", "host",
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
                  prefix: str | None = None) -> Response:
    """把请求转发到 agent 的 127.0.0.1 端口。inject_token 时替换鉴权头。
    port 缺省 agent["port"]（观察台）；自建服务传自己的端口。
    base_path 用于挂在子路径的服务（前拼）；prefix 非 None 时对返回的
    HTML / 根相对 Location 做前缀重写（/svc 场景，让 /docs 这类页面经代理可用）。"""
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
    try:
        resp = await client().request(request.method, url, params=params or None,
                                      headers=headers, content=body)
    except httpx.HTTPError as e:
        raise HTTPException(502, f"上游不可达（agent {agent['id']}，"
                                 f"127.0.0.1:{real_port}）：{type(e).__name__}。"
                                 f"若已暂停（SIGSTOP）属正常现象") from e

    out_headers = {"content-type": resp.headers.get("content-type", "application/octet-stream")}
    for k in ("cache-control", "location"):
        if k in resp.headers:
            out_headers[k] = resp.headers[k]
    content = resp.content

    ct = out_headers["content-type"]
    if "text/html" in ct and target_path.rstrip("/").split("?")[0].endswith("/ui"):
        # agent 自带观测台页面：根绝对路径 → 前缀路径（最小重写，尽力而为）
        content = rewrite_html(content, agent["id"])
    elif prefix and "text/html" in ct:
        content = rewrite_prefixed(content, prefix)
    if prefix and out_headers.get("location", "").startswith("/"):
        out_headers["location"] = prefix + out_headers["location"]

    return Response(content=content, status_code=resp.status_code, headers=out_headers)


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

    Authorization 一律 replace-or-drop：manifest 声明 token_file 则服务端读取替换
    注入（每次请求实时读，agent 轮换 token 自动跟随），否则删除——客户端的管理面
    token 绝不透传给 agent 自建服务。"""
    target = "/" + (sub_path or "")
    if ".." in target.split("/"):
        raise HTTPException(400, "路径不允许包含 ..")
    tok = services.service_token(agent, svc)
    return await forward(request, agent, target,
                         port=svc["port"], base_path=svc.get("base_path") or "",
                         inject_token=tok if tok is not None else "",
                         prefix=f"/svc/{agent['id']}/{svc['name']}")


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
