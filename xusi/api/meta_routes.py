"""元信息路由：健康检查 / 节点自报 / 大脑池 / 版本仓库 / 端口 / WebUI / 文档。

不鉴权的端点：/api/health、/api/node（公开身份：id/name/version，无敏感字段）、/、/api/docs.md。
"""
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse

from .. import __version__, brains, node, ports, registry, versions
from ..config import get_config, live_default_roots
from .auth import require_admin, require_auth
from .models import PatchNodeReq

router = APIRouter()


def _health() -> dict:
    return {"ok": True, "service": "xusi", "version": __version__,
            "agents": len(registry.list_agents()),
            "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}


@router.get("/api/health")
def api_health() -> dict:
    return _health()


@router.get("/api/whoami")
def api_whoami(_rec: dict = Depends(require_auth)) -> dict:
    """唯一的角色：admin。rec 形如 {"token": <admin token>}——只用来
    表示「鉴权通过」，对外 shape 保持最小。"""
    return {"role": "admin"}


# ── 节点身份（自报 / 改名）────────────────

@router.get("/api/node")
def api_node() -> dict:
    """本节点自报（WebUI 顶栏用）。不鉴权——只返回公开身份字段
    （id/name/version），从不返回 secret/token。"""
    return node.info()


@router.patch("/api/node")
def api_node_patch(req: PatchNodeReq, _rec: dict = Depends(require_admin)) -> dict:
    """改名。id 不让改（机器身份）。"""
    node.set_name(req.name)
    return node.info()


@router.get("/api/brains")
def api_brains(_rec: dict = Depends(require_auth)) -> list[dict]:
    return brains.pool_summary()


@router.get("/api/default-roots")
def api_default_roots(_rec: dict = Depends(require_auth)) -> dict:
    """缺省根智能体（etc/xusi.toml 的 [[default_roots]]）——创建对话框预填用。
    每次直读盘面（live_default_roots，不吃进程缓存）：换根 token 改 toml
    即生效，不用重启管理面。只回齐备条目（address/token 缺一的剔除）。"""
    return {"roots": live_default_roots()}


@router.get("/api/versions")
def api_versions(_rec: dict = Depends(require_auth)) -> dict:
    """xuseek-v2 版本仓库清单（zip 由管理员投放于 versions/，约定见 docs/versions.md）。
    创建 agent 的 source_version 缺省 = 清单最新版（每 agent 私有副本）。
    default_runtime 供创建对话框预选（[manager].default_runtime）。"""
    return {"repo_dir": str(get_config().versions_dir),
            "default_ready": bool(versions.list_versions()),
            "default_runtime": get_config().default_runtime,
            "versions": versions.list_versions()}


@router.get("/api/ports/available")
def api_ports(count: int = 10, _rec: dict = Depends(require_auth)) -> dict:
    return {"range": [get_config().port_lo, get_config().port_hi],
            "ports": ports.available_ports(max(1, min(count, 50)))}


# ── 静态：WebUI 与文档 ───────────────────────────────────────────────

@router.get("/")
def index() -> FileResponse:
    """WebUI 入口：单文件 SPA，inline JS + inline CSS。

    强制 no-cache：开发者改完 index.html 浏览器立刻拿到新版，不要等缓存过期。
    否则 FileResponse 默认带 Last-Modified + ETag → 浏览器下次 GET 收 304 →
    继续用旧 inline JS（看不见的 bug：JS 旧但 UI 没明显错误信息）。"""
    resp = FileResponse(get_config().webui_dir / "index.html", media_type="text/html")
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


@router.get("/api/docs.md")
def api_docs_md() -> FileResponse:
    p: Path = get_config().docs_dir / "api.md"
    if not p.exists():
        raise HTTPException(404, "docs/api.md 未生成")
    return FileResponse(p, media_type="text/markdown")
