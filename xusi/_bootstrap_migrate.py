"""一次性迁移：旧 etc/tokens.json → 新 [cluster].secret。

旧管理面 token 体系（tokens.json + 多把 PLAIN/JWT token）已废。系统启动时若
发现：

1. 老的 etc/tokens.json 仍存在，且 etc/xusi.toml 的 [cluster].secret 为空：
   取该文件里第一条 `role == "admin"` 且非 JWT 形态（不含两个点）的 token，
   写入 [cluster].secret，再把 tokens.json 改名为 `.migrated.YYYYMMDD-HHMMSS`。
   —— 这样老 admin 持有的 token 在新系统里直接可用（admin 通配一切），其它
   任何 token 都失效（与"减法重设计"一致）。

2. 老的 etc/tokens.json 仍存在，且 [cluster].secret 已设：
   把 tokens.json 改名 `.deprecated.YYYYMMDD-HHMMSS` 即结束——不动 secret。

3. 老的 etc/tokens.json 已不存在：no-op。

4. 已经迁过（看到 `.migrated.*` 或 `.deprecated.*` 的同伴文件）：跳过，不
   重复打印同样提示。

幂等。出错一律静默忽略——迁移失败不让服务起不来（首次 install 后老文件
还在的场景：让 admin 手工处理并继续起服务）。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .config import get_config


def _is_jwt(s: str) -> bool:
    return s.count(".") == 2


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def _read_secret_section(toml_path: Path) -> str | None:
    """读 etc/xusi.toml 的 [cluster].secret 当前值（手工解析——避免依赖 tomli/tomllib 写）。"""
    if not toml_path.exists():
        return None
    try:
        text = toml_path.read_text(encoding="utf-8")
    except Exception:
        return None
    in_cluster = False
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if s.startswith("["):
            in_cluster = (s == "[cluster]")
            continue
        if in_cluster:
            k, _, v = s.partition("=")
            if k.strip() == "secret":
                return v.strip().strip('"').strip("'")
    return None  # 段存在但未设 = 与"未写"等价


def _write_secret(toml_path: Path, secret: str) -> bool:
    """把 secret = "..." 写进 [cluster] 段。段不存在则追加。"""
    try:
        if toml_path.exists():
            text = toml_path.read_text(encoding="utf-8")
        else:
            text = ""
            toml_path.parent.mkdir(parents=True, exist_ok=True)
        in_cluster = False
        cluster_idx = -1
        lines = text.splitlines()
        for i, line in enumerate(lines):
            s = line.strip()
            if s.startswith("[cluster]"):
                in_cluster = True
                cluster_idx = i
                break
            if s.startswith("[") and s != "[cluster]":
                in_cluster = False
        new_line = f'secret = "{secret}"'
        if cluster_idx >= 0 and in_cluster:
            # 段存在：找 secret= 行替换；没有则插入段头后第一行
            replaced = False
            j = cluster_idx + 1
            while j < len(lines) and not lines[j].strip().startswith("["):
                k, _, _ = lines[j].strip().partition("=")
                if k.strip() == "secret":
                    lines[j] = f"secret = \"{secret}\""
                    replaced = True
                    break
                j += 1
            if not replaced:
                lines.insert(cluster_idx + 1, new_line)
            toml_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        else:
            # 段不存在：追加
            append = "\n[cluster]\n" + new_line + "\n"
            toml_path.write_text(text.rstrip() + ("\n" if text.strip() else "") + append,
                                 encoding="utf-8")
        try:
            toml_path.chmod(0o600)
        except Exception:
            pass
        return True
    except Exception:
        return False


def _pick_plain_admin(tokens: list[dict]) -> str | None:
    """从 tokens.json 里取第一条 PLAIN（非 JWT）的 admin token。"""
    for t in tokens:
        tok = str(t.get("token", ""))
        if not tok or _is_jwt(tok):
            continue
        role = str(t.get("role", ""))
        if role != "admin":
            continue
        return tok
    return None


def run() -> None:
    cfg = get_config()
    tokens_path = cfg.tokens_file
    if not tokens_path.exists():
        return  # 无老文件，no-op

    # 新约定（api tokens）：etc/tokens.json 是对象 {"tokens": [...]}——这是
    # 反代入口凭证的事实源，**不要碰**。老迁移只针对"顶层是 list"的旧格式。
    try:
        peek = json.loads(tokens_path.read_text(encoding="utf-8"))
    except Exception:
        peek = None
    if isinstance(peek, dict) and isinstance(peek.get("tokens"), list):
        return

    toml_path = cfg.root / "etc" / "xusi.toml"

    # 已迁过：同伴 .migrated.* / .deprecated.* 存在 → 跳过（防重启重复打日志）
    siblings = list(tokens_path.parent.glob(tokens_path.name + ".*"))
    if siblings:
        return

    # 当前 [cluster].secret 已设：仅弃用老 tokens.json
    current = _read_secret_section(toml_path)
    if current:
        try:
            tokens_path.rename(tokens_path.with_name(
                tokens_path.name + f".deprecated.{_stamp()}"))
        except Exception:
            pass
        return

    # 读老 tokens.json
    try:
        data = json.loads(tokens_path.read_text(encoding="utf-8"))
    except Exception:
        return
    if not isinstance(data, list):
        return
    picked = _pick_plain_admin(data)
    if not picked:
        # 没有可迁的 admin token（只剩 JWT 残留 / 已全废）：仅弃用
        try:
            tokens_path.rename(tokens_path.with_name(
                tokens_path.name + f".deprecated.{_stamp()}"))
        except Exception:
            pass
        return

    if not _write_secret(toml_path, picked):
        return

    # 改名 tokens.json → .migrated.* 留底
    try:
        tokens_path.rename(tokens_path.with_name(
            tokens_path.name + f".migrated.{_stamp()}"))
    except Exception:
        pass

    print(f"\n[xusi-bootstrap] 已把 etc/tokens.json 里的 PLAIN admin token 接管为"
          f" [cluster].secret（etc/tokens.json 改名 .migrated.* 留底）。")
    print(f"[xusi-bootstrap] 该 admin token 现已成为本机 admin token；新成员"
          f" 通过 `xusi init --cluster-secret <该值>` 加入集群。\n")
