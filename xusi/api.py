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
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from . import __version__, agentops, authtok, backup, brains, capabilities, node, peers, ports, proxy, registry, services, versions
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


@app.exception_handler(versions.VersionError)
async def _version_error(_req: Request, exc: versions.VersionError):
    return Response(content=f'{{"detail": {_json_str(str(exc))}}}',
                    status_code=400, media_type="application/json")


@app.exception_handler(capabilities.CapabilityError)
async def _capability_error(_req: Request, exc: capabilities.CapabilityError):
    return Response(content=f'{{"detail": {_json_str(str(exc))}}}',
                    status_code=400, media_type="application/json")


@app.exception_handler(backup.BackupError)
async def _backup_error(_req: Request, exc: backup.BackupError):
    return Response(content=f'{{"detail": {_json_str(str(exc))}}}',
                    status_code=400, media_type="application/json")


@app.exception_handler(ValueError)
async def _value_error(_req: Request, exc: ValueError):
    """node.set_name 等用户入参校验抛 ValueError，转 400 而非 500。"""
    return Response(content=f'{{"detail": {_json_str(str(exc))}}}',
                    status_code=400, media_type="application/json")


@app.exception_handler(peers.PeerUnreachable)
async def _peer_unreachable(_req: Request, exc: peers.PeerUnreachable):
    """peer 不可达——网络层失败，502 Bad Gateway（参照 proxy.py 内 127.0.0.1 不可达的同码处理）。"""
    return Response(content=f'{{"detail": {_json_str(str(exc))}}}',
                    status_code=502, media_type="application/json")


@app.exception_handler(peers.PeerRefused)
async def _peer_refused(_req: Request, exc: peers.PeerRefused):
    """本地拒绝——单节点模式 / 重名 / url 格式坏。400 Bad Request。"""
    return Response(content=f'{{"detail": {_json_str(str(exc))}}}',
                    status_code=400, media_type="application/json")


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


async def require_agent_or_remote(
    request: Request,
    agent_id: str,
    rec: dict = Depends(require_auth),
) -> tuple["proxy.AgentTarget", dict]:
    """Phase 2 跨节点读端点的鉴权依赖：
    - 作用域检查先于 locality 查询（不泄露远端 agent 的存在性）
    - 本地命中：kind="local"（pair[0].agent == registry 记录）
    - 远端命中：kind="remote"（pair[0].peer == peer 记录；proxy 时由
      proxy.forward_to_peer 透传 caller JWT 到 peer，peer 端重验 + 重 enforce 作用域）
    - 全 miss → 404
    """
    if not authtok.can_access(rec, agent_id):
        raise HTTPException(403, f"token 无权访问 agent {agent_id}")
    target = proxy.resolve(agent_id, rec=rec)
    if target is None:
        raise HTTPException(404, f"agent 不存在: {agent_id}")
    return target, rec


# ── 请求模型 ─────────────────────────────────────────────────────────

class CreateAgentReq(BaseModel):
    name: str = Field(min_length=1, max_length=64, description="显示名（生成 id 用）")
    mission: str = Field(min_length=1, description="长期使命")
    brains: list[str] = Field(min_length=1, description="大脑列表（首个为默认，顺序=故障转移序）")
    expose: bool = Field(False, description="true=监听 0.0.0.0 直接对外；默认 127.0.0.1 仅经反代")
    port: int | None = Field(None, description="指定端口（缺省自动分配，自 8601 起）")
    budgets: dict | None = Field(None, description="预算 {max_rounds, max_seconds, max_context_tokens}")
    note: str = Field("", description="备注")
    source_version: str = Field("", description="xuseek-v2 版本号（GET /api/versions）。缺省 = 仓库最新版"
                                                "（每 agent 自带私有副本，可单独迁移）；'main' = 共享主源码"
                                                "（保留值，过渡期后废弃）。私有副本创建后不可改")


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


