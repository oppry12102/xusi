"""远端机器清单路由（控制端 fan-out 的配置面）。

GET  /api/hosts         读清单（含明文密码——「先明文」阶段决议；admin 鉴权）
PUT  /api/hosts         整表替换（原子写 600；与 CLI 同源同一份 etc/hosts.toml）
POST /api/hosts/probe   单机连通体检（WebUI 加入/编辑前的 ssh 校验）
GET  /api/hosts/health  全机连通体检（并发 + 30s 缓存——WebUI 健康徽章用）

与 agent 无关：这是管理面↔管理面的运维面（多副本零管理的机器簿），
不触碰任何 agent 层。
"""
import time as _time
from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter, Depends

from .. import remote
from .auth import require_admin
from .models import HostProbeReq, HostsPutReq

router = APIRouter()

# 全机体检缓存（name → 结果 + _t 单调秒）——WebUI 60s 轮询不该打爆 ssh；
# 30s 内重复请求直接回缓存，过期条目并发补测
_HEALTH_CACHE: dict[str, dict] = {}
_HEALTH_TTL = 30.0


@router.get("/api/hosts")
def api_hosts(_rec: dict = Depends(require_admin)) -> dict:
    """远端机器清单（WebUI「远端机器」页用）。missing_ok：还没建过清单 → 空表。"""
    return {"hosts": remote.load_hosts(missing_ok=True)}


@router.put("/api/hosts")
def api_hosts_put(req: HostsPutReq, _rec: dict = Depends(require_admin)) -> dict:
    """整表替换（原子写 600）。字段白名单外丢弃；name/host/user 缺一报 400。"""
    from fastapi import HTTPException
    try:
        remote.save_hosts(req.hosts)
    except remote.RemoteError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "hosts": remote.load_hosts()}


@router.post("/api/hosts/probe")
def api_hosts_probe(req: HostProbeReq, _rec: dict = Depends(require_admin)) -> dict:
    """单机连通体检。永远 200：{ok, hostname?, seconds?, error?}——前端按 ok 分支展示。"""
    h: dict = {"host": req.host.strip(), "port": req.port, "user": req.user.strip()}
    if req.password:
        h["password"] = req.password
    if req.key:
        h["key"] = req.key
    if req.proxy:
        h["proxy"] = req.proxy
    if req.via:
        h["via"] = req.via
    return remote.probe_host(h)


@router.get("/api/hosts/health")
def api_hosts_health(_rec: dict = Depends(require_admin)) -> dict:
    """全机连通体检：并发、短超时（15s/机）；30s 缓存。返回
    {"health": [{name, host, ok, hostname?, seconds?, error?, at}]}。"""
    hosts = remote.load_hosts(missing_ok=True)
    now = _time.monotonic()

    def key_of(h: dict) -> str:
        return h.get("name") or f"{h.get('host')}:{h.get('port') or 22}"

    keys = [key_of(h) for h in hosts]
    stale = [(h, k) for h, k in zip(hosts, keys)
             if not (k in _HEALTH_CACHE and now - _HEALTH_CACHE[k].get("_t", 0) < _HEALTH_TTL)]
    if stale:
        def _probe(t: tuple[dict, str]) -> tuple[str, dict]:
            h, k = t
            r = dict(remote.probe_host(h, timeout=15))
            r.update(name=k, host=h.get("host"),
                     at=_time.strftime("%H:%M:%SZ", _time.gmtime()), _t=now)
            return k, r
        # 并发上限 16：机器簿大了不一股脑并发上百 ssh（15s/机超时已封顶时延）
        with ThreadPoolExecutor(max_workers=min(len(stale), 16)) as ex:
            for k, r in ex.map(_probe, stale):
                _HEALTH_CACHE[k] = r
    return {"health": [{k: v for k, v in _HEALTH_CACHE.get(k, {}).items()
                        if not k.startswith("_")} for k in keys]}
