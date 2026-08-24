"""管理面 token：etc/tokens.json —— 谁能调用管理 API、能看到哪些 agent。

两种角色：
- admin：全权（管理全部 agent、签发 token、删除等）；
- user：只能访问 agents 范围内的 agent（观察/投信/经代理访问）。

签发形态（按 cfg.cluster_secret 是否设置自动切换）：
- 单节点（secret 留空，默认）：token = secrets.token_urlsafe(32)，明文存 tokens.json，
  校验走等值比较（防时序侧信道）。完全无新行为，老 token 文件兼容。
- 集群（[cluster].secret 已设）：token = HS256-JWT，载荷 {label, role, agents, iat,
  jti, kpr}；同密钥的所有 xusi 互信（任一节点签发，所有节点通用），跨节点 SSO。
  校验先 JWT（带 kpr=xusi 标记，跨节点收到的 JWT 也能验）；失败回退到明文等值
  （覆盖 secret 由空转非空那一刻的遗留 token）。

不管哪种形态，tokens.json 始终存 {token, label, role, agents, created_at}——
JWT 模式下 token 字段直接是 JWT 字符串本身；list/revoke 不变。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets

from . import registry
from .config import get_config


def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _b64u_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def _jwt_sign(payload: dict, secret: str) -> str:
    header = {"alg": "HS256", "typ": "JWT"}
    h = _b64u(json.dumps(header, separators=(",", ":")).encode())
    p = _b64u(json.dumps(payload, separators=(",", ":"),
                        ensure_ascii=False).encode())
    sig = hmac.new(secret.encode("utf-8"),
                   f"{h}.{p}".encode("ascii"),
                   hashlib.sha256).digest()
    return f"{h}.{p}.{_b64u(sig)}"


def _jwt_verify(token: str, secret: str) -> dict | None:
    """HS256 校验。返回载荷；失败（坏格式/签名错/解密错）返 None。
    当前不强制 exp——本机集群信任语义里 token 直至 revoke，集群侧撤销用 secret 轮换。"""
    parts = token.split(".")
    if len(parts) != 3:
        return None
    h, p, s = parts
    expected = _b64u(hmac.new(secret.encode("utf-8"),
                              f"{h}.{p}".encode("ascii"),
                              hashlib.sha256).digest())
    if not hmac.compare_digest(expected, s):
        return None
    try:
        payload = json.loads(_b64u_decode(p).decode("utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def _cluster_on() -> bool:
    return bool(get_config().cluster_secret)


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
    cfg = get_config()
    now = registry.now_iso()
    if _cluster_on():
        # JWT：载荷是事实源；tokens.json 里 token 字段直接存 JWT，list/revoke 不变
        payload = {
            "label": label or "",
            "role": role,
            "agents": ["*"] if role == "admin" else (agents or []),
            "iat": now,
            "jti": secrets.token_urlsafe(8),
            "kpr": "xusi",
        }
        token = _jwt_sign(payload, cfg.cluster_secret)
        display_label = label or f"{role}-{len(list_tokens()) + 1}"
        rec = {
            "token": token,
            "label": display_label,
            "role": role,
            "agents": payload["agents"],
            "created_at": now,
        }
    else:
        rec = {
            "token": secrets.token_urlsafe(32),
            "label": label or f"{role}-{len(list_tokens()) + 1}",
            "role": role,
            "agents": ["*"] if role == "admin" else (agents or []),
            "created_at": now,
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
    """校验 token，返回 {token, label, role, agents, created_at} 或 None。

    集群（cluster_secret 非空）模式下按"输入形态"分流——这是 secret 轮换正确的关键：
    - 输入是 JWT（dot 数 == 2）：**只走 JWT 路径**。失败直接返 None。
      这样 secret v1 签发的旧 JWT 在 secret 轮换到 v2 后必然签名不匹配 → 拒绝；
      同时也不让"明文回退"去命中 tokens.json 里旧 JWT 的同名字符串绕过轮换。
    - 输入非 JWT（无 / 少 dot 的旧明文 token）：走明文回退，覆盖"先发 token 后开
      [cluster].secret"那一刻的遗留 token；过渡期内既有人仍可继续用。
    - 输入 JWT 但不属于本集群（kpr != 'xusi'）：等同签名错，返 None。

    单节点模式：只看明文（今天的行为，零变化）。"""
    if not token:
        return None
    if _cluster_on():
        if token.count(".") == 2:
            payload = _jwt_verify(token, get_config().cluster_secret)
            if payload and payload.get("kpr") == "xusi":
                return {
                    "token": token,
                    "label": payload.get("label") or "",
                    "role": payload.get("role") or "user",
                    "agents": payload.get("agents") or [],
                    "created_at": payload.get("iat") or "",
                }
            return None
        # 落到这里：非 JWT 输入 + 集群模式 → 走明文回退（只覆盖过渡期遗留 plaintext）
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
