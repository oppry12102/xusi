"""备份 / 恢复路由（全部本地——单 xusi）。

- POST /api/agents/{id}/backup
- POST /api/restore  永远本地（写本机 instances/）
- GET  /api/agents/{id}/backups
- GET  /api/backups  本机备份清单（admin-only）
"""
from pathlib import Path

import asyncio

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

from .. import backup
from .auth import require_agent, require_admin
from .models import BackupReq, RestoreReq

router = APIRouter()


@router.post("/api/agents/{agent_id}/backup", status_code=201)
async def api_agent_backup(req: BackupReq,
                           pair: tuple = Depends(require_agent)) -> JSONResponse:
    """备份到 backend（默认 LocalBackend：etc/backups/）。"""
    agent, _rec = pair
    # snapshot 含 SIGSTOP 冻结窗 + 双遍 tar（分钟级）——线程池跑，
    # 别冻事件循环（否则备份期间管理面请求一起卡）
    return JSONResponse(await asyncio.to_thread(
        backup.snapshot, agent["id"], reason=req.reason))


@router.get("/api/agents/{agent_id}/backups")
async def api_agent_backups_list(with_meta: bool = False,
                                 pair: tuple = Depends(require_agent)) -> JSONResponse:
    agent, _rec = pair
    # with_meta 要逐包开 tar 读头——线程池跑
    if with_meta:
        return JSONResponse(await asyncio.to_thread(
            backup.list_with_meta, agent["id"]))
    return JSONResponse(await asyncio.to_thread(
        backup.list_backups, agent["id"]))


@router.get("/api/backups")
def api_backups_all(with_meta: bool = False,
                    _rec: dict = Depends(require_admin)) -> list[dict]:
    """跨 agent 的全量备份清单（仅 admin）。WebUI 「从备份克隆」走这里。"""
    if with_meta:
        return backup.list_with_meta()
    return backup.list_backups()


@router.get("/api/backups/{key}")
def api_backup_get(key: str, _rec: dict = Depends(require_admin)) -> dict:
    """备份元数据 + 透传包内 meta（不下载包体）。"""
    be = backup.LocalBackend()
    rows = [r for r in be.list() if r["key"] == key]
    if not rows:
        raise HTTPException(404, f"备份不存在：{key}")
    # 读包内 meta
    import tarfile, json as _json
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


@router.delete("/api/backups/{key}")
def api_backup_delete(key: str, _rec: dict = Depends(require_admin)) -> dict:
    backup.delete_backup(key)
    return {"deleted": key}


@router.post("/api/restore", status_code=201)
def api_restore(req: RestoreReq, _rec: dict = Depends(require_admin)) -> dict:
    """从备份包恢复。req.key（WebUI）或 req.from_path（CLI）二选一。"""
    if req.key:
        bp = backup.path_of_key(req.key)
    elif req.from_path:
        bp = Path(req.from_path).expanduser().resolve()
    else:
        raise HTTPException(400, "需要 key 或 from_path 之一")
    return backup.restore(
        bp, new_id=req.new_id, port=req.port,
        overwrite=req.overwrite, brains=req.brains, note=req.note)
