"""管理面 HTTP API（FastAPI 应用入口）。

路由分区（见各 routes 子模块）：
  /api/*        管理 API（管理面 token：Bearer 或 ?mtoken=）
  /px/{id}/*    前缀反代（管理面 token → 注入 agent 观察台 token）
  /svc          凭 token 的服务发现（agent 观察台 token 或管理面 token）
  /svc/{id}/{服务名}/*  agent 自建服务全功能反代（任意方法；写方法入审计）
  /v1/* /ui/*   根路径 token 路由（agent 观察台 token → 定向转发，voidhub 直用）
  /             WebUI；/docs Swagger；/api/docs.md 中文文档

子模块：
- auth.py       鉴权依赖（require_auth/admin/agent/agent_or_remote + _rec_of）
- models.py     Pydantic 请求/响应模型
- meta_routes.py    /api/health, /api/whoami, /api/peer/id, /api/node, /api/cluster, /api/brains, /api/versions, /api/ports, /, /api/docs.md
- peer_routes.py    /api/peers/*
- token_routes.py   /api/tokens/*, /api/agents/{id}/tokens/*
- agent_routes.py   /api/agents/* CRUD + 生命周期 + 观察 + 投信 + capabilities + services
- backup_routes.py  /api/agents/{id}/backup, /api/agents/{id}/backups, /api/backups/*, /api/restore
- proxy_routes.py   /px, /svc, /v1, /ui + 远端 /px 专用转发辅助
"""
from __future__ import annotations

import json
import threading
import time

from fastapi import FastAPI, Request, Response

from .. import __version__, agentops, backup, capabilities, peers, proxy, versions
from ..systemdctl import SystemdError
from .meta_routes import router as meta_router
from .peer_routes import router as peer_router
from .token_routes import router as token_router
from .agent_routes import router as agent_router
from .backup_routes import router as backup_router
from .proxy_routes import router as proxy_router


def _json_str(s: str) -> str:
    return json.dumps(s, ensure_ascii=False)


app = FastAPI(
    title="墟司 xusi —— xuseek 智能体管理面",
    description="创建/启停/暂停/改参/观察/删除 xuseek-v2 自主体；单一对外端口反代各 agent 观察台。",
    version=__version__,
    openapi_url="/api/openapi.json", docs_url="/docs", redoc_url=None,
)


# ── 全局异常 handler（统一 JSON 错误响应）───────────────────────

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


# ── 启动 / 停止 ─────────────────────────────────────────────────────

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


# ── 路由注册（各子模块的 APIRouter）───────────────────────────────

app.include_router(meta_router)
app.include_router(peer_router)
app.include_router(token_router)
app.include_router(agent_router)
app.include_router(backup_router)
app.include_router(proxy_router)
