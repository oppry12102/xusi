"""注册表：etc/agents.json —— 管理面自己的数据（agent 档案 + 期望态）。

注册表是 agent 参数的唯一事实源：mission / brains / 端口 / 暴露开关 / 预算都以
这里为准，agent home 里的 config.toml 永远由本模块的数据渲染出来。
写入原子（tmp + os.replace），进程内加锁。
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


def record_token(agent_id: str, token: str, label: str) -> None:
    with _LOCK:
        data = _load()
        for a in data["agents"]:
            if a.get("id") == agent_id:
                a.setdefault("tokens", []).append(
                    {"token": token, "label": label, "created_at": now_iso()})
                a["updated_at"] = now_iso()
                _save(data)
                return


def drop_token(agent_id: str, token_prefix: str) -> int:
    """按前缀移除已记录的 token（撤销时用）。返回移除条数。"""
    with _LOCK:
        data = _load()
        n = 0
        for a in data["agents"]:
            if a.get("id") == agent_id:
                keep = []
                for t in a.get("tokens", []):
                    if t.get("token", "").startswith(token_prefix):
                        n += 1
                    else:
                        keep.append(t)
                a["tokens"] = keep
                a["updated_at"] = now_iso()
                _save(data)
                break
    return n


def used_ports() -> dict[int, str]:
    """注册表里已被占用的端口 → agent_id（分配时避开）。"""
    return {int(a["port"]): a["id"] for a in list_agents() if a.get("port")}


def ids() -> list[str]:
    return [a["id"] for a in list_agents()]


def next_seq() -> int:
    """自增序号（agent 编号展示用）。"""
    with _LOCK:
        return len(_load()["agents"]) + 1