class TokenMgrNewReq(BaseModel):
    """管理面 token 签发（仅 admin 可调）。

    rotate=True 时：先 revoke 同 role 的所有 JWT，再签发新的——用户层面始终只
    看见一把 active token；旧的被换掉就立刻作废。PLAIN 不被 rotate 触碰。"""
    label: str = ""
    role: str = Field("user", description="admin 或 user")
    agents: list[str] | None = Field(None, description="user 范围（admin 无需）")
    rotate: bool = Field(False, description="（仅 cluster）签发前先 revoke 同 role 的所有 JWT")


class BackupReq(BaseModel):
    reason: str = Field("manual", description="备份原因（manual/pre-modify/...）；写进 meta")


class RestoreReq(BaseModel):
    from_path: str | None = Field(None, description="备份 tar.gz 本机路径（CLI 用）")
    key: str | None = Field(None, description="备份 key（WebUI 用：从 LocalBackend 取，免下载）")
    new_id: str | None = Field(None, description="恢复后用新 id（避免冲突）")
    port: int | None = Field(None, description="恢复后端口（默认自动分配）")
    host: str = Field("127.0.0.1", description="监听 host")
    overwrite: bool = Field(False, description="覆盖同名已存在 agent")
    brains: list[str] | None = Field(None, description="覆盖备份 meta.brains；克隆对话框用，"
                                                  "让用户从 xusi 大脑池显式选，而不是沿用 meta")
    note: str | None = Field(None, description="覆盖备份 meta.note；克隆对话框用，"
                                           "自动写'从备份克隆于 YYYY-MM-DD'")


class PatchNodeReq(BaseModel):
    """改名（仅 name 可改；id/role 走 toml，API 改不了）。"""
    name: str = Field(min_length=1, max_length=64, description="新显示名")


class AddPeerReq(BaseModel):
    """注册一个 peer；server 会立即探活 {peer.url}/api/peer/id 拿 id。"""
    url: str = Field(min_length=1, description="peer 管理面 url（如 http://10.0.16.15:8601）")
    name: str = Field("", description="显示名（缺省用 peer 自报）")


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


# ── 节点身份（peer 自报 / 改名 / 集群视图）────────────────
# 注意：/api/peer/id **不鉴权**——peer 之间在建立信任之前就要先拿到对方自报；
# 仅返回公开字段（id/name/role/version/url），从不返回 secret/cluster_secret/tokens。

@app.get("/api/peer/id")
def api_peer_id() -> dict:
    """本节点自报（peer 之间 + WebUI 顶栏皆用）。"""
    return node.info()


@app.patch("/api/node")
def api_node_patch(req: PatchNodeReq, _rec: dict = Depends(require_admin)) -> dict:
    """改名。id/role 不让改（id 是机器身份；role 改完要重启，且本来也不该在一行 API 里改）。"""
    node.set_name(req.name)
    return node.info()


@app.get("/api/cluster")
def api_cluster(_rec: dict = Depends(require_auth)) -> dict:
    """集群视图：self + 探活后的 peers[]（每个 peer 含 ok/info/error/latency_ms）。
    前端顶栏的「切换节点下拉」与节点对话框的「其他节点」列表都直接消费本接口。
    单节点模式（cluster_secret 未设）：peers 永远空，不探活。
    排除自己——peer 列表来自共享 toml，集群模式下自己的 id 可能在里头。"""
    me = node.info()
    out = {"self": me, "peers": []}
    if not peers.is_cluster():
        return out
    for p in peers.list_peers():
        if p["id"] == me["id"]:
            continue  # 排除自递归
        r = peers.probe_peer(p)  # 5s TTL 缓存；前端 5s 轮询不会打爆 peer
        entry: dict = {"id": p["id"], "name": p.get("name", ""),
                       "url": p["url"], "ok": r["ok"]}
        if r.get("latency_ms") is not None:
            entry["latency_ms"] = r["latency_ms"]
        if r["ok"]:
            entry["info"] = r["info"]
        else:
            entry["error"] = r.get("error", "")
        out["peers"].append(entry)
    return out


# ── peer 名册 CRUD（Phase 2） ──────────────────────────────────────

