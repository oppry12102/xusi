"""反代入口凭证：`etc/tokens.json` 里的 api token。

仅供外部反代服务（如 voidhub 形态的 App / 经 8601 反代调 agent 自建服务的外部
客户端）使用。admin 签发、admin 吊销；**明文存盘**（文件权限 600，admin 视角），
供 `services.public_access_text()` 在 agent 详情页拼"对外接入说明"时直接读取。

跟另外两档凭证完全隔离：
- `[cluster].secret`（admin token）——管理面全权
- 该 agent 的 `webui_tokens.json`——仅该 agent 的 /v1 /ui

api token **只**进 `/px /svc` 两个反代入口（`/v1/health` 探活也放行——
token_routed 的例外）；`/api/*`（含本接口本身）
一律不认（任何端点若想被 api token 通，必须显式调 `apitokens.verify()`）。

token 格式：`secrets.token_urlsafe(32)`（43 字符 URL-safe base64，无 padding）。
"""
from __future__ import annotations

import hmac
import json
import secrets
import string
import threading
from datetime import datetime, timezone
from pathlib import Path

from .config import get_config


_LOCK = threading.Lock()


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _id4() -> str:
    """token id 后缀：4 字符 URL-safe（跟 agent id 后缀同风格），前缀 'tk_'。"""
    alpha = string.ascii_lowercase + string.digits
    return "tk_" + "".join(secrets.choice(alpha) for _ in range(6))


# ── 文件 IO ──────────────────────────────────────────────────────────

def _path() -> Path:
    return get_config().tokens_file


def load() -> list[dict]:
    """读 etc/tokens.json；不存在/解析失败/格式坏 → []（不抛，让 verify 走不到）。"""
    p = _path()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except Exception:
        return []
    rows = data.get("tokens") if isinstance(data, dict) else None
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def _save(rows: list[dict]) -> None:
    """原子写：先写 .tmp 再 rename（避免半截文件被并发读）。权限 600。"""
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps({"tokens": rows}, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    try:
        tmp.chmod(0o600)
    except OSError:
        pass
    tmp.replace(p)
    try:
        p.chmod(0o600)
    except OSError:
        pass


# ── 验证 / 管理 ──────────────────────────────────────────────────────

def verify(token: str) -> dict | None:
    """反代入口凭 token 验真：匹配返回 rec，不匹配返回 None。
    比较走 bytes 的 compare_digest（与 authtok 同款——非 ASCII 也稳）。"""
    if not token:
        return None
    for rec in load():
        tok = rec.get("token")
        if tok and hmac.compare_digest(tok.encode("utf-8"), token.encode("utf-8")):
            return rec
    return None


def mint(label: str = "") -> tuple[str, dict]:
    """签发新 api token：返 (明文 token, 记录 dict)。明文存盘，admin 视角下次可读。"""
    label = (label or "").strip()[:64]
    for _ in range(8):
        new_id = _id4()
        if not any(r.get("id") == new_id for r in load()):
            break
    else:
        raise RuntimeError("api token id 冲突 8 次——请重试")
    token = secrets.token_urlsafe(32)
    rec = {
        "id": new_id,
        "token": token,
        "label": label,
        "created_at": _stamp(),
    }
    with _LOCK:
        rows = load()
        rows.append(rec)
        _save(rows)
    return token, rec


def revoke(token_id: str) -> bool:
    """按 id 吊销：找到则删除该记录并写盘，返 True；未找到返 False。"""
    token_id = (token_id or "").strip()
    if not token_id:
        return False
    with _LOCK:
        rows = load()
        new_rows = [r for r in rows if r.get("id") != token_id]
        if len(new_rows) == len(rows):
            return False
        _save(new_rows)
        return True


def list_tokens() -> list[dict]:
    """列 token：admin 视角，含明文（文件本身只 admin 可读）。"""
    return [{"id": r.get("id", ""),
             "token": r.get("token", ""),
             "label": r.get("label", ""),
             "created_at": r.get("created_at", "")}
            for r in load()]


def latest_token() -> str | None:
    """最新一枚 api token 的明文（按 created_at 倒序）；用于对外接入说明。
    多枚时取最近签发那枚——admin 刚签给某外部调用方的那枚。无 token → None。"""
    rows = sorted(load(), key=lambda r: r.get("created_at", ""), reverse=True)
    for r in rows:
        if r.get("token"):
            return r["token"]
    return None