"""注册表：etc/agents.json —— 管理面自己的簿记（agent 档案 + 期望态）。

注册表记的是管理面侧的事实：id/name/端口/暴露开关/期望态/创建时的
mission·brains·budgets·roots 快照（出生配置已渲染进 config.toml，
此后归 agent 自治，快照仅供展示）。

写入原子（tmp + os.replace，600——roots 快照含根 token 明文），进程内加锁。
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_LOCK = threading.RLock()

# 期望态：running（常驻呼吸）/ stopped（停机，不自动拉起）/ paused（SIGSTOP 冻结）
DESIRED_STATES = ("running", "stopped", "paused")


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _agents_file() -> Path:
    from .config import get_config
    return get_config().agents_file


def _load() -> dict:
    f = _agents_file()
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("agents"), list):
            return data
    except FileNotFoundError:
        pass
    except Exception:
        pass
    return {"agents": []}


def _save(data: dict) -> None:
    f = _agents_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        tmp.chmod(0o600)   # roots 快照含根 token 明文，比 644 更稳
    except OSError:
        pass
    tmp.replace(f)


def list_agents() -> list[dict]:
    with _LOCK:
        return list(_load()["agents"])


def get_agent(agent_id: str) -> dict | None:
    with _LOCK:
        for a in _load()["agents"]:
            if a.get("id") == agent_id:
                return a
    return None


def add_agent(rec: dict) -> dict:
    with _LOCK:
        data = _load()
        data["agents"].append(rec)
        _save(data)
    return rec


def update_agent(agent_id: str, patch: dict) -> dict | None:
    """合并更新（浅合并顶层键），自动刷新 updated_at。"""
    with _LOCK:
        data = _load()
        for a in data["agents"]:
            if a.get("id") == agent_id:
                a.update(patch)
                a["updated_at"] = now_iso()
                _save(data)
                return a
    return None


def remove_agent(agent_id: str) -> bool:
    with _LOCK:
        data = _load()
        before = len(data["agents"])
        data["agents"] = [a for a in data["agents"] if a.get("id") != agent_id]
        if len(data["agents"]) == before:
            return False
        _save(data)
    return True


def used_ports() -> dict[int, str]:
    """注册表里已被占用的端口 → agent_id（分配时避开）。"""
    return {int(a["port"]): a["id"] for a in list_agents() if a.get("port")}
