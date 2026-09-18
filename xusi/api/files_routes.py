"""文件路由（详情页「文件」tab）：实例目录只开放 upload/（可写）与
workspace/（只读）——校验与实现全在 files.py（三层共用的那份），
本模块只做 HTTP 包装。远端镜像在 remote_routes.py。

下载/图片直链走 ?mtoken=（auth.py 本就认 query 参数——<a download> 与
<img> 加不了 Authorization 头，与登录流 ?mtoken= 同机制）。raw 的
Content-Type 白名单与 inline 判定见 files.INLINE_TYPES。
"""
from __future__ import annotations

import asyncio
import posixpath
import shutil
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from starlette.background import BackgroundTask

from .. import agentops, files
from .auth import require_agent
from .models import FsMoveReq, FsPathReq, FsWriteReq

router = APIRouter()


@router.get("/api/agents/{agent_id}/fs")
async def fs_list(path: str = "", pair: tuple = Depends(require_agent)) -> JSONResponse:
    """列目录（path 为空 = 根视图：upload + workspace 两个开放根）。"""
    agent, _rec = pair
    return JSONResponse(await asyncio.to_thread(files.list_dir, agent["id"], path))


@router.get("/api/agents/{agent_id}/fs/file")
async def fs_file(path: str = "", mode: str = "text", download: bool = False,
                  pair: tuple = Depends(require_agent)):
    """读文件：mode=text → JSON 预览（截断/二进制探测在 files.read_text）；
    mode=raw → 流式下载（位图白名单内可 inline，其余 attachment 落盘）。"""
    agent, _rec = pair
    if mode != "text":
        info = await asyncio.to_thread(files.open_raw, agent["id"], path)
        ctype = files.INLINE_TYPES.get(info["path"].suffix.lower(),
                                       "application/octet-stream")
        inline = info["inline"] and not download
        resp = FileResponse(info["path"], media_type=ctype,
                            filename=info["name"],
                            content_disposition_type="inline" if inline else "attachment")
    else:
        resp = JSONResponse(await asyncio.to_thread(
            files.read_text, agent["id"], path))
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Cache-Control"] = "no-store"
    return resp


@router.get("/api/agents/{agent_id}/fs/zip")
async def fs_zip(path: str = "", pair: tuple = Depends(require_agent)) -> FileResponse:
    """目录打 zip 下载（临时文件流出后由后台任务清理）。"""
    agent, _rec = pair
    arc, tmpdir = await asyncio.to_thread(files.zip_dir, agent["id"], path)
    fname = (Path(path.strip("/")).name or "bundle") + ".zip"
    resp = FileResponse(arc, media_type="application/zip", filename=fname,
                        background=BackgroundTask(shutil.rmtree, tmpdir, True))
    resp.headers["Cache-Control"] = "no-store"
    return resp


@router.post("/api/agents/{agent_id}/fs/upload")
async def fs_upload(path: str = "", overwrite: bool = False,
                    files_in: list[UploadFile] = File(..., alias="files"),
                    pair: tuple = Depends(require_agent)) -> JSONResponse:
    """multipart 多文件上传（只收 upload/ 目录）：逐个 basename 化、原子
    落盘；重名不覆盖时跳过并在 skipped 里回报（前端确认后带 overwrite=1
    重传被跳过的）。"""
    agent, _rec = pair
    if not files_in:
        raise HTTPException(400, "没有收到文件")
    return JSONResponse(await asyncio.to_thread(
        _store_uploads, agent["id"], path, files_in, overwrite))


def _store_uploads(agent_id: str, dir_rel: str, files_in: list, overwrite: bool) -> dict:
    """UploadFile 列表 → files.put_bytes 循环（在线程池里跑，别冻事件循环）。
    dir_rel 是目标目录（相对路径，首段须 upload/）；文件名只取 basename——
    浏览器（尤其 Windows）可能给带路径的 filename。"""
    saved, skipped = [], []
    for f in files_in:
        name = posixpath.basename(f.filename or "").strip()
        if not name or name in (".", ".."):
            skipped.append(f.filename or "(空名)")
            continue
        data = f.file.read()
        try:
            rel = posixpath.join(dir_rel or files.WRITE_ROOT, name)
            files.put_bytes(agent_id, rel, data, overwrite=overwrite)
            saved.append({"name": name, "size": len(data)})
        except (agentops.AgentError, OSError) as e:
            skipped.append(f"{name}（{e}）")
    return {"id": agent_id, "path": dir_rel, "uploaded": saved, "skipped": skipped}


@router.post("/api/agents/{agent_id}/fs/mkdir")
async def fs_mkdir(req: FsPathReq, pair: tuple = Depends(require_agent)) -> JSONResponse:
    agent, _rec = pair
    return JSONResponse(await asyncio.to_thread(files.mkdir, agent["id"], req.path))


@router.post("/api/agents/{agent_id}/fs/write")
async def fs_write(req: FsWriteReq, pair: tuple = Depends(require_agent)) -> JSONResponse:
    """新建/保存文本文件（在线编辑的保存腿；只收 upload/ 路径）。"""
    agent, _rec = pair
    return JSONResponse(await asyncio.to_thread(
        files.write_text, agent["id"], req.path, req.text))


@router.post("/api/agents/{agent_id}/fs/move")
async def fs_move(req: FsMoveReq, pair: tuple = Depends(require_agent)) -> JSONResponse:
    agent, _rec = pair
    return JSONResponse(await asyncio.to_thread(
        files.move, agent["id"], req.path, req.new_path))


@router.delete("/api/agents/{agent_id}/fs")
async def fs_delete(path: str = "", pair: tuple = Depends(require_agent)) -> JSONResponse:
    """软删（挪 etc/.trash/fs/<agent-id>/，误删可捞回；拒删 upload/ 根）。"""
    agent, _rec = pair
    return JSONResponse(await asyncio.to_thread(files.delete, agent["id"], path))
