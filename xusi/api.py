"""管理面 HTTP API（FastAPI）。

路由分区：
  /api/*        管理 API（管理面 token：Bearer 或 ?mtoken=）
  /px/{id}/*    前缀反代（管理面 token → 注入 agent 观察台 token）
  /svc          凭 token 的服务发现（agent 观察台 token 或管理面 token）
  /svc/{id}/{服务名}/*  agent 自建服务全功能反代（任意方法；写方法入审计）
  /v1/* /ui/*   根路径 token 路由（agent 观察台 token → 定向转发，voidhub 直用）
  /             WebUI；/docs Swagger；/api/docs.md 中文文档
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from . import __version__, agentops, authtok, brains, ports, proxy, registry, services
from .config import get_config
from .systemdctl import SystemdError

app = FastAPI(
    title="墟司 xusi —— xuseek 智能体管理面",
    description="创建/启停/暂停/改参/观察/删除 xuseek-v2 自主体；单一对外端口反代各 agent 观察台。",
    version=__version__,
    openapi_url="/api/openapi.json", docs_url="/docs", redoc_url=None,
)


@app.exception_handler(agentops.AgentError)
async def _agent_error(_req: Request, exc: agentops.AgentError):
    return Response(content=f'{{"detail": {_json_str(str(exc))}}}',
                    status_code=400, media_type="application/json")


@app.exception_handler(SystemdError)
async def _systemd_error(_req: Request, exc: SystemdError):
    return Response(content=f'{{"detail": {_json_str(str(exc))}}}',
                    status_code=500, media_type="application/json")


def _json_str(s: str) -> str:
    import json
    return json.dumps(s, ensure_ascii=False)


# ── 启动/停止：后台 reconcile（掉线保护第二层）──────────────────────

@app.on_event("startup")
async def _startup() -> None:
    def _run() -> None:
        time.sleep(1.0)   # 等自身监听就绪后再拉齐
        try:
            report = agentops.reconcile()
            if report:
                print(f"[xusi] reconcile: {report}")
        except Exception as e:
            print(f"[xusi] reconcile 失败：{e}")
    threading.Thread(target=_run, daemon=True, name="xusi-reconcile").start()


@app.on_event("shutdown")
async def _shutdown() -> None:
    await proxy.close()


# ── 鉴权依赖 ─────────────────────────────────────────────────────────

def require_auth(request: Request) -> dict:
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


# ── 请求模型 ─────────────────────────────────────────────────────────

class CreateAgentReq(BaseModel):
    name: str = Field(min_length=1, max_length=64, description="显示名（生成 id 用）")
    mission: str = Field(min_length=1, description="长期使命")
    brains: list[str] = Field(min_length=1, description="大脑列表（首个为默认，顺序=故障转移序）")
    expose: bool = Field(False, description="true=监听 0.0.0.0 直接对外；默认 127.0.0.1 仅经反代")
    port: int | None = Field(None, description="指定端口（缺省自动分配，自 8601 起）")
    budgets: dict | None = Field(None, description="预算 {max_rounds, max_seconds, max_context_tokens}")
    note: str = Field("", description="备注")


class PatchAgentReq(BaseModel):
    name: str | None = None
    mission: str | None = None
    brains: list[str] | None = None
    budgets: dict | None = None
    expose: bool | None = None
    port: int | None = None
    note: str | None = None


class MailReq(BaseModel):
    text: str = Field(min_length=1)


class TokenNewReq(BaseModel):
    label: str = ""


# ── 元信息 ───────────────────────────────────────────────────────────

def _health() -> dict:
    return {"ok": True, "service": "xusi", "version": __version__,
            "agents": len(registry.list_agents()),
            "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}


@app.get("/api/health")
def api_health() -> dict:
    return _health()


@app.get("/api/whoami")
def api_whoami(rec: dict = Depends(require_auth)) -> dict:
    return {"label": rec["label"], "role": rec["role"], "agents": rec["agents"]}


@app.get("/api/brains")
def api_brains(_rec: dict = Depends(require_auth)) -> list[dict]:
    return brains.pool_summary()


@app.get("/api/ports/available")
def api_ports(count: int = 10, _rec: dict = Depends(require_auth)) -> dict:
    return {"range": [get_config().port_lo, get_config().port_hi],
            "ports": ports.available_ports(max(1, min(count, 50)))}


# ── agent 管理 ───────────────────────────────────────────────────────

@app.get("/api/agents")
def api_agents_list(rec: dict = Depends(require_auth)) -> list[dict]:
    rows = agentops.list_status()
    if authtok.is_admin(rec):
        return rows
    return [r for r in rows if authtok.can_access(rec, r["id"])]


@app.post("/api/agents", status_code=201)
def api_agents_create(req: CreateAgentReq, _rec: dict = Depends(require_admin)) -> dict:
    return agentops.create_agent(
        req.name, req.mission, req.brains, expose=req.expose, port=req.port,
        budgets=req.budgets, note=req.note)


@app.get("/api/agents/{agent_id}")
def api_agent_get(pair: tuple = Depends(require_agent)) -> dict:
    return agentops.status(pair[0]["id"])


@app.patch("/api/agents/{agent_id}")
def api_agent_patch(req: PatchAgentReq, apply_restart: bool = False,
                    pair: tuple = Depends(require_agent),
                    _rec: dict = Depends(require_admin)) -> dict:
    changes = {k: v for k, v in req.model_dump().items() if v is not None}
    if not changes:
        raise HTTPException(400, "请求体里没有任何要修改的字段")
    return agentops.patch_agent(pair[0]["id"], changes, apply_restart=apply_restart)


@app.delete("/api/agents/{agent_id}")
def api_agent_delete(pair: tuple = Depends(require_agent),
                     _rec: dict = Depends(require_admin)) -> dict:
    return agentops.delete(pair[0]["id"])


# ── 生命周期 ─────────────────────────────────────────────────────────

def _lifecycle(agent_id: str, action: str) -> dict:
    fn = {"start": agentops.start, "stop": agentops.stop, "pause": agentops.pause,
          "resume": agentops.resume, "restart": agentops.restart}[action]
    return fn(agent_id)


def _mk_lifecycle(action: str):
    def _h(pair: tuple = Depends(require_agent), _rec: dict = Depends(require_admin)) -> dict:
        return _lifecycle(pair[0]["id"], action)
    _h.__name__ = f"api_agent_{action}"
    return app.post(f"/api/agents/{{agent_id}}/{action}")(_h)


for _action in ("start", "stop", "pause", "resume", "restart"):
    _mk_lifecycle(_action)


# ── agent 自建服务（services.json 发现；管理面只读，不代登记不代改）──
# 注意：本节必须在上面 _mk_observe 循环注册之前定义——Starlette 按注册顺序
# 匹配，GET /api/agents/{id}/services 与 GET /api/agents/{id}/{what} 同形，
# 定义在后就走不通了。

@app.get("/api/agents/{agent_id}/services")
def api_services_list(probe: bool = True, pair: tuple = Depends(require_agent)) -> dict:
    return services.list_services(pair[0], probe=probe)


# ── 观察（只读）与投信 ───────────────────────────────────────────────

def _mk_observe(what: str):
    def _h(limit: int = 50, pair: tuple = Depends(require_agent)) -> dict:
        if what == "logs":
            return agentops.logs(pair[0]["id"], limit)
        return {"id": pair[0]["id"], "what": what,
                "data": agentops.observe(pair[0]["id"], what, limit)}
    _h.__name__ = f"api_agent_observe_{what}"
    return app.get(f"/api/agents/{{agent_id}}/{what}")(_h)


for _what in ("status", "events", "sessions", "messages", "outbox", "logs"):
    _mk_observe(_what)


@app.post("/api/agents/{agent_id}/mail")
def api_agent_mail(req: MailReq, pair: tuple = Depends(require_agent)) -> dict:
    return agentops.mail(pair[0]["id"], req.text)


# ── agent 观察台 token ───────────────────────────────────────────────

@app.get("/api/agents/{agent_id}/tokens")
def api_tokens_list(pair: tuple = Depends(require_agent)) -> list[dict]:
    return agentops.tokens_list(pair[0]["id"])


@app.post("/api/agents/{agent_id}/tokens", status_code=201)
def api_token_new(req: TokenNewReq, pair: tuple = Depends(require_agent),
                  _rec: dict = Depends(require_admin)) -> dict:
    return agentops.token_new(pair[0]["id"], req.label)


@app.delete("/api/agents/{agent_id}/tokens/{prefix}")
def api_token_revoke(prefix: str, pair: tuple = Depends(require_agent),
                     _rec: dict = Depends(require_admin)) -> dict:
    return agentops.token_revoke(pair[0]["id"], prefix)


# ── 反代 ─────────────────────────────────────────────────────────────

def _svc_px_auth(request: Request, agent: dict) -> None:
    """/px 与 /svc 共用鉴权（二选一）：
    ① 管理面 token（admin 或该 agent 范围的 user）；
    ② 该 agent 自己的观察台 token（让 agent 自带页面/仅持观察台 token 的
       外部客户端如 voidhub App 也能通行）。"""
    tok = request.query_params.get("mtoken")
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        tok = auth[7:].strip() or tok
    rec = authtok.verify(tok) if tok else None
    if rec:
        if not authtok.can_access(rec, agent["id"]):
            raise HTTPException(403, f"token 无权访问 agent {agent['id']}")
    elif not tok or tok not in agentops.read_agent_tokens(agent):
        raise HTTPException(401, "missing or invalid token（管理面 token 或该 agent 的观察台 token）")


@app.api_route("/px/{agent_id}/{sub_path:path}", methods=["GET", "POST", "PUT",
                  "DELETE", "PATCH", "HEAD", "OPTIONS"])
async def px(request: Request, agent_id: str, sub_path: str = "") -> Response:
    """前缀反代。鉴权二选一（见 _svc_px_auth）：
    ① 管理面 token（admin 或该 agent 范围的 user）——转发时自动注入 agent token；
    ② 该 agent 自己的观察台 token——让 agent 自带观测台页面在新标签页里
       （拿不到管理面 token 的上下文）发出的 Bearer 请求也能通行。"""
    agent = registry.get_agent(agent_id)
    if not agent:
        raise HTTPException(404, f"agent 不存在: {agent_id}")
    _svc_px_auth(request, agent)
    return await proxy.prefix_proxy(request, agent_id, sub_path)


@app.get("/svc")
def svc_discover(request: Request, probe: bool = False) -> dict:
    """服务发现：凭 token 找到服务入口，无需预知 agent-id / 服务名
    （App 形态只有 IP+端口+token，正是这个入口的受众）。
    agent 观察台 token → 仅该 agent；管理面 token → admin 全部 / user 范围内。
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
            agents = [a for a in registry.list_agents()
                      if authtok.can_access(rec, a["id"])]
            return {"agents": [_entry(a) for a in agents]}
    raise HTTPException(401, "missing or invalid token（管理面 token 或 agent 观察台 token）")


@app.api_route("/svc/{agent_id}/{svc_name}/{sub_path:path}",
               methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD",
                        "OPTIONS"])
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


@app.api_route("/v1/{sub_path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def root_v1(request: Request, sub_path: str = "") -> Response:
    return await proxy.token_routed(request, "/v1/" + sub_path)


@app.api_route("/ui/{sub_path:path}", methods=["GET", "POST"])
async def root_ui(request: Request, sub_path: str = "") -> Response:
    return await proxy.token_routed(request, "/ui/" + sub_path)


# ── 静态：WebUI 与文档 ───────────────────────────────────────────────

@app.get("/")
def index() -> FileResponse:
    return FileResponse(get_config().webui_dir / "index.html", media_type="text/html")


@app.get("/api/docs.md")
def api_docs_md() -> FileResponse:
    p: Path = get_config().docs_dir / "api.md"
    if not p.exists():
        raise HTTPException(404, "docs/api.md 未生成")
    return FileResponse(p, media_type="text/markdown")
