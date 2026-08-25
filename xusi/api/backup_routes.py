"""备份 / 恢复路由。"""
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from .. import agentops, backup, proxy
from .auth import require_admin, require_agent, require_agent_or_remote
from .models import BackupReq, RestoreReq

router = APIRouter()


@router.post("/api/agents/{agent_id}/backup", status_code=201)
def api_agent_backup(req: BackupReq, pair: tuple = Depends(require_agent),
                     _rec: dict = Depends(require_admin)) -> dict:
    """备份到 backend（默认 LocalBackend：etc/backups/）。前置：sleeping + grace。"""
    return backup.snapshot(pair[0]["id"], reason=req.reason)


@router.get("/api/agents/{agent_id}/backups")
async def api_agent_backups_list(request: Request, with_meta: bool = False,
                                 pair: tuple = Depends(require_agent_or_remote)) -> Response:
    target, rec = pair
    if target.kind == "local":
        if with_meta:
            return JSONResponse(backup.list_with_meta(agent_id=target.agent["id"]))
        return JSONResponse(backup.list_backups(agent_id=target.agent["id"]))
    return await proxy.forward_to_peer(target.peer, request, request.url.path, rec=rec)


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
        bp, new_id=req.new_id, port=req.port, host=req.host,
        overwrite=req.overwrite, brains=req.brains, note=req.note)
