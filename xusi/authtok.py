"""管理面凭证：`etc/xusi.toml` 的 `[admin].secret` 就是 admin token。

单 xusi 单档凭证：admin token 通吃所有 `/api/*`。其它档凭证已全部删除：
- 反代入口 api token（/px /svc 已取消，etc/tokens.json 作废）；
- 互联 token（由 agent 自己发行，经管理邮箱发布，见 mailroom.py）；
- agent 自己的各类凭证（webui_tokens.json 等）由 agent 自己管理，xusi 不碰——
  那是 agent 自家的事。

本模块同时提供 secret 的落盘工具（install / init 共用）。
"""
from __future__ import annotations

import hmac
from pathlib import Path

from .config import get_config


def verify(token: str) -> dict | None:
    """常数时间比对 admin token。匹配返回 `{"token": token}`，否则 None。

    比较走 bytes 形式——str 版 compare_digest 遇非 ASCII 头会 TypeError
    （畸形请求打出 500 而非 401）。"""
    sec = get_config().admin_secret
    if not sec or not token:
        return None
    if hmac.compare_digest(sec.encode("utf-8"), token.encode("utf-8")):
        return {"token": token}
    return None


def admin_secret() -> str:
    """返回本机当前 admin token（管理面启动 banner / CLI 展示用）。"""
    return get_config().admin_secret or ""


# ── secret 落盘（install / init 共用）───────────────────────────────

def write_secret(toml_path: Path, secret: str) -> bool:
    """把 secret = "..." 写进 [admin] 段。段不存在则追加。

    若 toml 里只有旧 [cluster] 段（历史键位），就地把它重命名为 [admin] 再写
    （存量升级顺带收敛键位）。
    """
    try:
        if toml_path.exists():
            text = toml_path.read_text(encoding="utf-8")
        else:
            text = ""
            toml_path.parent.mkdir(parents=True, exist_ok=True)

        # 旧键位收敛：只有 [cluster] 段（无 [admin]）→ 改段头为 [admin]
        if "[admin]" not in text and "[cluster]" in text:
            text = text.replace("[cluster]", "[admin]", 1)

        lines = text.splitlines()
        sec = "admin"
        sec_idx = -1
        in_sec = False
        for i, line in enumerate(lines):
            s = line.strip()
            if s.startswith(f"[{sec}]"):
                in_sec = True
                sec_idx = i
                break
            if s.startswith("["):
                in_sec = False
        new_line = f'secret = "{secret}"'
        if sec_idx >= 0 and in_sec:
            # 段存在：找 secret= 行替换；没有则插入段头后第一行
            replaced = False
            j = sec_idx + 1
            while j < len(lines) and not lines[j].strip().startswith("["):
                k, _, _ = lines[j].strip().partition("=")
                if k.strip() == "secret":
                    lines[j] = new_line
                    replaced = True
                    break
                j += 1
            if not replaced:
                lines.insert(sec_idx + 1, new_line)
            toml_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        else:
            # 段不存在：追加
            append = f"\n[{sec}]\n" + new_line + "\n"
            toml_path.write_text(text.rstrip() + ("\n" if text.strip() else "") + append,
                                 encoding="utf-8")
        try:
            toml_path.chmod(0o600)
        except Exception:
            pass
        return True
    except Exception:
        return False
