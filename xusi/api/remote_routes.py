"""远端机器操作路由（WebUI 远程总控的 HTTP 面，讨论稿十四节）。

浏览器只与控制端 :8601 说话：所有远端操作由 serve 中转 ssh/scp
（asyncio.to_thread + remote.py 原语）——远端零管理形态不破（无 serve、
无端口、无监听）。全部端点 admin 鉴权；远端操作写控制端 audit。

刻意不做：/v1/events 事件流反代（观察台直连，observe-token 端点供 token）；
任务队列（长操作同步等待，前端 spinner）。
"""
from __future__ import annotations

import asyncio
import json
import re
import shutil
import tempfile
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from .. import agentops, files, remote
from ..config import get_config
from .auth import require_admin
from .models import (CreateAgentReq, FsMoveReq, FsPathReq, FsWriteReq, MailReq,
                     PatchAgentReq, RemoteRestoreReq)

router = APIRouter()

_OPS = ("start", "stop", "pause", "resume", "restart", "delete")


def _host(name: str) -> dict:
    try:
        return remote.find_host(name)
    except remote.RemoteError as e:
        raise HTTPException(400, str(e))


def _check(cp, what: str) -> None:
    if cp.returncode != 0:
        out = (cp.stderr or cp.stdout or "").strip()[-500:]
        raise HTTPException(400, f"{what}失败：{out}")


@router.get("/api/remote/status")
async def api_remote_status(host: str | None = Query(None),
                            _rec: dict = Depends(require_admin)) -> dict:
    """单机/全队 agent 状态（并行 ssh fan-out）；每条带 installed 标志
    （远端自洽目录是否存在——WebUI 未接入空态用）。"""
    hosts = [_host(host)] if host else remote.load_hosts(missing_ok=True)
    if not hosts:
        return {"results": []}

    results = await asyncio.to_thread(remote.fan_out, remote.remote_status, hosts)
    return {"results": results}


@router.post("/api/remote/agents")
async def api_remote_create(req: CreateAgentReq, host: str = Query(...),
                            _rec: dict = Depends(require_admin)) -> dict:
    """创建远端 agent：body 与本地创建完全同构——serve 写临时 spec.json →
    scp → 远端 `create --spec --json`（零新远端代码）。同步等待（首启装依赖
    1-3 分钟）。"""
    h = _host(host)
    with tempfile.TemporaryDirectory(prefix="xusi-spec-") as d:
        spec = Path(d) / "spec.json"
        spec.write_text(json.dumps(req.model_dump(), ensure_ascii=False),
                        encoding="utf-8")
        cp = await asyncio.to_thread(remote.remote_create, h,
                                     ["--spec", str(spec), "--json"], timeout=900)
    _check(cp, "远端创建")
    try:
        rec = json.loads(cp.stdout)
    except Exception:
        rec = {}
    agentops.audit("remote.create", host=h.get("name", host),
                   agent=rec.get("id", ""), name=rec.get("name", ""))
    return {"ok": True, "host": h.get("name", host), "agent": rec}


@router.get("/api/remote/agents/{agent_id}")
async def api_remote_agent(agent_id: str, host: str = Query(...),
                           _rec: dict = Depends(require_admin)) -> dict:
    """单个远端 agent 簿记 + 进程态（详情抽屉「状态」tab）。"""
    h = _host(host)
    res = await asyncio.to_thread(remote.remote_status, h)
    if "error" in res:
        raise HTTPException(400, res["error"])
    for row in res.get("rows", []):
        if row.get("id") == agent_id:
            return row
    raise HTTPException(404, f"远端 {h.get('name', host)} 上没有 agent {agent_id}")


@router.patch("/api/remote/agents/{agent_id}")
async def api_remote_patch(agent_id: str, req: PatchAgentReq, host: str = Query(...),
                           apply_restart: bool = False,
                           _rec: dict = Depends(require_admin)) -> dict:
    """远端改参（以 brains 切换为主）：inline python 直调远端
    agentops.patch_agent——与本地 PATCH 同一条实现，门控/白名单/手术重渲染
    全部同源。expose 变更可带 ?apply_restart=true 立即换监听参数重启。"""
    h = _host(host)
    changes = req.model_dump(exclude_none=True)
    if not changes:
        raise HTTPException(400, "至少给一个可改字段")
    try:
        r = await asyncio.to_thread(remote.remote_patch, h, agent_id, changes,
                                    apply_restart=apply_restart)
    except remote.RemoteError as e:
        raise HTTPException(400, str(e)) from None
    agentops.audit("remote.patch", host=h.get("name", host), agent=agent_id,
                   fields=sorted(changes))
    return r


