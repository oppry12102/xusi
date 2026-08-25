"""备份 / 恢复路由。

- POST /api/agents/{id}/backup  支持远端 forward（peer 端用同一 cluster_secret
  verify 后由 peer 自己 backup.snapshot，落到 peer 自己的 etc/backups/）
- POST /api/restore  永远本地（写本机 instances/，与远端无关）
- GET  /api/agents/{id}/backups  支持远端 forward
- GET  /api/backups  本机备份清单（admin-only，与 peer 无关——peer 自己的
  备份清单去 peer 的 WebUI 看）
"""
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from .. import backup, proxy
from .auth import require_admin, require_agent_or_remote, require_agent_or_remote_admin
from .models import BackupReq, RestoreReq

router = APIRouter()


@router.post("/api/agents/{agent_id}/backup", status_code=201)
async def api_agent_backup(request: Request, req: BackupReq,
                           pair: tuple = Depends(require_agent_or_remote_admin)) -> Response:
    """备份到 backend（默认 LocalBackend：etc/backups/）。前置：sleeping + grace。

    远端 agent 走 forward——peer 端自己 snapshot，落 peer 自己的 etc/backups/。
    想拉远端备份到本机：先在 peer 端备份，再用 peer 的 /api/backups/{key} 拉
    tar.gz 流回本机——备份内容走的是路径，不走身份。"""
    target, _rec = pair
    if target.kind == "local":
        return JSONResponse(backup.snapshot(target.agent["id"], reason=req.reason))
    return await proxy.forward_to_peer(target.peer, request, request.url.path)


@router.get("/api/agents/{agent_id}/backups")
async def api_agent_backups_list(request: Request, with_meta: bool = False,
                                 pair: tuple = Depends(require_agent_or_remote)) -> Response:
    target, _rec = pair
    if target.kind == "local":
        if with_meta:
            return JSONResponse(backup.list_with_meta(agent_id=target.agent["id"]))
        return JSONResponse(backup.list_backups(agent_id=target.agent["id"]))
    return await proxy.forward_to_peer(target.peer, request, request.url.path)


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