@app.get("/api/peers")
def api_peers_list(_rec: dict = Depends(require_auth)) -> dict:
    """列出所有 peer + 探活结果（带 5s TTL）。
    返回 shape 与 /api/cluster.peers 相同；前端若只需要名册而非 self 也用这个。"""
    out = {"cluster": peers.is_cluster(), "peers": []}
    if not peers.is_cluster():
        return out
    for p in peers.list_peers():
        r = peers.probe_peer(p)
        entry = {"id": p["id"], "name": p.get("name", ""),
                 "url": p["url"], "ok": r["ok"]}
        if r.get("latency_ms") is not None:
            entry["latency_ms"] = r["latency_ms"]
        if r["ok"]:
            entry["info"] = r["info"]
        else:
            entry["error"] = r.get("error", "")
        out["peers"].append(entry)
    return out


@app.post("/api/peers", status_code=201)
def api_peers_add(req: AddPeerReq,
                  _rec: dict = Depends(require_admin)) -> dict:
    """注册一个 peer：先探活（拿 id），落 etc/peers.toml。
    失败：peer 不可达 → 502 PeerUnreachable；本地拒绝（单节点模式 / 重名 / url 坏）→ 400 PeerRefused。"""
    rec = peers.add_peer(req.url, name=req.name)  # 抛异常被全局 handler 接住
    r = peers.probe_peer(rec)
    return {
        **rec,
        "ok": r["ok"],
        "latency_ms": r.get("latency_ms"),
        "info": r.get("info") if r["ok"] else None,
        "error": r.get("error") if not r["ok"] else None,
    }


@app.delete("/api/peers/{peer_id}")
def api_peers_remove(peer_id: str,
                     _rec: dict = Depends(require_admin)) -> dict:
    if not peers.remove_peer(peer_id):
        raise HTTPException(404, f"peer 不存在: {peer_id}")
    return {"removed": peer_id}


@app.post("/api/peers/probe")
def api_peers_probe_all(_rec: dict = Depends(require_admin)) -> dict:
    """强制清 5s 探活缓存 + 立即全员重探；前端手动刷新按钮用。"""
    peers.clear_probe_cache()
    rows = peers.list_peers()
    out = []
    for p in rows:
        r = peers.probe_peer(p)
        out.append({"id": p["id"], "url": p["url"],
                    "ok": r["ok"], "latency_ms": r.get("latency_ms"),
                    "error": r.get("error", "") if not r["ok"] else ""})
    return {"probed": len(out), "results": out}


@app.get("/api/brains")
def api_brains(_rec: dict = Depends(require_auth)) -> list[dict]:
    return brains.pool_summary()


@app.get("/api/versions")
def api_versions(_rec: dict = Depends(require_auth)) -> dict:
    """xuseek-v2 版本仓库清单（zip 由管理员投放于 versions/，约定见 docs/versions.md）。
    创建 agent 的 source_version 缺省 = 清单最新版（每 agent 私有副本）。
    'main' = 共享主源码（过渡期字段，新约定不再推荐），其就绪与否见 main_ready。
    default_ready = 版本仓库是否非空（实际默认源 = 仓库最新版）。"""
    return {"repo_dir": str(get_config().versions_dir),
            "default_ready": bool(versions.list_versions()),
            "main_ready": (get_config().source_dir / "xuseek.sh").exists(),
            "versions": versions.list_versions()}


@app.get("/api/ports/available")
def api_ports(count: int = 10, _rec: dict = Depends(require_auth)) -> dict:
    return {"range": [get_config().port_lo, get_config().port_hi],
            "ports": ports.available_ports(max(1, min(count, 50)))}


# ── agent 管理 ───────────────────────────────────────────────────────