@router.post("/api/remote/agents/{agent_id}/mail")
async def api_remote_mail(agent_id: str, req: MailReq, host: str = Query(...),
                          _rec: dict = Depends(require_admin)) -> dict:
    """投信（与 agent 的唯一写通道）。"""
    h = _host(host)
    cp = await asyncio.to_thread(remote.remote_agent_op, h, "mail",
                                 [agent_id, req.text])
    _check(cp, "投信")
    return {"ok": True}


@router.get("/api/remote/agents/{agent_id}/mailbox")
async def api_remote_mailbox(agent_id: str, host: str = Query(...),
                             limit: int = 50, box: str = "outbox",
                             _rec: dict = Depends(require_admin)) -> dict:
    """收信（远端 CLI mailbox --json）。"""
    h = _host(host)
    cp = await asyncio.to_thread(remote.remote_agent_op, h, "mailbox",
                                 [agent_id, "--json", "--limit", str(limit),
                                  "--box", box])
    _check(cp, "收信")
    try:
        return json.loads(cp.stdout)
    except Exception:
        raise HTTPException(502, "远端输出不是 JSON（远端版本过旧？先 remote upgrade）")


@router.get("/api/remote/agents/{agent_id}/sessions")
async def api_remote_sessions(agent_id: str, host: str = Query(...),
                              limit: int = 30,
                              _rec: dict = Depends(require_admin)) -> dict:
    """会话索引：远端 CLI sessions --json（事实账 session_end 行——文件通道，
    不反代）。"""
    h = _host(host)
    cp = await asyncio.to_thread(remote.remote_agent_op, h, "sessions",
                                 [agent_id, "--limit", str(limit)])
    _check(cp, "会话索引")
    try:
        return json.loads(cp.stdout)
    except Exception:
        raise HTTPException(502, "远端输出不是 JSON（远端版本过旧？先 remote upgrade）")


@router.get("/api/remote/agents/{agent_id}/sessions/{session_id}")
async def api_remote_session_detail(agent_id: str, session_id: str,
                                    host: str = Query(...),
                                    _rec: dict = Depends(require_admin)) -> dict:
    """单口呼吸完整总结：远端 CLI sessions <sid>（读存档 result 段——文件通道，
    不反代；与本地 /api/agents/{id}/sessions/{sid} 同构）。"""
    h = _host(host)
    cp = await asyncio.to_thread(remote.remote_agent_op, h, "sessions",
                                 [agent_id, session_id])
    _check(cp, "会话详情")
    try:
        return json.loads(cp.stdout)
    except Exception:
        raise HTTPException(502, "远端输出不是 JSON（远端版本过旧？先 remote upgrade）")


@router.get("/api/remote/agents/{agent_id}/events")
async def api_remote_events(agent_id: str, host: str = Query(...),
                            limit: int = 80,
                            _rec: dict = Depends(require_admin)) -> dict:
    """事件流：远端 CLI events（与本地观察通道同一条 agentops.observe 实现——
    ssh 过去在远端本机 curl 127.0.0.1，token 现取，agent 端口零暴露）。
    一次性快照（内存环形缓冲，进程重启清零），不是直播。"""
    h = _host(host)
    cp = await asyncio.to_thread(remote.remote_agent_op, h, "events",
                                 [agent_id, "--limit", str(limit)])
    _check(cp, "读事件流")
    try:
        return json.loads(cp.stdout)
    except Exception:
        raise HTTPException(502, "远端输出不是 JSON（远端版本过旧？先 remote upgrade）")


@router.get("/api/remote/agents/{agent_id}/boot")
async def api_remote_boot(agent_id: str, host: str = Query(...),
                          _rec: dict = Depends(require_admin)) -> dict:
    """Boot 自述：远端 CLI boot（读磁盘 workspace/BOOT.md，agent 停机也能看）。"""
    h = _host(host)
    cp = await asyncio.to_thread(remote.remote_agent_op, h, "boot", [agent_id])
    _check(cp, "读 BOOT.md")
    try:
        return json.loads(cp.stdout)
    except Exception:
        raise HTTPException(502, "远端输出不是 JSON（远端版本过旧？先 remote upgrade）")


