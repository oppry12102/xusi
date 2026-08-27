"""peer 名册：etc/peers.toml 持久化 + 实时探活 + 集群内自收敛。

加 peer = 探活拿 id 写 toml；列 peer = 读 toml；探活 = hit
{peer.url}/api/peer/id（不鉴权，握手用）。前端 refresh 周期 15s，频率
远低于 5s 缓存能省穿透的边界——探活直接打，不缓存。peer 挂掉立刻反映；
起来也立刻看见。

集群互信 = 两端 `[cluster].secret` 一致。admin 拿同一把 token 即可
登任何一台——跨节点转发也只是把 `Authorization: Bearer <secret>` 透传给
peer 让它自己 verify。前代 invitation JWT（带 sid / 一次性消费 /
集群 secret 内嵌 / join.sh 引导脚本）已删除——加新节点走：
    A：xusi status           # 拿 secret
    B：xusi init --cluster-secret <secret>   # 同步
    A：xusi peer add http://B:8601
    B：xusi peer add http://A:8601

peer 名册自收敛（每台 xusi 都知道全集群）：
- add_peer 成功后 fire-and-forget 通知每个已知 peer（`POST /api/internal/peers/announce`）
- 接收端 idempotent 入册（id 命中则保留本地，跳过覆盖——>防止远程通告
  把已对齐的本地数据踢回旧值；首次见到才入）
- bootstrap：某 xusi 名册为空时调 `POST /api/internal/peers/resync`
  从已知 peer 拉 /api/peers 全表合并（手动触发一次即可）
- 整个机制用唯一的 cluster HTTP 通道（read-only GET 已用于 fan-in；
  POST 只多了 announce / resync 两个端点，纯集群内部事务）

fail-soft 原则：所有错误都从函数签名表达（返回 dict 含 ok/error），不抛
HTTPException——上层（api.py）按上下文决定码（404/400/502）。
"""
from __future__ import annotations

import time
from pathlib import Path

import httpx
import tomllib

from .config import get_config


class PeerUnreachable(Exception):
    """peer 不可达——网络层失败（add_peer / 探活超时 / 连接拒绝）。"""


class PeerRefused(Exception):
    """本地拒绝——cluster_secret 未设（无集群模式）、url 格式坏、id 重名等
    不是网络问题。"""


class PeerHttpError(Exception):
    """peer 返了 4xx——与 PeerUnreachable 区分；按 HTTP 码透传给 caller。"""
    def __init__(self, status: int, body):
        self.status = status
        self.body = body
        super().__init__(f"peer HTTP {status}")


def is_cluster() -> bool:
    """集群模式开关：cluster_secret 设值 = 开，留空 = 无集群模式（早期安装
    / 没设 secret 的机器——这时 peer 名册禁用，所有 `/api/*` 退化为单节点）。"""
    return bool(get_config().cluster_secret)


# ── 存储 ───────────────────────────────────────────────────────────

def _load() -> dict:
    f = get_config().peers_file
    if not f.exists():
        return {"peers": []}
    try:
        with f.open("rb") as fh:
            data = tomllib.load(fh)
    except Exception as e:
        print(f"警告：{f} 解析失败（{e}），按空名册起服务")
        return {"peers": []}
    # tomllib.load 对 0 字节 / 全注释文件返回 {}——补默认键
    data.setdefault("peers", [])
    return data


def _save(data: dict) -> None:
    f = get_config().peers_file
    f.parent.mkdir(parents=True, exist_ok=True)
    # 手工 toml 写出（无 tomli_w 依赖；schema 简单）
    lines: list[str] = []
    for p in data["peers"]:
        lines.append("[[peers]]")
        lines.append(f'id = "{_toml_escape(p["id"])}"')
        lines.append(f'url = "{_toml_escape(p["url"])}"')
        if p.get("name"):
            lines.append(f'name = "{_toml_escape(p["name"])}"')
        # show_agents 默认 true——只在显式为 false 时写出，避免污染 toml
        if p.get("show_agents") is False:
            lines.append("show_agents = false")
        lines.append("")
    tmp = f.with_suffix(".toml.tmp")
    tmp.write_text("\n".join(lines), encoding="utf-8")
    tmp.replace(f)


