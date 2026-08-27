"""管理面凭证：`etc/xusi.toml` 的 `[cluster].secret` 就是 admin token。

同 secret 的所有 xusi 互信：admin 拿这把 token 登任何一台都能访问所有资源；
跨节点转发也只是把 Authorization 头原样透传过去，peer 端用同样的常时间比对
接收。

三档凭证完全隔离、各管各的：
- `[cluster].secret`（admin token）——管理面全权（写端点只认它）；
- 反代入口 api token（`etc/tokens.json`，见 apitokens.py）——admin 签发、
  吊销，**只**进 `/px /svc`（`/v1` 仅 health 探活）反代入口，`/api/*` 一律不认；
- 该 agent 自己的 `webui_tokens.json`——仅该 agent 的 `/v1 /ui /px`。

agent 观察台 token 由 agent 自己管，那是 agent 自家的事，跟管理面凭证无关。
过去的 7 类凭证 / 3 套 JWT 体系 / invitation bootstrap 已全部归零。
"""
from __future__ import annotations

import hmac

from .config import get_config


def verify(token: str) -> dict | None:
    """常数时间比对 `[cluster].secret`。匹配返回 `{"token": token}`，否则 None。

    被任意请求携带管理员 token 时验证唯一身份。比较走 bytes 形式——str 版
    compare_digest 遇非 ASCII 头会 TypeError（畸形请求打出 500 而非 401）。"""
    sec = get_config().cluster_secret
    if not sec or not token:
        return None
    if hmac.compare_digest(sec.encode("utf-8"), token.encode("utf-8")):
        return {"token": token}
    return None


def cluster_secret() -> str:
    """返回本机当前 admin token（管理面启动 banner / CLI 展示用）。"""
    return get_config().cluster_secret or ""