@router.post("/api/remote/agents/{agent_id}/observe-token")
async def api_remote_observe_token(agent_id: str, host: str = Query(...),
                                   new: bool = False,
                                   _rec: dict = Depends(require_admin)) -> dict:
    """取观察台 token（卡片「观察台 ↗」直连用——agent 自家业务，xusi 不反代）。"""
    h = _host(host)
    argv = [agent_id, "--json"] + (["--new"] if new else [])
    cp = await asyncio.to_thread(remote.remote_agent_op, h, "observe-token", argv)
    _check(cp, "签发观察 token")
    # 观察台 URL 由前端拼接（hosts 清单的 host + 卡片 port），这里只给 token
    return {"token": cp.stdout.strip()}


@router.post("/api/remote/install")
async def api_remote_install(host: str = Query(...),
                             _rec: dict = Depends(require_admin)) -> dict:
    """一键接入（幂等：python3.12 + linger + 推代码 + 播种 brains + doctor）。
    同步等待 2-5 分钟。"""
    h = _host(host)
    try:
        # install_host 是生成器——to_thread(生成器) 只会在子线程里创建生成器
        # 对象，list() 的消费仍会同步跑在事件循环上（冻结整个 API 数分钟）；
        # 必须把「创建 + 消费」整体挪进子线程。
        logs = await asyncio.to_thread(lambda: list(remote.install_host(h)))
    except remote.RemoteError as e:
        raise HTTPException(400, str(e))
    agentops.audit("remote.install", host=h.get("name", host))
    return {"ok": True, "logs": logs}


@router.post("/api/remote/adopt")
async def api_remote_adopt(host: str = Query(...),
                           _rec: dict = Depends(require_admin)) -> dict:
    """收编存量部署（自动化四步，幂等）：探测部署根 → 回写清单 → 升级 →
    停+禁 serve（单头原则）→ doctor 验证。既有 agent 原样接管。"""
    h = _host(host)
    try:
        # 同 install：生成器的消费必须整体进子线程（见上）
        logs = await asyncio.to_thread(lambda: list(remote.adopt_host(h)))
    except remote.RemoteError as e:
        raise HTTPException(400, str(e))
    agentops.audit("remote.adopt", host=h.get("name", host))
    return {"ok": True, "logs": logs}


@router.post("/api/remote/upgrade")
async def api_remote_upgrade(host: str = Query(...),
                             _rec: dict = Depends(require_admin)) -> dict:
    """重推代码 tar（管理面升级 / 内核版本发布）。"""
    h = _host(host)
    try:
        await asyncio.to_thread(remote.upgrade_host, h)
    except remote.RemoteError as e:
        raise HTTPException(400, str(e))
    agentops.audit("remote.upgrade", host=h.get("name", host))
    return {"ok": True}


@router.post("/api/remote/agents/{agent_id}/backup")
async def api_remote_backup(agent_id: str, host: str = Query(...),
                            _rec: dict = Depends(require_admin)) -> dict:
    """远端备份 → scp 拉回控制端 etc/remote-backups/<机器名>/。"""
    h = _host(host)
    safe_name = re.sub(r"[^\w.\-]", "_", h.get("name") or host)
    out_dir = get_config().etc_dir / "remote-backups" / safe_name
    try:
        local = await asyncio.to_thread(remote.backup_host, h, agent_id, out_dir)
    except remote.RemoteError as e:
        raise HTTPException(400, str(e))
    agentops.audit("remote.backup", host=h.get("name", host), agent=agent_id,
                   file=local.name)
    return {"ok": True, "path": str(local), "name": local.name}


@router.get("/api/remote/backups")
def api_remote_backups(_rec: dict = Depends(require_admin)) -> dict:
    """控制端已拉回的远端备份清单。"""
    root = get_config().etc_dir / "remote-backups"
    items = []
    if root.is_dir():
        for p in sorted(root.rglob("*.tar.gz")):
            st = p.stat()
            items.append({"host": p.parent.name, "name": p.name, "path": str(p),
                          "size": st.st_size,
                          "mtime": datetime.fromtimestamp(st.st_mtime)
                          .strftime("%Y-%m-%dT%H:%M:%SZ")})
    return {"backups": items}


