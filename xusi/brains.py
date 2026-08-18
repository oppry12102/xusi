"""主密钥池：etc/brains.toml —— 管理员维护的大脑（LLM 厂商）模板。

创建/改参时从这里取模板，连同 api_key 直写进 agent 的 config.toml（600），
agent 保持「目录即自主体」的自洽性。key 轮换：改本文件 + PATCH 触发重渲染，
agent 每个大循环热重载 config，无需重启。
"""
from __future__ import annotations

import json
import tomllib
from pathlib import Path
from typing import Any

from .config import get_config

# 渲染进 agent config.toml 时允许透传的可选字段（v2 config 认识的）
_OPTIONAL_FIELDS = ("temperature", "timeout", "tier", "price_prompt", "price_completion")


def _load_pool() -> dict[str, dict]:
    f: Path = get_config().brains_file
    try:
        with f.open("rb") as fp:
            raw = tomllib.load(fp)
    except FileNotFoundError:
        return {}
    except Exception:
        return {}
    out = {}
    for name, spec in raw.get("brains", {}).items():
        if isinstance(spec, dict):
            out[str(name)] = dict(spec)
    return out


def pool_names() -> list[str]:
    """密钥池里的大脑名（有序）。"""
    return list(_load_pool().keys())


def pool_summary() -> list[dict]:
    """对外展示（绝不回 api_key）：名称、model、是否已配 key。"""
    return [
        {
            "name": name,
            "base_url": str(spec.get("base_url", "")),
            "model": str(spec.get("model", "")),
            "has_key": bool(spec.get("api_key")),
        }
        for name, spec in _load_pool().items()
    ]


def get_brain(name: str) -> dict | None:
    return _load_pool().get(name)


def _q(s: Any) -> str:
    """TOML 基本字符串：JSON 字符串转义规则与 TOML 兼容。"""
    return json.dumps(str(s), ensure_ascii=False)


def render_agent_config(mission: str, brains: list[str], budgets: dict | None = None,
                        display_timezone: str | None = None) -> str:
    """渲染 agent 的 config.toml 全文（注册表数据 → 配置文件，单向渲染）。"""
    pool = _load_pool()
    chosen = [b for b in brains if b in pool]
    if not chosen:
        raise ValueError("密钥池中没有任何可用的大脑（etc/brains.toml）")
    cfg = get_config()
    tz = display_timezone or cfg.display_timezone
    b = budgets or {}

    lines = [
        "# ═══════════════════════════════════════════════════════════════════",
        "# 本文件由墟司（xusi 管理面）渲染生成 —— 参数的唯一事实源是管理面注册表。",
        "# 手工改动会在下次改参时被覆盖；新增大脑请编辑管理面的 etc/brains.toml。",
        "# ═══════════════════════════════════════════════════════════════════",
        "",
        f"mission = {_q(mission)}",
        f'display_timezone = {_q(tz)}',
        "",
        "[brain]",
        f"default = {_q(chosen[0])}",
        "",
    ]
    for name in chosen:
        spec = pool[name]
        lines.append(f"[brains.{name}]")
        lines.append(f"api_key = {_q(spec.get('api_key', ''))}")
        lines.append(f"base_url = {_q(spec.get('base_url', ''))}")
        lines.append(f"model = {_q(spec.get('model', ''))}")
        for k in _OPTIONAL_FIELDS:
            if k in spec and spec[k] not in ("", None):
                v = spec[k]
                lines.append(f"{k} = {_q(v)}" if isinstance(v, str) else f"{k} = {v}")
        lines.append("")
    # 预算段：缺省一个都不写（xuseek 自身默认 = 全不限，LLM 完全自主）；
    # 仅当显式给 budgets 时写出，且只写给出的键（0 = 不限）
    if b:
        lines.append("# 探索回路安全网（仅管理面显式指定的键；0 = 不限；热重载即时生效）")
        lines.append("[agent]")
        for k in ("max_rounds", "max_seconds", "max_context_tokens"):
            if k in b:
                lines.append(f"{k} = {int(b[k])}")
        lines.append("")
    return "\n".join(lines)


def write_agent_config(home: Path, mission: str, brains: list[str],
                       budgets: dict | None = None) -> Path:
    """渲染并写入 <home>/config.toml（chmod 600，含 api_key）。"""
    text = render_agent_config(mission, brains, budgets)
    p = home / "config.toml"
    p.write_text(text, encoding="utf-8")
    p.chmod(0o600)
    return p