def _toml_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


# ── 名册 CRUD ───────────────────────────────────────────────────────

def list_peers() -> list[dict]:
    """读 etc/peers.toml，返回 [[peers]] 列表（深拷贝）。

    show_agents 字段缺省补 True（老 toml 没这个字段时按"主动加的"对待——
    升级前已存在的 peer 都是手动加的）。"""
    out = []
    for p in _load()["peers"]:
        if "show_agents" not in p:
            p = {**p, "show_agents": True}
        out.append(p)
    return out


def get_peer(peer_id: str) -> dict | None:
    for p in list_peers():
        if p["id"] == peer_id:
            return p
    return None


def find_by_url(url: str) -> dict | None:
    """按 url 查 peer（用于 location 反查，避免重名 url）。"""
    target = url.rstrip("/")
    for p in list_peers():
        if p["url"].rstrip("/") == target:
            return p
    return None


def add_peer(url: str, name: str = "", show_agents: bool = True) -> dict:
    """加 peer。先探活（拿 id），落 toml。失败抛 PeerUnreachable / PeerRefused。

    前置：cluster_secret 非空（无集群模式直接拒绝）。两端 secret 一致的事
    由 admin 自己保证——admin 把同一 secret 同步到了对端的 etc/xusi.toml
    才会来这里 add peer。

    show_agents 默认 true（admin 主动加就是想看对端 agents）；传 false
    走纯通信模式（peer 行还在但 fan-in 视图不显示）。"""
    if not is_cluster():
        raise PeerRefused("无集群模式（[cluster].secret 为空）；peer 注册需要集群模式"
                          "——先 `xusi init --cluster-secret <值>`")
    url = url.rstrip("/")
    if not (url.startswith("http://") or url.startswith("https://")):
        raise PeerRefused(f"peer url 必须 http(s)://；got: {url}")
    info = _probe_url(url)
    if not info["ok"]:
        raise PeerUnreachable(f"peer 不可达：{info['error']}")
    pid = info["info"]["id"]
    data = _load()
    for p in data["peers"]:
        if p["id"] == pid:
            raise PeerRefused(f"peer {pid} 已存在")
        if p["url"].rstrip("/") == url:
            raise PeerRefused(f"peer url {url} 已存在")
    rec = {"id": pid, "url": url,
           "name": (name or info["info"].get("name", "")).strip(),
           "show_agents": bool(show_agents)}
    data["peers"].append(rec)
    _save(data)
    return rec


def remove_peer(peer_id: str) -> bool:
    data = _load()
    before = len(data["peers"])
    data["peers"] = [p for p in data["peers"] if p["id"] != peer_id]
    if len(data["peers"]) == before:
        return False
    _save(data)
    return True


def update_peer_visibility(peer_id: str, show_agents: bool) -> dict | None:
    """切换单个 peer 行的 show_agents。返更新后的 record，未找到返 None。

    用于"节点页面打开"——admin 把某个被动收进来的 peer 改成可见，或反过来。
    不影响其他字段；show_agents 持久化到 toml。"""
    data = _load()
    for p in data["peers"]:
        if p["id"] == peer_id:
            old = p.get("show_agents", True)
            if old == show_agents:
                return p  # 没变化，不写盘
            p["show_agents"] = show_agents
            _save(data)
            return p
    return None


# ── 集群内自收敛（announce / resync）───────────────────────────

