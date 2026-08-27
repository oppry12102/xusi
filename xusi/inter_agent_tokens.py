"""智能体互联 token：每 xusi 一把，集群内 agent ↔ agent 互调 /svc 时用。

跟 api token 同结构（存盘、明文、admin 管），但隔离清晰：
- admin token（cluster_secret）—— 管理面全权，不外泄
- api token（etc/tokens.json）—— 外部反代服务（voidhub App 等），revoke 影响外部
- **互联 token（本文件，etc/inter_agent_tokens.json）—— 同集群 agent 互调**
  /svc 时用；revoke 仅影响内部 agent 通信，不影响 admin、不影响外部服务。
- webui token（每 agent）—— 该 agent 的 /v1 /ui /px

性质：
- **每 xusi 一把**（不是每 agent 一把）—— 一台 xusi 持 0 或 1 把，给该 xusi
  上所有 agent 公用。主目的不是鉴权而是"8601 端口透传门票"（同集群信任
  域内 agent → /svc 入口的入场券）。
- **不验目标** —— 持合法互联 token 进 /svc 任一服务都通（与 api token
  同语义：持票即登机）。services.json 决定哪些服务公开，xusi 不做"为谁
  专门开放"的细粒度授权。
- **集群 fan-in 跨节点时** —— 远端 xusi 自带它那把互联 token，本机
  /api/agent-peers 聚合时按 peer 归属填充；远端没签发时该 peer 行
  inter_agent_token 字段省略。
- **不自动签发** —— admin 一手动作为准（与 api token 同生命周期管理）。
  想关就 DELETE，想开就 POST。

token 格式：secrets.token_urlsafe(32)（43 字符 URL-safe base64，无 padding）。
"""
from __future__ import annotations

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
    """record id 后缀：URL-safe 6 字符，前缀 'iat_'（inter-agent token）。"""
    alpha = string.ascii_lowercase + string.digits
    return "iat_" + "".join(secrets.choice(alpha) for _ in range(6))


# ── 文件 IO ──────────────────────────────────────────────────────────

def _path() -> Path:
    return get_config().inter_agent_tokens_file


def load() -> list[dict]:
    """读 etc/inter_agent_tokens.json；不存在/解析失败/格式坏 → []。

    一 xusi 只允许一把互联 token。list 形式仅为结构对称；调用方应当用
    current() 取单值而非遍历。"""
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
    """原子写：先写 .tmp 再 rename。权限 600。"""
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


# ── 验证 / 查询 ──────────────────────────────────────────────────────

def verify(token: str) -> dict | None:
    """凭 token 验真：匹配返回 rec，不匹配返回 None。"""
    if not token:
        return None
    for rec in load():
        if rec.get("token") and rec["token"] == token:
            return rec
    return None


def current() -> dict | None:
    """当前那把互联 token 记录（若无则 None）。

    /api/agent-peers 在填充本地 peer 行时调这个，把明文 token 写进
    inter_agent_token 字段让 peer agent 直接拿去用。"""
    rows = load()
    return rows[0] if rows else None


def get_token() -> str | None:
    """当前那把互联 token 的明文字符串（无记录则 None）。"""
    rec = current()
    return rec.get("token") if rec else None


# ── 管理 ─────────────────────────────────────────────────────────────

def mint(label: str = "") -> tuple[str, dict]:
    """签发互联 token：若已存在则直接返现有那条（不重发——避免覆盖正在用的）。

    设计原因：互联 token 是集群共享的入口凭证，不是"每用户一把"。重复签发
    会立刻让所有已下发的副本失效。轮换路径：先 DELETE，再 POST。

    返回 (明文 token, 记录 dict)。"""
    label = (label or "").strip()[:64]
    with _LOCK:
        rows = load()
        if rows:
            # 已存在：返现有（admin 视角落盘可读，POST 不重发新 token）
            rec = rows[0]
            return rec.get("token", ""), rec
        for _ in range(8):
            new_id = _id4()
            if not any(r.get("id") == new_id for r in rows):
                break
        else:
            raise RuntimeError("inter-agent token id 冲突 8 次——请重试")
        token = secrets.token_urlsafe(32)
        rec = {
            "id": new_id,
            "token": token,
            "label": label or "inter-agent",
            "created_at": _stamp(),
        }
        rows.append(rec)
        _save(rows)
    return token, rec


def revoke(token_id: str) -> bool:
    """按 id 吊销：找到则删除并写盘，返 True；未找到返 False。"""
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
    """列互联 token：admin 视角，含明文（文件本身只 admin 可读）。"""
    return [{"id": r.get("id", ""),
             "token": r.get("token", ""),
             "label": r.get("label", ""),
             "created_at": r.get("created_at", "")}
            for r in load()]
