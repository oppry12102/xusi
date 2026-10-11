"""管理面凭证：两档——admin token + 智能体专属 token。

- admin token = `etc/xusi.toml` 的 `[admin].secret`，通吃所有 `/api/*`；
- 智能体专属 token = 注册表记录的 `access_token` 字段（管理面 PATCH 改参
  按需签发），属地受限：只能访问绑定的那一个 agent，管理能力（创建/改参/
  删除/备份/远端）全部 403。

其余凭证仍归各家：
- 根智能体 token（创建时经 [[roots]] 渲染进出生 config，内核交割后即死键——
  那是 xuseek 互联自家的事）；
- agent 自己的各类凭证（webui_tokens.json 等）由 agent 自己管理，xusi 不碰——
  那是 agent 自家的事。

本模块同时提供 secret 的落盘工具（install / init 共用）。
"""
from __future__ import annotations

import hmac
from pathlib import Path

from .config import get_config


def verify(token: str) -> dict | None:
    """双路比对：先 admin（常数时间），再查注册表的专属 token。

    匹配返回带角色的 rec：admin → `{"token", "role": "admin"}`；
    专属 → `{"token", "role": "agent", "agent_id", "name"}`；否则 None。

    比较走 bytes 形式——str 版 compare_digest 遇非 ASCII 头会 TypeError
    （畸形请求打出 500 而非 401）。admin 分支永远先走。"""
    sec = get_config().admin_secret
    if sec and token and hmac.compare_digest(sec.encode("utf-8"), token.encode("utf-8")):
        return {"token": token, "role": "admin"}
    if token:
        from . import registry          # 延迟 import：registry 反向依赖 config，顶层会环
        rec = registry.find_by_access_token(token)
        if rec:
            return {"token": token, "role": "agent",
                    "agent_id": rec["id"], "name": rec.get("name", "")}
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
