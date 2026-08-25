"""peer 名册路由（Phase 2 集群）：CRUD + 强制重探。

加/减 peer 走标准三段：add 探活拿 id（验证两端 `[cluster].secret` 一致
后才能互信）；remove 直接改 toml。新节点引导走 `xusi init --cluster-secret <A的secret>`
→ `xusi peer add http://B:8601`（不再用 invitation JWT / join.sh 脚本）。

跨节点跳转走前端：peer 行「打开」直接拼 `${peer.url}/?mtoken=<tok>` 跳到
peer——浏览器一次性到 peer，省一次本机中转 round-trip + admin token 不在
本机 server access log 留痕。"""
from fastapi import APIRouter, Depends, HTTPException

from .. import peers
from .auth import require_admin, require_auth
from .models import AddPeerReq

router = APIRouter()


@router.get("/api/peers")
def api_peers_list(_rec: dict = Depends(require_auth)) -> dict:
    """列出所有 peer + 实时探活。
    返回 shape 与 /api/cluster.peers 相同；前端若只需要名册而非 self 也用这个。"""
    out = {"cluster": peers.is_cluster(), "peers": []}
    if not peers.is_cluster():
        return out
    for p in peers.list_peers():
        r = peers.probe_peer(p)
        entry = {"id": p["id"], "name": p.get("name", ""),
                 "url": p["url"], "ok": r["ok"]}
        if r.get("latency_ms") is not None:
            entry["latency_ms"] = r["latency_ms"]
        if r["ok"]:
            entry["info"] = r["info"]
        else:
            entry["error"] = r.get("error", "")
        out["peers"].append(entry)
    return out


@router.post("/api/peers", status_code=201)
def api_peers_add(req: AddPeerReq,
                  _rec: dict = Depends(require_admin)) -> dict:
    """注册一个 peer：先探活（拿 id），落 etc/peers.toml。
    失败：peer 不可达 → 502 PeerUnreachable；本地拒绝（cluster_secret 为空 /
    重名 / url 坏）→ 400 PeerRefused。"""
    rec = peers.add_peer(req.url, name=req.name)  # 抛异常被全局 handler 接住
    r = peers.probe_peer(rec)
    return {
        **rec,
        "ok": r["ok"],
        "latency_ms": r.get("latency_ms"),
        "info": r.get("info") if r["ok"] else None,
        "error": r.get("error") if not r["ok"] else None,
    }


@router.delete("/api/peers/{peer_id}")
def api_peers_remove(peer_id: str,
                     _rec: dict = Depends(require_admin)) -> dict:
    if not peers.remove_peer(peer_id):
        raise HTTPException(404, f"peer 不存在: {peer_id}")
    return {"removed": peer_id}


@router.post("/api/peers/probe")
def api_peers_probe_all(_rec: dict = Depends(require_admin)) -> dict:
    """立即全员重探；前端手动刷新按钮用。"""
    rows = peers.list_peers()
    out = []
    for p in rows:
        r = peers.probe_peer(p)
        out.append({"id": p["id"], "url": p["url"],
                    "ok": r["ok"], "latency_ms": r.get("latency_ms"),
                    "error": r.get("error", "") if not r["ok"] else ""})
    return {"probed": len(out), "results": out}