@router.post("/api/remote/restore")
async def api_remote_restore(req: RemoteRestoreReq,
                             _rec: dict = Depends(require_admin)) -> dict:
    """备份包推上远端并恢复（跨主机迁移的腿）。from_path = 控制端本机路径
    （通常来自 GET /api/remote/backups）。"""
    h = _host(req.host)
    path = Path(req.from_path).expanduser().resolve()
    argv = []
    if req.new_id:
        argv += ["--new-id", req.new_id]
    if req.port:
        argv += ["--port", str(req.port)]
    if req.overwrite:
        argv.append("--overwrite")
    try:
        cp = await asyncio.to_thread(remote.restore_host, h, path, argv)
    except remote.RemoteError as e:
        raise HTTPException(400, str(e))
    _check(cp, "远端恢复")
    agentops.audit("remote.restore", host=h.get("name", req.host),
                   from_file=path.name)
    return {"ok": True, "out": cp.stdout}


# ── 远端文件通道（upload/ 可写 · workspace/ 只读；远端 `xusi fs-*` 同一份
#    files.py 实现，校验/原子写/审计都在远端发生，控制端只做 ssh 搬运）─────


@router.get("/api/remote/agents/{agent_id}/fs")
async def api_remote_fs_list(agent_id: str, host: str = Query(...), path: str = "",
                             _rec: dict = Depends(require_admin)) -> dict:
    """远端列目录（「文件」tab；path 为空 = 根视图）。"""
    h = _host(host)
    try:
        return await asyncio.to_thread(remote.fs_list, h, agent_id, path)
    except remote.RemoteError as e:
        raise HTTPException(400, str(e)) from None


@router.get("/api/remote/agents/{agent_id}/fs/file")
async def api_remote_fs_file(agent_id: str, host: str = Query(...), path: str = "",
                             mode: str = "text", download: bool = False,
                             _rec: dict = Depends(require_admin)):
    """远端读文件：mode=text → JSON 预览；mode=raw → ssh 流式拉到控制端临时
    文件再 FileResponse 流出（大文件不在内存里整体缓冲）。"""
    h = _host(host)
    if mode == "text":
        try:
            return await asyncio.to_thread(remote.fs_read, h, agent_id, path)
        except remote.RemoteError as e:
            raise HTTPException(400, str(e)) from None
    # 临时文件必须活到流式响应送完——mkdtemp + BackgroundTask 清理
    # （TemporaryDirectory 的 with 块在 return 时就拆，FileResponse 还没读）
    d = tempfile.mkdtemp(prefix="xusi-fs-dl-")
    local = Path(d) / "blob"
    try:
        await asyncio.to_thread(remote.fs_cat_to_file, h, agent_id, path, local)
    except remote.RemoteError as e:
        shutil.rmtree(d, ignore_errors=True)
        raise HTTPException(400, str(e)) from None
    suffix = Path(path.strip("/")).suffix.lower()
    ctype = files.INLINE_TYPES.get(suffix, "application/octet-stream")
    inline = suffix in files.INLINE_TYPES and not download
    fname = Path(path.strip("/")).name or "blob"
    resp = FileResponse(local, media_type=ctype, filename=fname,
                        content_disposition_type="inline" if inline else "attachment",
                        background=BackgroundTask(shutil.rmtree, d, True))
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Cache-Control"] = "no-store"
    return resp


@router.get("/api/remote/agents/{agent_id}/fs/zip")
async def api_remote_fs_zip(agent_id: str, host: str = Query(...), path: str = "",
                            _rec: dict = Depends(require_admin)) -> FileResponse:
    """远端目录 zip：远端出包 → ssh 流式拉回控制端临时文件 → FileResponse。"""
    h = _host(host)
    d = tempfile.mkdtemp(prefix="xusi-fs-zip-")
    local = Path(d) / "bundle.zip"
    try:
        await asyncio.to_thread(remote.fs_zip_to_file, h, agent_id, path, local)
    except remote.RemoteError as e:
        shutil.rmtree(d, ignore_errors=True)
        raise HTTPException(400, str(e)) from None
    fname = (Path(path.strip("/")).name or "bundle") + ".zip"
    resp = FileResponse(local, media_type="application/zip", filename=fname,
                        background=BackgroundTask(shutil.rmtree, d, True))
    resp.headers["Cache-Control"] = "no-store"
    return resp


