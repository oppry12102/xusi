"""远端机器清单路由（控制端 fan-out 的配置面）。

GET /api/hosts   读清单（含明文密码——「先明文」阶段决议；admin 鉴权）
PUT /api/hosts   整表替换（原子写 600；与 CLI 同源同一份 etc/hosts.toml）

与 agent 无关：这是管理面↔管理面的运维面（多副本零管理的机器簿），
不触碰任何 agent 层。
"""
from fastapi import APIRouter, Depends, HTTPException

from .. import remote
from .auth import require_admin
from .models import HostsPutReq

router = APIRouter()


@router.get("/api/hosts")
def api_hosts(_rec: dict = Depends(require_admin)) -> dict:
    """远端机器清单（WebUI「远端机器」页用）。missing_ok：还没建过清单 → 空表。"""
    return {"hosts": remote.load_hosts(missing_ok=True)}


@router.put("/api/hosts")
def api_hosts_put(req: HostsPutReq, _rec: dict = Depends(require_admin)) -> dict:
    """整表替换（原子写 600）。字段白名单外丢弃；name/host/user 缺一报 400。"""
    try:
        remote.save_hosts(req.hosts)
    except remote.RemoteError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "hosts": remote.load_hosts()}