@app.get("/api/agents")
async def api_agents_list(rec: dict = Depends(require_auth),
                          local_only: bool = False) -> list[dict]:
    """agent 一览：本地 + 集群模式下 fan-in peer（每行 _via=<peer-id>）。
    单 peer 挂了不影响其他 / 本地——降级展示即可。

    local_only=true 用于 fan-in 中继：只返回本地注册的 agent，不再二次 fan-out。
    关键意义：防止双边注册时的 fan-in 回环。每个节点的"集群视图"是 local +
    direct peers 的 local，不再递归 peers-of-peers。"""
    rows = list(agentops.list_status())
    if not local_only and peers.is_cluster() and authtok.is_admin(rec):
        # 排除自己——peer 列表来自共享 etc/peers.toml，集群模式下自己的 id
        # 也可能在里头（多机器各自 git pull 同一份 toml）；fan-in 到自己 = 自递归。
        me_id = node.info()["id"]
        pls = [p for p in peers.list_peers() if p["id"] != me_id]
        if pls:
            import asyncio
            async def _one(p: dict) -> list[dict]:
                try:
                    # 给 peer 传 local_only=1：peer 也只返回自己的 local，
                    # 不再 fan-in 它自己的 peers——这是双边注册能 work 的关键。
                    r = await proxy.fetch_json(p, "/api/agents?local_only=1",
                                                rec, timeout=5)
                    for row in r:
                        if isinstance(row, dict):
                            row["_via"] = p["id"]
                    return [row for row in r if isinstance(row, dict)]
                except proxy.PeerUnreachable:
                    return []  # 单 peer 挂掉让 list 降级而非 502
                except proxy.PeerHttpError:
                    return []
            results = await asyncio.gather(*[_one(p) for p in pls])
            for r in results:
                rows.extend(r)
    if authtok.is_admin(rec):
        return rows
    return [r for r in rows if authtok.can_access(rec, r["id"])]


async def _agent_or_proxy(target: "proxy.AgentTarget", request: Request,
                          local_handler, rec: dict) -> Response:
    """agent-scoped 读端点统一调度：local 直接调 handler；remote 转发到 peer。
    local_handler 必须接受 (agent_id, ...) 并返回 JSON-serializable dict。
    写路径不用此 helper（v1 范围外）。"""
    if target.kind == "local":
        return JSONResponse(local_handler(target.agent["id"]))
    # remote：透传同 path 到 peer
    sub = request.url.path  # /api/agents/{id} 或 /api/agents/{id}/...
    return await proxy.forward_to_peer(target.peer, request, sub, rec=rec)


@app.post("/api/agents", status_code=201)
def api_agents_create(req: CreateAgentReq, _rec: dict = Depends(require_admin)) -> dict:
    return agentops.create_agent(
        req.name, req.mission, req.brains, expose=req.expose, port=req.port,
        budgets=req.budgets, note=req.note, source_version=req.source_version)


# ── 能力包（只读观察：种子已无条件播入 workspace，启用与否、依赖安装归大脑）──
# 注意：本节必须在 _mk_observe 循环之前注册（同 services 一节的原因）。

@app.get("/api/agents/{agent_id}/capabilities")
async def api_agent_capabilities(request: Request,
                                pair: tuple = Depends(require_agent_or_remote)) -> Response:
    """agent 的能力包清单（只读）。enabled 反映其 config [capabilities] 的
    实况（通常全 false——墟司不写该段；若大脑自行写入亦如实显示）。"""
    target, rec = pair
    if target.kind == "local":
        return JSONResponse(capabilities.list_for_agent(target.agent))
    return await proxy.forward_to_peer(target.peer, request, request.url.path, rec=rec)


@app.get("/api/agents/{agent_id}")
async def api_agent_get(request: Request,
                        pair: tuple = Depends(require_agent_or_remote)) -> Response:
    target, rec = pair
    if target.kind == "local":
        return JSONResponse(agentops.status(target.agent["id"]))
    return await proxy.forward_to_peer(target.peer, request, request.url.path, rec=rec)


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
async def api_services_list(request: Request, probe: bool = True,
                            pair: tuple = Depends(require_agent_or_remote)) -> Response:
    target, rec = pair
    if target.kind == "local":
        return JSONResponse(services.list_services(target.agent, probe=probe))
    return await proxy.forward_to_peer(target.peer, request, request.url.path, rec=rec)


# ── 观察（只读）与投信 ───────────────────────────────────────────────

