"""管理面 token：etc/tokens.json —— 谁可以调用管理 API。

本系统只供管理员使用，不对外服务——管理面 token **统一为 admin**：
- `?mtoken=` 或 `Authorization: Bearer` 传入；
- 签发形态：**PLAIN**（secrets.token_urlsafe(32)，明文存 tokens.json）；
- 跨节点转发时由 `sign_jwt_for(rec)` 现场把 caller 的 PLAIN 包装成短期 JWT
  （默认 5 分钟），peer 端用同密钥验签后再 enforce 作用域（admin 通配 '*'）。

历史背景：`role` 字段曾在 schema 里区分 admin / user，user 限制 agents 范围。
当前系统统一只签 admin；启动时 `_load()` 把存量的 `role="user"` 记录静默升 admin
（agents 列表保留但已无意义——admin 通配）。`is_admin()` / `can_access()` 已删除，
caller 不再做角色/范围检查（所有 token 都是 admin）。

tokens.json schema：{token, label, role, agents, created_at}，role 字段保留以便
list 接口展示，但新签发的 role 永远是 "admin"。"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time

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
    """HS256 校验。返回载荷；失败（坏格式/签名错/exp 过期）返 None。

    exp 字段是**可选**的——只在签发时显式设了才生效：
    - sign_jwt_for 给 forward_to_peer 用，签短期 JWT（默认 5 分钟）→ 带 exp，
      过期后 peer 端 verify 自动拒绝，避免长生命周期临时凭证被滥用。
    - new_token 给用户用的长生命周期 token 不带 exp——直至 revoke 或 secret 轮换。
    """
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
    exp = payload.get("exp")
    if exp is not None:
        try:
            if int(time.time()) > int(exp):
                return None   # 过期——签发方设了 ttl，过期即作废
        except (ValueError, TypeError):
            return None   # exp 字段格式坏，等同无效 token
    return payload


def _cluster_on() -> bool:
    return bool(get_config().cluster_secret)


def is_jwt(token: str) -> bool:
    """判断 token 是否为 JWT 形态（xxx.yyy.zzz）。仅供内部区分用——不验签。

    用户层只签 PLAIN，但 tokens.json 里可能仍有老 cluster 模式自动签
    的 JWT 残留、且 sign_jwt_for 会给 peer 现场包装 JWT——这两条路径都需要识别。
    """
    return token.count(".") == 2


def _load() -> dict:
    f = get_config().tokens_file
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("tokens"), list):
            return data
    except Exception:
        pass
    return {"tokens": []}


def _migrate(data: dict) -> dict:
    """启动时静默迁移：role=user → role=admin（agents 列表保留但无意义——admin 通配）。

    系统当前只签 admin，但旧的 tokens.json 可能还留着 user 记录（CLI 兼容路径
    也已删，避免再签新的）。一次性迁完让 list 接口显示一致，revoke 也无需特判。
    """
    changed = False
    for t in data.get("tokens", []):
        if isinstance(t, dict) and t.get("role") == "user":
            t["role"] = "admin"
            agents = t.get("agents") or []
            if agents != ["*"]:
                t["agents"] = ["*"]
                changed = True
            changed = True
    if changed:
        try:
            f = get_config().tokens_file
            f.parent.mkdir(parents=True, exist_ok=True)
            tmp = f.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            tmp.replace(f)
            f.chmod(0o600)
        except Exception:
            pass   # 迁移写回失败不致命——下次启动再试
    return data


def _save(data: dict) -> None:
    f = get_config().tokens_file
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(f)
    f.chmod(0o600)


def list_tokens() -> list[dict]:
    return list(_migrate(_load())["tokens"])


def new_token(label: str = "", rotate: bool = False) -> dict:
    """签发管理面 admin token——统一 PLAIN 形态（secrets.token_urlsafe(32)）。

    rotate=True：先 revoke 既有 PLAIN，再签发新的——用户层「一把 active」
    永远是最近签的。

    rotate=True 时默认 label 加 unix 时间戳后缀（admin-r1724567890），多次 rotate
    后 list 里每把都能一眼区分；非 rotate 时仍按位置计数（admin-1/admin-2/...）。"""
    cfg = get_config()
    now = registry.now_iso()
    if rotate and not label:
        display_label = f"admin-r{int(time.time())}"
    elif label:
        display_label = label
    else:
        display_label = f"admin-{len(list_tokens()) + 1}"
    token = secrets.token_urlsafe(32)
    rec = {
        "token": token,
        "label": display_label,
        "role": "admin",
        "agents": ["*"],
        "created_at": now,
    }
    data = _migrate(_load())
    if rotate:
        # 仅 revoke PLAIN 形态的 token（JWT 是 xusi 内部事务，不动）
        data["tokens"] = [
            t for t in data["tokens"]
            if not (t.get("role") == "admin" and not is_jwt(t["token"]))
        ]
    data["tokens"].append(rec)
    _save(data)
    return rec


def revoke_token(prefix: str) -> int:
    if len(prefix) < 8:
        raise ValueError("请提供至少 8 位 token 前缀")
    data = _migrate(_load())
    before = len(data["tokens"])
    data["tokens"] = [t for t in data["tokens"] if not t["token"].startswith(prefix)]
    _save(data)
    return before - len(data["tokens"])


def sign_jwt_for(rec: dict, *, ttl_seconds: int = 300) -> str | None:
    """当场签短期 JWT，**仅 xusi 内部使用**（跨节点转发时给 peer 验签）。

    用途：caller 的 token 是 PLAIN，peer 那边 tokens.json 里查不到——自动从 rec 提
    取 claims 当场签 JWT 给 peer。caller 已是 JWT 时透传不重签（保留签发者署名）。

    rec：verify() 返回的 rec（含 label/agents/iat）。
    ttl_seconds：默认 5 分钟足够 cover 一次跨节点请求往返——payload 里写 exp，
    peer 端 _jwt_verify 会检查，过期自动拒绝。
    返回 None：集群模式未开（无法签 JWT）或 caller 已是 JWT。"""
    if not _cluster_on():
        return None
    tok = (rec.get("token") or "").strip()
    if is_jwt(tok):
        # 已是 JWT——透传由 forward_to_peer 直接发，不重签
        return None
    cfg = get_config()
    payload = {
        "label": rec.get("label") or "",
        "role": "admin",
        "agents": ["*"],
        "iat": rec.get("created_at") or registry.now_iso(),
        "exp": int(time.time()) + ttl_seconds,
        "jti": secrets.token_urlsafe(8),
        "kpr": "xusi",
    }
    return _jwt_sign(payload, cfg.cluster_secret)


def verify(token: str) -> dict | None:
    """校验 token，返回 {token, label, role, agents, created_at} 或 None。

    校验路径：
    1. JWT（dot 数 == 2）：用 cluster_secret 验签 + 检查 exp + kpr=xusi。
       用户层签发的都是 PLAIN（参见 `new_token`），这条路径只为兼容历史 tokens.json
       里的 JWT 残留保留；verify 仍接受。
    2. PLAIN（任何 dot 数 < 2 的输入）：tokens.json 里等值比较（防时序侧信道）。

    集群（cluster_secret 非空）模式下 JWT 输入**只走 JWT 路径**——失败直接 None，
    不让明文回退去命中 tokens.json 里旧 JWT 的同名字符串绕过 secret 轮换。"""
    if not token:
        return None
    if is_jwt(token):
        if _cluster_on():
            payload = _jwt_verify(token, get_config().cluster_secret)
            if payload and payload.get("kpr") == "xusi":
                return {
                    "token": token,
                    "label": payload.get("label") or "",
                    "role": "admin",
                    "agents": ["*"],
                    "created_at": payload.get("iat") or "",
                }
            return None
        # 集群未开：JWT 输入视为无效（没密钥验签）
        return None
    for t in _load()["tokens"]:
        if hmac.compare_digest(t["token"], token):
            return t
    return None