"""peer 名册路由（Phase 2 集群）：CRUD + 强制重探 + 集群内自收敛。

加/减 peer 走标准三段：add 探活拿 id（验证两端 `[cluster].secret` 一致
后才能互信）；remove 直接改 toml。新节点引导走 `xusi init --cluster-secret <A的secret>`
→ `xusi peer add http://B:8601`（不再用 invitation JWT / join.sh 脚本）。

集群内自收敛（每台 xusi 自动拥有全表）：
- POST /api/peers 成功后 fire-and-forget 通知每个已知 peer 调
  /api/internal/peers/announce（idempotent 入册）
- bootstrap 场景：某 xusi peers.toml 为空时，先手动 add 一个 peer，再调
  /api/internal/peers/resync（任选 from_peer_id，缺省 = 任一可达 peer），
  从其 /api/peers 拉全表合并

跨节点跳转走前端：peer 行「打开」直接拼 `${peer.url}/?mtoken=<tok>` 跳到
peer——浏览器一次性到 peer，省一次本机中转 round-trip + admin token 不在
本机 server access log 留痕。
"""
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query

from .. import peers
from .auth import require_admin, require_auth
from .models import AddPeerReq, AnnouncePeerReq, VisibilityReq, WelcomePeersReq

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
                 "url": p["url"], "ok": r["ok"],
                 "show_agents": p.get("show_agents", True)}
        if r.get("latency_ms") is not None:
            entry["latency_ms"] = r["latency_ms"]
        if r["ok"]:
            entry["info"] = r["info"]
        else:
            entry["error"] = r.get("error", "")
        out["peers"].append(entry)
    return out


@router.post("/api/peers", status_code=201)
async def api_peers_add(req: AddPeerReq,
                        bg: BackgroundTasks,
                        _rec: dict = Depends(require_admin)) -> dict:
    """注册一个 peer：先探活（拿 id），落 etc/peers.toml，广播给其他已知 peer。
    失败：peer 不可达 → 502 PeerUnreachable；本地拒绝（cluster_secret 为空 /
    重名 / url 坏）→ 400 PeerRefused。

    广播是 fire-and-forget：失败仅记日志，不影响本次 add 的 201 响应。
    BackgroundTasks 直接 await 异步协程（不再套 asyncio.run——后者在已有
    event loop 里 RuntimeError，被 catch 吞了导致静默失败）。"""
    rec = peers.add_peer(req.url, name=req.name, show_agents=req.show_agents)  # 抛异常被全局 handler 接住
    bg.add_task(peers._broadcast_peer_add, rec)   # async 协程；FastAPI 自动 await
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


@router.post("/api/peers/{peer_id}/visibility")
def api_peers_visibility(peer_id: str,
                          req: VisibilityReq,
                          _rec: dict = Depends(require_admin)) -> dict:
    """节点页面开关：切换某个 peer 行的 show_agents。

    show_agents=true → 该 peer 的 agents 出现在 /api/agent-peers fan-in 里
    show_agents=false → fan-in 跳过该 peer（但 peer 行本身还在，互联 token
    不受影响；之前缓存过的 token 仍能调 /svc/...）

    仅影响本机 fan-in 视图；不通知对端（这是本机自己的偏好）。"""
    updated = peers.update_peer_visibility(peer_id, req.show_agents)
    if not updated:
        raise HTTPException(404, f"peer 不存在: {peer_id}")
    return {"id": peer_id, "show_agents": updated.get("show_agents", True)}


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


# ── 集群内自收敛：announce / resync ────────────────────────────────

@router.post("/api/internal/peers/announce")
def api_peers_announce(req: AnnouncePeerReq,
                       _rec: dict = Depends(require_admin)) -> dict:
    """集群内自收敛：另一台 xusi add_peer 成功后 fire-and-forget 通告过来。

    接收端 idempotent 入册（见 `peers.local_add_or_update`）：
    - id 未见 → 入册，status='added'
    - id 命中 + url 一致 → 跳过，status='skipped'
    - id 命中 + url 冲突 → 保留本地，status='skipped_conflict'（不信任
      单方面通告去改 peer 地址——要改走 remove_peer + add_peer）

    返回 `{ok, status, id}` 给 sender 做日志/可观测性。"""
    status = peers.local_add_or_update({"id": req.id, "url": req.url,
                                        "name": req.name})
    return {"ok": True, "status": status, "id": req.id}


@router.post("/api/internal/peers/welcome")
async def api_peers_welcome(req: WelcomePeersReq,
                            _rec: dict = Depends(require_admin)) -> dict:
    """迎新接收端：通告方把自己的全表（除 self 与新人）一次性发给新人做合并。

    与 announce 配对：announce 通告单条 peer，welcome 是整张表。新人拿到
    welcome 后立即拥有完整集群视图，不必再手动调 resync。

    典型链路：A 加 peer X → A 给所有已知 peer 发 announce "X 来了" → A
    给 X 发 welcome（当前全表）；announce + welcome 同步进行，结果对称。"""
    summary = await peers.welcome_peers(req.from_id, req.peers)
    return {"ok": True, **summary}


@router.post("/api/internal/peers/resync")
async def api_peers_resync(from_peer_id: str | None = Query(
        None, description="从哪个 peer 拉全表；缺省 = 任一可达的已知 peer"),
        _rec: dict = Depends(require_admin)) -> dict:
    """bootstrap 助手：从已知 peer 拉 /api/peers 全表合并入本地。

    适用：
    - 新节点加入集群：先手动 add 一个 peer 拿到第一根线，再调本端点把全表
      拉齐（避免手动复制 4 个 [[peers]] 块）
    - 集群拓扑剧变后手动收敛

    鉴权：admin（cluster_secret）。
    """
    pls = peers.list_peers()
    if not pls:
        raise HTTPException(400, "本地无 peer；先 add 一个再 resync")

    src = None
    if from_peer_id:
        src = next((p for p in pls if p["id"] == from_peer_id), None)
        if not src:
            raise HTTPException(404, f"peer {from_peer_id} 不在名册")
    else:
        for p in pls:
            if peers.probe_peer(p)["ok"]:
                src = p
                break
        if not src:
            raise HTTPException(502, "没有可达 peer 可供 resync")

    summary = await peers.resync_from_peer(src)
    return {"ok": True, **summary}