def _mk_observe(what: str):
    async def _h(request: Request, limit: int = 50,
                 pair: tuple = Depends(require_agent_or_remote)) -> Response:
        target, rec = pair
        if target.kind == "local":
            if what == "logs":
                return JSONResponse(agentops.logs(target.agent["id"], limit))
            return JSONResponse({"id": target.agent["id"], "what": what,
                                 "data": agentops.observe(target.agent["id"], what, limit)})
        # 远程：把 ?limit= 一并透传，peer 端 handler 自己解析
        return await proxy.forward_to_peer(target.peer, request,
                                            request.url.path, rec=rec)
    _h.__name__ = f"api_agent_observe_{what}"
    return app.get(f"/api/agents/{{agent_id}}/{what}")(_h)


for _what in ("status", "events", "sessions", "messages", "outbox", "logs"):
    _mk_observe(_what)


@app.post("/api/agents/{agent_id}/mail")
def api_agent_mail(req: MailReq, pair: tuple = Depends(require_agent)) -> dict:
    return agentops.mail(pair[0]["id"], req.text)


# ── 备份 / 恢复 ───────────────────────────────────────────────────────

@app.post("/api/agents/{agent_id}/backup", status_code=201)
def api_agent_backup(req: "BackupReq", pair: tuple = Depends(require_agent),
                     _rec: dict = Depends(require_admin)) -> dict:
    """备份到 backend（默认 LocalBackend：etc/backups/）。前置：sleeping + grace。"""
    return backup.snapshot(pair[0]["id"], reason=req.reason)


@app.get("/api/agents/{agent_id}/backups")
async def api_agent_backups_list(request: Request, with_meta: bool = False,
                                 pair: tuple = Depends(require_agent_or_remote)) -> Response:
    target, rec = pair
    if target.kind == "local":
        if with_meta:
            return JSONResponse(backup.list_with_meta(agent_id=target.agent["id"]))
        return JSONResponse(backup.list_backups(agent_id=target.agent["id"]))
    return await proxy.forward_to_peer(target.peer, request, request.url.path, rec=rec)


@app.get("/api/backups")
def api_backups_all(with_meta: bool = False,
                     _rec: dict = Depends(require_admin)) -> list[dict]:
    """跨 agent 的全量备份清单（仅 admin）。WebUI 「从备份克隆」走这里。"""
    if with_meta:
        return backup.list_with_meta()
    return backup.list_backups()


@app.get("/api/backups/{key}")
def api_backup_get(key: str, _rec: dict = Depends(require_auth)) -> dict:
    """备份元数据 + 透传包内 meta（不下载包体）。"""
    be = backup.LocalBackend()
    rows = [r for r in be.list() if r["key"] == key]
    if not rows:
        raise HTTPException(404, f"备份不存在：{key}")
    # 读包内 meta
    import tarfile, json as _json, tempfile
    p = be._path(key)
    with tarfile.open(p, "r:gz") as tar:
        for m in tar.getmembers():
            if m.name == "meta.json":
                f = tar.extractfile(m)
                meta = _json.loads(f.read().decode("utf-8"))
                break
        else:
            meta = {}
    return {**rows[0], "meta": meta}


@app.delete("/api/backups/{key}")
def api_backup_delete(key: str, _rec: dict = Depends(require_admin)) -> dict:
    backup.delete_backup(key)
    return {"deleted": key}


@app.post("/api/restore", status_code=201)
def api_restore(req: "RestoreReq", _rec: dict = Depends(require_admin)) -> dict:
    """从备份包恢复。req.key（WebUI）或 req.from_path（CLI）二选一。"""
    from pathlib import Path
    if req.key:
        bp = backup.path_of_key(req.key)
    elif req.from_path:
        bp = Path(req.from_path).expanduser().resolve()
    else:
        raise HTTPException(400, "需要 key 或 from_path 之一")
    return backup.restore(
        bp, new_id=req.new_id, port=req.port, host=req.host,
        overwrite=req.overwrite, brains=req.brains, note=req.note)


# ── 管理面 token（admin / user；可视 / 签发 / 撤销）─────────────────────

