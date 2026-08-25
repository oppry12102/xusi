"""元信息路由：健康检查 / 自报 / 集群视图 / 大脑池 / 版本仓库 / 端口 / WebUI / 文档。

不鉴权的端点：/api/health、/api/peer/id（peer 间建立信任前的握手）、/、/api/docs.md。
"""
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse

from .. import __version__, brains, node, peers, ports, registry, versions
from ..config import get_config
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
    """唯一的角色：admin。rec 形如 {"token": <cluster_secret>}——只用来
    表示「鉴权通过」，对外 shape 保持最小。"""
    return {"role": "admin"}


# ── 节点身份（peer 自报 / 改名 / 集群视图）────────────────
# 注意：/api/peer/id **不鉴权**——peer 之间在建立信任之前就要先拿到对方自报；
# 仅返回公开字段（id/name/role/version/url），从不返回 secret/cluster_secret/tokens。

@router.get("/api/peer/id")
def api_peer_id() -> dict:
    """本节点自报（peer 之间 + WebUI 顶栏皆用）。"""
    return node.info()


@router.patch("/api/node")
def api_node_patch(req: PatchNodeReq, _rec: dict = Depends(require_admin)) -> dict:
    """改名。id/role 不让改（id 是机器身份；role 改完要重启，且本来也不该在一行 API 里改）。"""
    node.set_name(req.name)
    return node.info()


@router.get("/api/cluster")
def api_cluster(_rec: dict = Depends(require_auth)) -> dict:
    """集群视图：self + 探活后的 peers[]（每个 peer 含 ok/info/error/latency_ms）。
    前端顶栏的「切换节点下拉」与节点对话框的「其他节点」列表都直接消费本接口。
    单节点模式（cluster_secret 未设）：peers 永远空，不探活。
    排除自己——peer 列表来自共享 toml，集群模式下自己的 id 可能在里头。"""
    me = node.info()
    cluster_on = peers.is_cluster()
    out = {"self": me, "cluster": cluster_on, "peers": []}
    if not cluster_on:
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


@router.get("/api/brains")
def api_brains(_rec: dict = Depends(require_auth)) -> list[dict]:
    return brains.pool_summary()


@router.get("/api/versions")
def api_versions(_rec: dict = Depends(require_auth)) -> dict:
    """xuseek-v2 版本仓库清单（zip 由管理员投放于 versions/，约定见 docs/versions.md）。
    创建 agent 的 source_version 缺省 = 清单最新版（每 agent 私有副本）。
    'main' = 共享主源码（过渡期字段，新约定不再推荐），其就绪与否见 main_ready。
    default_ready = 版本仓库是否非空（实际默认源 = 仓库最新版）。"""
    return {"repo_dir": str(get_config().versions_dir),
            "default_ready": bool(versions.list_versions()),
            "main_ready": (get_config().source_dir / "xuseek.sh").exists(),
            "versions": versions.list_versions()}


@router.get("/api/ports/available")
def api_ports(count: int = 10, _rec: dict = Depends(require_auth)) -> dict:
    return {"range": [get_config().port_lo, get_config().port_hi],
            "ports": ports.available_ports(max(1, min(count, 50)))}


# ── 静态：WebUI 与文档 ───────────────────────────────────────────────

@router.get("/")
def index() -> FileResponse:
    return FileResponse(get_config().webui_dir / "index.html", media_type="text/html")


@router.get("/api/docs.md")
def api_docs_md() -> FileResponse:
    p: Path = get_config().docs_dir / "api.md"
    if not p.exists():
        raise HTTPException(404, "docs/api.md 未生成")
    return FileResponse(p, media_type="text/markdown")
