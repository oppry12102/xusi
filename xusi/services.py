"""agent 自建对外服务的发现清单：services.json 读取 + 注册表登记 + 合并 + 探活。

文件即通道（延续 webui_tokens.json / mailbox.jsonl 先例）：agent 是自己运行时
状态的事实源，它把自建服务写进清单，管理面每次请求实时读取——agent 换端口、
换 token，UI 与反代自动跟随，管理面无需任何配置。

清单文件（UTF-8 JSON 数组，agent 自己维护，管理面只读）：
  canonical: <home>/workspace/data/services.json   （agent 的 run_shell 顺手写的地方）
  兼容:      <home>/data/services.json             （与 outbox.jsonl 的 agent→管理面通道对称）
  同名时 workspace 侧优先（agent 最新态）。

另一来源是注册表 agent["services"]（管理员 WebUI 登记的兜底，文件同名条目遮蔽之）。
非法输入逐级降级（缺文件=空清单、坏 JSON=整文件忽略、坏条目=跳过），errors 带回 UI。
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

import httpx

from . import agentops, registry
from .config import get_config

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


class ServiceError(RuntimeError):
    """业务错误（登记/删除时的用户可读信息，API 层转 400）。"""


def manifest_paths(agent: dict) -> list[Path]:
    """清单候选路径（顺序即优先级：后面的覆盖前面）。"""
    home = get_config().instance_home(agent["id"])
    return [home / "data" / "services.json",
            home / "workspace" / "data" / "services.json"]


def _parse_manifest(p: Path) -> tuple[list[dict], list[str]]:
    """读一个清单文件。返回 (原始条目列表, 错误信息列表)。缺失视为空清单。"""
    try:
        raw = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        return [], []
    except Exception as e:
        return [], [f"{p.name}（{p.parent.name}/…）不可读：{type(e).__name__}"]
    try:
        entries = json.loads(raw)
        if not isinstance(entries, list):
            raise ValueError("顶层必须是数组")
    except Exception as e:
        return [], [f"services.json 解析失败（{p.parent.name}/{p.name}）：{e}"]
    out: list[dict] = []
    errs: list[str] = []
    for i, e in enumerate(entries, 1):
        if not isinstance(e, dict):
            errs.append(f"{p.parent.name}/{p.name} 第 {i} 条：不是对象，跳过")
            continue
        out.append(e)
    return out, errs


def validate_entry(raw: dict, source: str) -> tuple[dict | None, str | None]:
    """normalize + 校验单条。通过返回脱敏条目（不含 token 内容，只有 auth 布尔），
    不通过返回 (None, 原因)。"""
    name = str(raw.get("name", "")).strip()
    if not _NAME_RE.fullmatch(name):
        return None, f"{name or '(空)'}：name 须匹配 {_NAME_RE.pattern}"
    try:
        port = int(raw.get("port", 0))
    except (TypeError, ValueError):
        return None, f"{name}：port 不是整数"
    if not (1 <= port <= 65535):
        return None, f"{name}：port 越界（1-65535）"

    base_path = str(raw.get("base_path", "") or "").strip()
    if base_path and not base_path.startswith("/"):
        base_path = "/" + base_path
    base_path = base_path.rstrip("/")

    token_file = str(raw.get("token_file", "") or "").strip()
    if token_file and (token_file.startswith("/") or ".." in Path(token_file).parts):
        return None, f"{name}：token_file 须为相对 agent home 的路径（禁绝对路径/..）"

    openapi = raw.get("openapi", "/openapi.json")
    openapi = openapi if openapi is False else (str(openapi) or "/openapi.json")

    return {
        "name": name,
        "port": port,
        "title": str(raw.get("title", "") or "") or name,
        "base_path": base_path,
        "openapi": openapi,
        "probe": str(raw.get("probe", "/") or "/"),
        "token_file": token_file or None,
        "readonly": bool(raw.get("readonly", False)),
        "note": str(raw.get("note", "") or ""),
        "source": source,          # "file"（agent 自声明）| "registry"（管理员登记）
    }, None


def merge_services(agent: dict) -> tuple[list[dict], list[str], list[str]]:
    """合并双来源 → (服务条目按名排序, errors, 被文件遮蔽的注册表条目名)。"""
    out: dict[str, dict] = {}
    errors: list[str] = []
    for p in manifest_paths(agent):
        entries, errs = _parse_manifest(p)
        errors.extend(errs)
        for e in entries:
            norm, err = validate_entry(e, source="file")
            if norm is None:
                errors.append(f"{p.parent.name}/{p.name}：{err}")
                continue
            out[norm["name"]] = norm          # workspace 侧后读，同名覆盖
    shadowed: list[str] = []
    for e in agent.get("services", []) or []:
        norm, err = validate_entry(e, source="registry")
        if norm is None:
            errors.append(f"注册表 services：{err}")
            continue
        if norm["name"] in out:
            shadowed.append(norm["name"])    # 文件优先（agent 是运行时事实源）
            continue
        out[norm["name"]] = norm
    return sorted(out.values(), key=lambda s: s["name"]), errors, shadowed


def find_service(agent: dict, name: str) -> dict | None:
    svcs, _, _ = merge_services(agent)
    for s in svcs:
        if s["name"] == name:
            return s
    return None


def _token_path(agent: dict, svc: dict) -> Path | None:
    if not svc.get("token_file"):
        return None
    return get_config().instance_home(agent["id"]) / svc["token_file"]


def service_token(agent: dict, svc: dict) -> str | None:
    """实时读服务 token（每次转发都读，agent 轮换 token 自动跟随）。空/不可读 → None。"""
    p = _token_path(agent, svc)
    if p is None:
        return None
    try:
        tok = p.read_text(encoding="utf-8").strip()
    except Exception:
        return None
    return tok or None


def probe_service(svc: dict) -> dict:
    """探活：连 127.0.0.1:port{base_path}{probe}。ok = 连上且 status<500（401/404 也算活着）。"""
    url = f"http://127.0.0.1:{svc['port']}{svc.get('base_path', '')}{svc.get('probe', '/')}"
    t0 = time.monotonic()
    try:
        r = httpx.get(url, timeout=1.5, follow_redirects=True)
        return {"ok": r.status_code < 500, "status": r.status_code,
                "ms": int((time.monotonic() - t0) * 1000)}
    except Exception as e:
        return {"ok": False, "status": None, "ms": None, "note": type(e).__name__}


def list_services(agent: dict, *, probe: bool = True) -> dict:
    """清单聚合（API 端点用）：合并 + auth 标记 + 端口池警告 + 可选探活。"""
    cfg = get_config()
    svcs, errors, shadowed = merge_services(agent)
    out = []
    for s in svcs:
        row = dict(s)
        row["auth"] = service_token(agent, s) is not None
        if cfg.port_lo <= s["port"] <= cfg.port_hi:
            row["warn"] = f"端口 {s['port']} 在管理面分配池 [{cfg.port_lo},{cfg.port_hi}] 内，可能与新 agent 冲突（建议 8700-8799）"
        if probe:
            row["health"] = probe_service(s)
        out.append(row)
    return {"id": agent["id"], "services": out, "errors": errors, "shadowed": shadowed}


# ── 注册表登记（管理员兜底；agent 自声明走文件）────────────────────

def add_registry(agent_id: str, req: dict) -> dict:
    agent = registry.get_agent(agent_id)
    if not agent:
        raise ServiceError(f"agent 不存在: {agent_id}")
    norm, err = validate_entry(req, source="registry")
    if norm is None:
        raise ServiceError(f"服务条目非法：{err}")
    cfg = get_config()
    if norm["port"] == cfg.port:
        raise ServiceError(f"端口 {norm['port']} 是管理面自身端口")
    used = registry.used_ports()
    if norm["port"] in used:
        raise ServiceError(f"端口 {norm['port']} 已被 agent {used[norm['port']]} 占用")
    entries = [e for e in (agent.get("services") or []) if e.get("name") != norm["name"]]
    keep = [{k: v for k, v in e.items() if k not in ("source", "auth", "health", "warn")}
            for e in entries]
    registry.update_agent(agent_id, {"services": keep + [req]})
    agentops.audit("service.add", agent=agent_id, name=norm["name"], port=norm["port"])
    return list_services(registry.get_agent(agent_id) or agent)


def remove_registry(agent_id: str, name: str) -> dict:
    agent = registry.get_agent(agent_id)
    if not agent:
        raise ServiceError(f"agent 不存在: {agent_id}")
    reg_entries = agent.get("services") or []
    if not any(e.get("name") == name for e in reg_entries):
        # 注册表里没有：若文件里自声明了，提醒找 agent；否则就是这个名字不存在
        svc = find_service(agent, name)
        if svc and svc["source"] == "file":
            raise ServiceError(f"服务 {name} 由 agent 在 services.json 里自声明，管理面不代改——请投信让 agent 修改")
        raise ServiceError(f"注册表里没有服务 {name}")
    entries = [e for e in reg_entries if e.get("name") != name]
    registry.update_agent(agent_id, {"services": entries})
    agentops.audit("service.remove", agent=agent_id, name=name)
    return list_services(registry.get_agent(agent_id) or agent)


def service_names(agent: dict) -> list[str]:
    svcs, _, _ = merge_services(agent)
    return [s["name"] for s in svcs]