@app.get("/api/tokens")
def api_tokens_list(_rec: dict = Depends(require_admin)) -> list[dict]:
    """列出管理面 token。仅 admin——含其他 admin 的 token 原文。

    `kind` 标记 token 形态：
    - `plain`：xusi 统一签发的 PLAIN（用户应持有的形态，跨集群也通——转发时
       xusi 内部自动包成短期 JWT 给 peer）；
    - `jwt`：历史 cluster 自动签发的 JWT 残留（xusi 内部事务，不再签发），
       仍可 verify 通过；UI 不暴露签发/撤销入口，让管理员知道那不是用户 token。"""
    rows = []
    for t in authtok.list_tokens():
        is_jwt = authtok.is_jwt(t["token"])
        rows.append({
            "token": t["token"],
            "label": t["label"],
            "role": t["role"],
            "agents": t["agents"],
            "created_at": t["created_at"],
            "kind": "jwt" if is_jwt else "plain",
        })
    return rows


@app.post("/api/tokens", status_code=201)
def api_tokens_new(req: TokenMgrNewReq,
                   _rec: dict = Depends(require_admin)) -> dict:
    """签发新的管理面 token。仅 admin 可调——admin / user 都得 admin 来签。"""
    if req.role not in ("admin", "user"):
        raise HTTPException(400, "role 须为 admin 或 user")
    try:
        rec = authtok.new_token(req.label, role=req.role,
                                agents=req.agents, rotate=req.rotate)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {
        "token": rec["token"],
        "label": rec["label"],
        "role": rec["role"],
        "agents": rec["agents"],
        "created_at": rec["created_at"],
        "rotated": req.rotate,
    }


@app.delete("/api/tokens/{prefix}")
def api_tokens_revoke(prefix: str,
                      _rec: dict = Depends(require_admin)) -> dict:
    """按前缀撤销管理面 token。仅 admin 可调。"""
    if len(prefix) < 8:
        raise HTTPException(400, "请提供至少 8 位 token 前缀")
    n = authtok.revoke_token(prefix)
    return {"revoked": n, "prefix": prefix}


# ── agent 观察台 token ───────────────────────────────────────────────

@app.get("/api/agents/{agent_id}/tokens")
async def api_tokens_list(request: Request,
                          pair: tuple = Depends(require_agent_or_remote)) -> Response:
    target, rec = pair
    if target.kind == "local":
        return JSONResponse(agentops.tokens_list(target.agent["id"]))
    return await proxy.forward_to_peer(target.peer, request, request.url.path, rec=rec)


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
    import httpx
    from fastapi.responses import StreamingResponse
    from starlette.background import BackgroundTask
    from . import proxy as _proxy
    url = f"{peer['url'].rstrip('/')}{target_path}"
    headers = {k: v for k, v in request.headers.items() if k.lower() not in _proxy._HOP_HEADERS}
    if rec is not None:
        # 用包装/透传后的 Authorization 覆盖 caller 原 header（如果有的话）
        headers.update(proxy.bearer_headers(rec))
    # query 整段透传：peer 端 require_auth / _svc_px_auth 自己读 mtoken + token。
    params = dict(request.query_params)
    body = await request.body()
    try:
        cli = _proxy.client()
        req = cli.build_request(request.method, url, params=params or None,
                                headers=headers, content=body,
                                timeout=httpx.Timeout(30.0, connect=5.0))
        resp = await cli.send(req, stream=True)
    except httpx.HTTPError as e:
        raise HTTPException(502, f"peer {peer['id']} 不可达：{type(e).__name__}: {e}") from e
    out_headers = [(k, v) for k, v in resp.headers.multi_items()
                   if k.lower() not in _proxy._DROP_RESP_HEADERS]
    heads, extra = _proxy._split_headers(out_headers)
    out = StreamingResponse(resp.aiter_raw(), status_code=resp.status_code,
                            headers=heads, background=BackgroundTask(resp.aclose))
    out.raw_headers.extend(extra)
    return out


def _rec_of(request: Request) -> dict | None:
    """从 Request 提取 caller 鉴权记录（verify 失败返 None，不抛 401——上层按业务决定码）。"""
    tok = None
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        tok = auth[7:].strip()
    if not tok:
        tok = request.query_params.get("mtoken")
    return authtok.verify(tok) if tok else None


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