@router.post("/api/remote/agents/{agent_id}/fs/upload")
async def api_remote_fs_upload(agent_id: str, host: str = Query(...), path: str = "",
                               overwrite: bool = False,
                               files_in: list[UploadFile] = File(..., alias="files"),
                               _rec: dict = Depends(require_admin)) -> dict:
    """远端上传：multipart 在控制端收齐 → 逐文件 ssh stdin → `xusi fs-put`。
    重名不覆盖时跳过并回报（与本地同语义；前端确认后 overwrite=1 重传）。"""
    import posixpath
    h = _host(host)
    if not files_in:
        raise HTTPException(400, "没有收到文件")
    dir_rel = path or files.WRITE_ROOT
    saved, skipped = [], []
    for f in files_in:
        name = posixpath.basename(f.filename or "").strip()
        if not name or name in (".", ".."):
            skipped.append(f.filename or "(空名)")
            continue
        data = await f.read()
        try:
            await asyncio.to_thread(remote.fs_put, h, agent_id,
                                    posixpath.join(dir_rel, name), data,
                                    overwrite=overwrite)
            saved.append({"name": name, "size": len(data)})
        except remote.RemoteError as e:
            skipped.append(f"{name}（{e}）")
    agentops.audit("remote.fs.upload", host=h.get("name", host), agent=agent_id,
                   dir=dir_rel, files=[s["name"] for s in saved])
    return {"id": agent_id, "path": dir_rel, "uploaded": saved, "skipped": skipped}


@router.post("/api/remote/agents/{agent_id}/fs/mkdir")
async def api_remote_fs_mkdir(agent_id: str, req: FsPathReq, host: str = Query(...),
                              _rec: dict = Depends(require_admin)) -> dict:
    h = _host(host)
    try:
        r = await asyncio.to_thread(remote.fs_mkdir, h, agent_id, req.path)
    except remote.RemoteError as e:
        raise HTTPException(400, str(e)) from None
    agentops.audit("remote.fs.mkdir", host=h.get("name", host), agent=agent_id,
                   path=req.path)
    return r


@router.post("/api/remote/agents/{agent_id}/fs/write")
async def api_remote_fs_write(agent_id: str, req: FsWriteReq, host: str = Query(...),
                              _rec: dict = Depends(require_admin)) -> dict:
    h = _host(host)
    try:
        r = await asyncio.to_thread(remote.fs_write, h, agent_id, req.path, req.text)
    except remote.RemoteError as e:
        raise HTTPException(400, str(e)) from None
    agentops.audit("remote.fs.write", host=h.get("name", host), agent=agent_id,
                   path=req.path, chars=len(req.text))
    return r


@router.post("/api/remote/agents/{agent_id}/fs/move")
async def api_remote_fs_move(agent_id: str, req: FsMoveReq, host: str = Query(...),
                             _rec: dict = Depends(require_admin)) -> dict:
    h = _host(host)
    try:
        r = await asyncio.to_thread(remote.fs_move, h, agent_id, req.path, req.new_path)
    except remote.RemoteError as e:
        raise HTTPException(400, str(e)) from None
    agentops.audit("remote.fs.move", host=h.get("name", host), agent=agent_id,
                   frm=req.path, to=req.new_path)
    return r


@router.delete("/api/remote/agents/{agent_id}/fs")
async def api_remote_fs_delete(agent_id: str, host: str = Query(...), path: str = "",
                               _rec: dict = Depends(require_admin)) -> dict:
    """远端软删（远端 etc/.trash/fs/ 里可捞回；拒删 upload/ 根）。"""
    h = _host(host)
    try:
        r = await asyncio.to_thread(remote.fs_delete, h, agent_id, path)
    except remote.RemoteError as e:
        raise HTTPException(400, str(e)) from None
    agentops.audit("remote.fs.delete", host=h.get("name", host), agent=agent_id,
                   path=path)
    return r


# 注意：泛化的 {action} 路由必须注册在全部具体路由（mail/mailbox/sessions/
# observe-token/backup）之后——FastAPI 按注册顺序匹配，放前面会把
# /mail、/observe-token 吞成 {action} 而 404。
@router.post("/api/remote/agents/{agent_id}/{action}")
async def api_remote_agent_op(agent_id: str, action: str, host: str = Query(...),
                              _rec: dict = Depends(require_admin)) -> dict:
    """生命周期六件套（start/stop/pause/resume/restart/delete）。"""
    if action not in _OPS:
        raise HTTPException(404, f"未知操作：{action}")
    h = _host(host)
    cp = await asyncio.to_thread(remote.remote_agent_op, h, action, [agent_id])
    _check(cp, f"远端 {action}")
    agentops.audit("remote.op", host=h.get("name", host), agent=agent_id, op=action)
    return {"ok": True}
