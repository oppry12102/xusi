"""Agent 路由：CRUD + 生命周期（start/stop/pause/resume/restart）+ 投信/收信 + 日志。

按生命周期分：
- CRUD：list / create / get / patch / delete
- 生命周期：5 个 POST /api/agents/{id}/{action}
- 邮箱（唯一的 agent 通信通道）：POST mail 投信 / GET mailbox 收信
- 日志：GET logs（journalctl，进程宿主职责）

单 xusi：所有 agent 都在本机 registry。写端点（PATCH / DELETE / 5 lifecycle /
mail）走 `require_agent`（admin + 本地存在性）。
"""
import asyncio

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

from .. import agentops
from .auth import require_agent, require_admin, require_auth
from .models import CreateAgentReq, MailReq, PatchAgentReq

router = APIRouter()


# ── CRUD ─────────────────────────────────────────────────────────────

@router.get("/api/agents")
async def api_agents_list(_rec: dict = Depends(require_auth)) -> list[dict]:
    """agent 一览（本机 registry）。所有 token 都是 admin——列全量。"""
    return await asyncio.to_thread(agentops.list_status)


@router.post("/api/agents", status_code=201)
def api_agents_create(req: CreateAgentReq, _rec: dict = Depends(require_admin)) -> dict:
    return agentops.create_agent(
        req.name, req.mission, req.brains, expose=req.expose, port=req.port,
        budgets=req.budgets, note=req.note, source_version=req.source_version)


@router.get("/api/agents/{agent_id}")
async def api_agent_get(pair: tuple = Depends(require_agent)) -> JSONResponse:
    agent, _rec = pair
    return JSONResponse(await asyncio.to_thread(agentops.status, agent["id"]))


@router.patch("/api/agents/{agent_id}")
async def api_agent_patch(req: PatchAgentReq, apply_restart: bool = False,
                          pair: tuple = Depends(require_agent)) -> JSONResponse:
    """改 agent 字段（簿记 + 进程层）。apply_restart=1 对 port/expose 立即重启生效。"""
    changes = {k: v for k, v in req.model_dump().items() if v is not None}
    if not changes:
        raise HTTPException(400, "请求体里没有任何要修改的字段")
    agent, _rec = pair
    # patch 含 systemd 子进程；apply_restart 时还有 90s 级验收——
    # 线程池跑，别冻事件循环
    return JSONResponse(await asyncio.to_thread(
        agentops.patch_agent, agent["id"], changes, apply_restart=apply_restart))


@router.delete("/api/agents/{agent_id}")
async def api_agent_delete(pair: tuple = Depends(require_agent)) -> JSONResponse:
    agent, _rec = pair
    # delete 要停单元 + 把整个 home 挪进 .trash（GB 级目录可达分钟）——
    # 线程池跑，别冻事件循环
    return JSONResponse(await asyncio.to_thread(agentops.delete, agent["id"]))


# ── 生命周期（5 个 POST）────────────────────────────────────────────

_LIFECYCLE_ACTIONS = ("start", "stop", "pause", "resume", "restart")


def _lifecycle(agent_id: str, action: str) -> dict:
    fn = {"start": agentops.start, "stop": agentops.stop, "pause": agentops.pause,
          "resume": agentops.resume, "restart": agentops.restart}[action]
    return fn(agent_id)


def _make_lifecycle_handler(action: str):
    async def _h(pair: tuple = Depends(require_agent)) -> JSONResponse:
        agent, _rec = pair
        # 生命周期 = systemd 子进程 + 最长 90s 的验收（wait_health）——
        # 必须丢线程池；在事件循环上同步等会冻住整个管理面
        return JSONResponse(await asyncio.to_thread(
            _lifecycle, agent["id"], action))
    _h.__name__ = f"api_agent_{action}"
    return _h


for _action in _LIFECYCLE_ACTIONS:
    router.post(f"/api/agents/{{agent_id}}/{_action}")(
        _make_lifecycle_handler(_action))


# ── 邮箱（唯一的 agent 通信通道）与日志 ────────────────────────────

@router.post("/api/agents/{agent_id}/mail")
async def api_agent_mail(req: MailReq, pair: tuple = Depends(require_agent)) -> JSONResponse:
    """投信：admin 把一条文字塞进 agent 的 mailbox（唯一的 agent 通信通道）。"""
    agent, _rec = pair
    return JSONResponse(agentops.mail(agent["id"], req.text))


@router.get("/api/agents/{agent_id}/mailbox")
async def api_agent_mailbox(limit: int = 50, box: str = "outbox",
                            pair: tuple = Depends(require_agent)) -> JSONResponse:
    """读邮箱文件尾部：box=outbox 来信（agent→admin，send_mail）；box=inbox
    投信历史（admin→agent，mailbox_log）。

    只读展示；信封（互联发布/目录申请）的自动处理由 mailroom 后台线程完成。"""
    agent, _rec = pair
    return JSONResponse(agentops.mailbox(agent["id"], limit, box=box))


@router.get("/api/agents/{agent_id}/logs")
async def api_agent_logs(limit: int = 200, pair: tuple = Depends(require_agent)) -> JSONResponse:
    """日志：journalctl 该 agent 单元最近 N 行（进程宿主职责，非 agent 接口）。"""
    agent, _rec = pair
    return JSONResponse(await asyncio.to_thread(
        agentops.logs, agent["id"], limit))
