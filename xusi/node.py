"""节点身份：name 走 etc/node.json（可改，UI 改）；
id / role 走 etc/xusi.toml（不可改 / 改 role 重启）。

去耦合的不变式：
  - node.json 只存 name（连同 updated_at）
  - id / role 永远以 cfg.node_id / cfg.node_role 为准；本模块不镜像，调用方按需取
  - 任何 toml 改动不在本模块发生（__main__.install 生成初始 id 后回写 toml）
"""
from __future__ import annotations

import json
import socket
import threading
from datetime import datetime, timezone
from pathlib import Path

from .config import get_config
from . import __version__

_LOCK = threading.RLock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load() -> dict:
    f = get_config().node_file
    try:
        d = json.loads(f.read_text("utf-8"))
        if isinstance(d, dict) and isinstance(d.get("name"), str):
            return d
    except FileNotFoundError:
        pass
    except Exception:
        pass
    return {}


def _save(rec: dict) -> None:
    f = get_config().node_file
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(f)


def default_name() -> str:
    """默认名：socket.gethostname()。空时回退 'xusi'。"""
    try:
        return socket.gethostname() or "xusi"
    except Exception:
        return "xusi"


def load_name() -> str:
    """读 etc/node.json 的 name；文件不存在/损坏/无 name 时回退 host。"""
    with _LOCK:
        d = _load()
    n = (d.get("name") or "").strip()
    return n or default_name()


def set_name(name: str) -> dict:
    """改显示名。空字符串或纯空白拒收；过长拒收。返回新记录。"""
    name = (name or "").strip()
    if not name:
        raise ValueError("name 不能为空")
    if len(name) > 64:
        raise ValueError("name 太长（>64字符）")
    with _LOCK:
        rec = _load()
        rec["name"] = name
        rec["updated_at"] = _now_iso()
        _save(rec)
    return rec


def info() -> dict:
    """对外摘要（/api/peer/id、/api/cluster）。不含敏感字段。"""
    cfg = get_config()
    return {
        "id": cfg.node_id or "(unset)",
        "name": load_name(),
        "role": cfg.node_role,
        "version": __version__,
        "url": cfg.public_url,
    }


def can_register_agents() -> bool:
    """仅 worker role 可注册 agent；backup / portal 在 create_agent 入口直接拒绝。"""
    return get_config().node_role == "worker"
