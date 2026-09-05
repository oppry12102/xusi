"""管理面 HTTP API（FastAPI 应用入口）。

路由分区（见各 routes 子模块）：
  /api/*        管理 API（管理面 token：Bearer 或 ?mtoken=）
  /             WebUI；/docs Swagger；/api/docs.md 中文文档

与 agent 的唯一写接口是管理邮箱（投信 mailbox.jsonl / 读 outbox.jsonl）；
观察收窄为只读两条 HTTP GET
（/v1/events、/v1/status，详情页事件流/工具统计/会话 banner；观察 token
缺失时自动签发一枚写 data/webui_tokens.json）。本应用不反代、不调 xuseek CLI。
互联是 xuseek 内核自家业务（根智能体 + [[roots]] 出生交割，
见内核 docs/interconnect.md）——xusi 彻底本地化，不参与。

子模块：
- auth.py        鉴权依赖（require_auth / require_admin / require_agent）
- models.py      Pydantic 请求/响应模型
- meta_routes.py    /api/health, /api/whoami, /api/node, /api/brains, /api/versions, /api/ports, /, /api/docs.md
- agent_routes.py   /api/agents/* CRUD + 生命周期 + 投信 + 收信 + 日志 + 只读观察（events/status）+ 会话（sessions，磁盘）
- backup_routes.py  /api/agents/{id}/backup, /api/agents/{id}/backups, /api/backups/*, /api/restore
- hosts_routes.py   /api/hosts（远端机器清单——多副本零管理的机器簿，控制端）
"""
from __future__ import annotations

import json
import threading
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response

from .. import __version__, agentops, backup, remote, versions
from ..systemdctl import SystemdError
from .meta_routes import router as meta_router
from .agent_routes import router as agent_router
from .backup_routes import router as backup_router
from .hosts_routes import router as hosts_router


def _json_str(s: str) -> str:
    return json.dumps(s, ensure_ascii=False)


# ── 启动 / 停止 ─────────────────────────────────────────────────────

@asynccontextmanager
async def _lifespan(_app: FastAPI):
    def _reconcile() -> None:
        time.sleep(1.0)   # 等自身监听就绪后再拉齐
        try:
            report = agentops.reconcile()
            if report:
                print(f"[xusi] reconcile: {report}")
        except Exception as e:
            print(f"[xusi] reconcile 失败：{e}")

    threading.Thread(target=_reconcile, daemon=True, name="xusi-reconcile").start()
    yield


app = FastAPI(
    title="墟司 xusi —— xuseek 智能体管理面",
    description="创建/启停/暂停/删除 xuseek-v2 自主体；唯一写接口是管理邮箱（投信/收信），观察为只读 events/status 两条。",
    version=__version__,
    openapi_url="/api/openapi.json", docs_url="/docs", redoc_url=None,
    lifespan=_lifespan,
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


@app.exception_handler(backup.BackupError)
async def _backup_error(_req: Request, exc: backup.BackupError):
    return Response(content=f'{{"detail": {_json_str(str(exc))}}}',
                    status_code=400, media_type="application/json")


@app.exception_handler(remote.RemoteError)
async def _remote_error(_req: Request, exc: remote.RemoteError):
    """远端机器清单的入参/解析错误 → 400（而非 500）。"""
    return Response(content=f'{{"detail": {_json_str(str(exc))}}}',
                    status_code=400, media_type="application/json")


@app.exception_handler(ValueError)
async def _value_error(_req: Request, exc: ValueError):
    """node.set_name 等用户入参校验抛 ValueError，转 400 而非 500。"""
    return Response(content=f'{{"detail": {_json_str(str(exc))}}}',
                    status_code=400, media_type="application/json")


# ── 路由注册（各子模块的 APIRouter）───────────────────────────────

app.include_router(meta_router)
app.include_router(agent_router)
app.include_router(backup_router)
app.include_router(hosts_router)