def local_add_or_update(rec: dict, default_show_agents: bool = False) -> str:
    """接收端 idempotent 入册：远端 announce / welcome 调用此函数。

    语义：
    - 本地无此 id → 入册，show_agents=default_show_agents（announce/welcome 默认 false）；
      但若 rec 显式带 show_agents 字段，以 rec 的为准（主动加的不会降级）
    - 本地有此 id 且 url 相同 → 跳过，返 'skipped'——**不**改本地 show_agents
      （保护主动意图：不主动收进来的 sync，永远不能把已 true 的降为 false）
    - 本地有此 id 且 url 不一致 → 保留本地，返 'skipped_conflict'（不信任
      单方面通告去改 peer 地址——要改走 remove_peer + add_peer）

    与 `add_peer` 区分：add_peer 对重复主动报错（admin 显式意图），这里
    对重复容忍（集群内自收敛的常态）。"""
    if not (isinstance(rec, dict) and rec.get("id") and rec.get("url")):
        raise PeerRefused(f"announce 字段不全: {rec}")
    new_id, new_url = rec["id"], rec["url"].rstrip("/")
    new_name = (rec.get("name") or "").strip()
    # rec 显式带 show_agents 时尊重它；否则用默认（welcome/announce 路径 = false）
    rec_show = rec.get("show_agents")
    if rec_show is None:
        rec_show = default_show_agents
    data = _load()
    for p in data["peers"]:
        if p["id"] == new_id:
            if p["url"].rstrip("/") == new_url:
                return "skipped"  # 已有：不动（包括 show_agents）
            return "skipped_conflict"
    data["peers"].append({"id": new_id, "url": new_url, "name": new_name,
                          "show_agents": rec_show})
    _save(data)
    return "added"


async def _broadcast_peer_add(rec: dict) -> None:
    """add_peer 成功后 fire-and-forget 做两件事：

    1) 给每个已知 peer（除 self 与 rec）发 announce：告诉"新人 X 来了"
    2) 给 rec 这个新人发 welcome：把当前全表（除 self 与 rec）发给它
       —— 否则 rec 那边名册是空的，要它手动 resync 才能补齐

    失败均仅记日志，不抛——本地已落盘，远端不通下次 resync 也会拉齐。
    """
    cfg = get_config()
    if not cfg.cluster_secret:
        return
    me_id = cfg.node_id
    payload = {"id": rec["id"], "url": rec["url"],
               "name": (rec.get("name") or "").strip()}

    # 1) 通告：新人 X 来了
    for p in list_peers():
        if p["id"] == rec["id"] or p["id"] == me_id:
            continue
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(5.0, connect=3.0)) as cli:
                r = await cli.post(
                    f"{p['url'].rstrip('/')}/api/internal/peers/announce",
                    json=payload,
                    headers={"authorization": f"Bearer {cfg.cluster_secret}"})
            if r.status_code >= 400:
                print(f"[peers] broadcast → {p['id']} HTTP {r.status_code}",
                      flush=True)
        except Exception as e:
            print(f"[peers] broadcast → {p['id']} 失败：{type(e).__name__}: {e}",
                  flush=True)

    # 2) 迎新：把当前全表（除 rec）发给新人；包括自己——否则新人
    #    永远不知道 welcome 发送方是谁，缺这一根线 cluster 就不对称。
    welcome_rows = [
        {"id": p["id"], "url": p["url"], "name": (p.get("name") or "").strip()}
        for p in list_peers()
        if p["id"] != rec["id"]
    ]
    if not welcome_rows:
        return  # 单节点 bootstrap 时不必发空 welcome
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(5.0, connect=3.0)) as cli:
            r = await cli.post(
                f"{rec['url'].rstrip('/')}/api/internal/peers/welcome",
                json={"from_id": me_id, "peers": welcome_rows},
                headers={"authorization": f"Bearer {cfg.cluster_secret}"})
        if r.status_code >= 400:
            print(f"[peers] welcome → {rec['id']} HTTP {r.status_code}",
                  flush=True)
    except Exception as e:
        print(f"[peers] welcome → {rec['id']} 失败：{type(e).__name__}: {e}",
              flush=True)


async def welcome_peers(from_id: str, peers_list: list) -> dict:
    """迎新接收端：把对方发来的全表合并入本地（idempotent）。

    与 announce 的差别：announce 是单条 peer；welcome 是整张表一次性合并，
    给刚加进来的新人用它做"快速 bootstrap"——之前只有 announce 的话，新人
    自己还是空名册，要它手动 resync 才行。

    返回 {from_id, total, added, skipped, skipped_conflict}。"""
    added = skipped = skipped_conflict = 0
    for row in peers_list or []:
        if not (isinstance(row, dict) and row.get("id") and row.get("url")):
            continue
        s = local_add_or_update({"id": row["id"], "url": row["url"],
                                 "name": row.get("name", "")})
        if s == "added":
            added += 1
        elif s == "skipped":
            skipped += 1
        elif s == "skipped_conflict":
            skipped_conflict += 1
    return {"from_id": from_id, "total": len(peers_list or []),
            "added": added, "skipped": skipped,
            "skipped_conflict": skipped_conflict}


