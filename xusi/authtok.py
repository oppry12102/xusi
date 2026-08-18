"""管理面 token：etc/tokens.json —— 谁能调用管理 API、能看到哪些 agent。

两种角色：
- admin：全权（管理全部 agent、签发 token、删除等）；
- user：只能访问 agents 范围内的 agent（观察/投信/经代理访问）。

token 为随机 urlsafe(32)，明文存于 600 权限文件（管理员随时可读回，与
xuseek 观察台 token 的朴素管理方式同构）。签发走本机 CLI，不经 HTTP。
"""
from __future__ import annotations

import hmac
import json
import secrets

from . import registry
from .config import get_config


def _load() -> dict:
    f = get_config().tokens_file
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("tokens"), list):
            return data
    except Exception:
        pass
    return {"tokens": []}


def _save(data: dict) -> None:
    f = get_config().tokens_file
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(f)
    f.chmod(0o600)


def list_tokens() -> list[dict]:
    return list(_load()["tokens"])


def new_token(label: str = "", role: str = "user", agents: list[str] | None = None) -> dict:
    if role not in ("admin", "user"):
        raise ValueError("role 须为 admin 或 user")
    rec = {
        "token": secrets.token_urlsafe(32),
        "label": label or f"{role}-{len(list_tokens()) + 1}",
        "role": role,
        "agents": ["*"] if role == "admin" else (agents or []),
        "created_at": registry.now_iso(),
    }
    data = _load()
    data["tokens"].append(rec)
    _save(data)
    return rec


def revoke_token(prefix: str) -> int:
    if len(prefix) < 8:
        raise ValueError("请提供至少 8 位 token 前缀")
    data = _load()
    before = len(data["tokens"])
    data["tokens"] = [t for t in data["tokens"] if not t["token"].startswith(prefix)]
    _save(data)
    return before - len(data["tokens"])


def verify(token: str) -> dict | None:
    """校验管理面 token；返回记录或 None。等值比较防时序侧信道。"""
    if not token:
        return None
    for t in _load()["tokens"]:
        if hmac.compare_digest(t["token"], token):
            return t
    return None


def is_admin(rec: dict) -> bool:
    return rec.get("role") == "admin"


def can_access(rec: dict, agent_id: str) -> bool:
    """admin 或范围含该 agent（'*' 通配）。"""
    if is_admin(rec):
        return True
    scopes = rec.get("agents") or []
    return "*" in scopes or agent_id in scopes