async def resync_from_peer(peer: dict) -> dict:
    """从指定 peer 拉 /api/peers 全表，合并入本地。

    用于 bootstrap：某 xusi peers.toml 为空时，先手动 add 一个 peer，然后
    调一次 resync，全表对齐。也用于集群拓扑发生剧变后的手动收敛。

    返回 {from, total, added, skipped, skipped_conflict}——上层路由用此组装响应，
    函数本身只抛 PeerUnreachable / PeerRefused / PeerHttpError。"""
    cfg = get_config()
    if not cfg.cluster_secret:
        raise PeerRefused("无集群模式，resync 无意义")
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0)) as cli:
            r = await cli.get(f"{peer['url'].rstrip('/')}/api/peers",
                              headers={"authorization": f"Bearer {cfg.cluster_secret}"})
    except httpx.HTTPError as e:
        raise PeerUnreachable(f"peer {peer['id']} 不可达：{type(e).__name__}: {e}") from e
    if r.status_code == 401:
        raise PeerRefused(f"peer {peer['id']} 拒绝：cluster_secret 不一致")
    if r.status_code != 200:
        try:
            body = r.json()
        except Exception:
            body = {"detail": r.text[:200]}
        raise PeerHttpError(r.status_code, body)

    try:
        body = r.json()
    except Exception as e:
        raise PeerUnreachable(f"peer {peer['id']} 返回非 JSON：{e}") from e
    rows = body.get("peers") if isinstance(body, dict) else None
    if not isinstance(rows, list):
        raise PeerRefused(f"peer {peer['id']} /api/peers 返回格式坏：{body!r}")

    added = skipped = skipped_conflict = 0
    for row in rows:
        if not (isinstance(row, dict) and row.get("id") and row.get("url")):
            continue
        # 显式传 source 的 show_agents：local_add_or_update 在 rec 显式带
        # show_agents 时以 rec 为准（参见该函数 docstring）；不带则走
        # default_show_agents=False。已有 peer 不被覆盖（不降级策略生效）。
        s = local_add_or_update({"id": row["id"], "url": row["url"],
                                 "name": row.get("name", ""),
                                 "show_agents": row.get("show_agents")})
        if s == "added":
            added += 1
        elif s == "skipped":
            skipped += 1
        elif s == "skipped_conflict":
            skipped_conflict += 1
    return {"from": peer["id"], "total": len(rows),
            "added": added, "skipped": skipped,
            "skipped_conflict": skipped_conflict}


# ── 探活 ───────────────────────────────────────────────────────────

def _probe_url(url: str) -> dict:
    """裸 url 探活（给 add_peer 还没 id 的情形用）。"""
    start = time.monotonic()
    try:
        with httpx.Client(timeout=httpx.Timeout(5.0, connect=3.0)) as c:
            r = c.get(f"{url.rstrip('/')}/api/peer/id")
        latency = int((time.monotonic() - start) * 1000)
        if r.status_code == 200:
            info = r.json()
            if not isinstance(info, dict) or "id" not in info:
                return {"ok": False, "error": "响应缺 id 字段", "latency_ms": latency}
            return {"ok": True, "info": info, "latency_ms": latency}
        return {"ok": False, "error": f"HTTP {r.status_code}", "latency_ms": latency}
    except Exception as e:
        latency = int((time.monotonic() - start) * 1000)
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "latency_ms": latency}


def probe_peer(peer: dict) -> dict:
    """peer 记录（带 id）的实时探活——不缓存：前端 15s 轮询无穿透压力，
    不缓存换来挂/起立刻反映。"""
    return _probe_url(peer["url"])
