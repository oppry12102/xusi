"""主密钥池：etc/brains.toml —— 管理员维护的大脑（LLM 厂商）模板。

创建 agent 时从这里取模板，连同 api_key 直写进 agent 的 config.toml（600）
——出生配置。此后 config.toml 归 agent 自治：改 mission / 调预算投信让
agent 自己改（内核每个大循环热重载，无需重启）。**唯一例外**：大脑段——
改参接口按密钥池手术式重渲染 [brain] + [brains.*] 段（agentops
_rewrite_brain_sections，下次呼吸生效，不重启），其余段绝不触碰。
xusi 不读回 config.toml（手术改写是唯一写入口）。
"""
from __future__ import annotations

import json
import re
import time
import tomllib
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .config import get_config
from .versions import at_least

# 渲染进 agent config.toml 时允许透传的可选字段（v2 config 认识的）
_OPTIONAL_FIELDS = ("temperature", "timeout", "tier", "price_prompt", "price_completion",
                    "context_window")

# 老名 → 新名（池条目改名的一次性别名）：厂商段展开为多模型平级条目后，
# 老段名（deepseek/glm）在池里查无此人——校验时自动升级并落快照，
# 旧快照与旧备份的 PATCH/restore 都走得通。
_LEGACY_ALIASES = {
    "deepseek": "deepseek-v4-flash",   # 原 [brains.deepseek] model = deepseek-v4-flash
    "glm": "glm-5.3",                  # 原 [brains.glm] model = glm-5.2 → 平级新大脑
}

# 内核 v2.7.5 起清理了 [agent] 预算段：max_seconds 删除、max_context_tokens
# 改为自动派生（同档可用脑最小窗口 − 8k，现场活算）；可配置限额只剩
# [limits] max_rounds。更早内核仍认 [agent] 三段——出生配置按所选内核
# 版本渲染，写错段 = 限额静默失效（旧核不认 [limits]，新核不认 [agent]）。
_LIMITS_STYLE_SINCE = "2.7.5"


def _load_pool() -> dict[str, dict]:
    """加载密钥池：一家厂商一个 [brains.X] 段。

    `models = [...]`（可选）：该厂商段展开为**每模型一个平级池条目**——
    条目名 = 模型名（模型名自带厂商前缀），api_key/base_url/tier/temperature/
    context_window/note/price 等其余字段继承厂商段，模型间不分级。没有
    models 的厂商段照旧单条目（名字 = 段名，model = 单值）。"""
    f: Path = get_config().brains_file
    try:
        with f.open("rb") as fp:
            raw = tomllib.load(fp)
    except FileNotFoundError:
        return {}
    except Exception:
        return {}
    out: dict[str, dict] = {}
    for name, spec in raw.get("brains", {}).items():
        if not isinstance(spec, dict):
            continue
        spec = dict(spec)
        models = spec.get("models")
        if isinstance(models, list) and models:
            for m in models:
                if not isinstance(m, str) or not m.strip():
                    continue
                entry = {k: v for k, v in spec.items() if k != "models"}
                entry["model"] = m.strip()
                out[m.strip()] = entry
        else:
            out[str(name)] = spec
    for entry in out.values():
        _normalize_context_window(entry)
    return out


def _normalize_context_window(spec: dict) -> None:
    """context_window 类型归一："190000" → 190000。字符串会让护栏推导
    静默跳过，而内核侧仍按 int 解析——管理面与内核两套规则必须同源。
    接受正整数串（含 "+190000" / " 190000 "），以及小数位全 0 / 科学计数
    表示的整数（"190000.0" / "1.9e5"）；非整数浮点串（"190000.5"）拒绝——按
    整数语义，避免静默丢精度。"""
    w = spec.get("context_window")
    if isinstance(w, str):
        s = w.strip()
        try:
            i = int(s)
            if i > 0:
                spec["context_window"] = i
        except ValueError:
            try:
                f = float(s)
                if f > 0 and f.is_integer():
                    spec["context_window"] = int(f)
            except (ValueError, OverflowError):
                pass


def pool_names() -> list[str]:
    """密钥池里的大脑名（有序）。"""
    return list(_load_pool().keys())


def pool_specs() -> dict[str, dict]:
    """密钥池全量条目（展开后，含 api_key）——仅进程内 CLI 用（check-brains），
    绝不进 HTTP 响应（对外展示走 pool_summary）。"""
    return _load_pool()


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


def validate_selection(bl: list[str]) -> list[str]:
    """校验大脑选择：非空、都在密钥池里、都有 key。失败抛 ValueError（带用户可读信息）。

    返回**规范化列表**：老名自动升级为新名（_LEGACY_ALIASES——池条目改名后
    旧快照/旧备份里的名字仍走得通，升级结果由调用方落快照）。
    create / patch / restore 共用同一份断言——恢复侧原先手抄了一份等价检查，
    两处会各自漂移，收敛到这里。"""
    if not bl:
        raise ValueError("至少选择一家大脑")
    pool = _load_pool()
    normalized: list[str] = []
    for b in bl:
        b = str(b)
        if b in pool:
            normalized.append(b)
            continue
        nb = _LEGACY_ALIASES.get(b)
        if nb and nb in pool:
            normalized.append(nb)
            continue
        raise ValueError(f"密钥池中没有这个大脑：{b}")
    dups = sorted({b for b in normalized if normalized.count(b) > 1})
    if dups:
        raise ValueError(f"大脑列表有重复（渲染会产生坏 TOML）：{', '.join(dups)}")
    no_key = [b for b in normalized if not pool[b].get("api_key")]
    if no_key:
        raise ValueError(f"这些大脑没配 api_key（etc/brains.toml）：{', '.join(no_key)}")
    return normalized


def _q(s: Any) -> str:
    """TOML 基本字符串：JSON 字符串转义规则与 TOML 兼容。"""
    return json.dumps(str(s), ensure_ascii=False)


_BARE_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_\-]+$")


def _section(segs: list[str]) -> str:
    """段路径 → 段头行。含点的段须**逐段**引号：裸段头里的点是分层符，
    [brains.glm-5.3] 会解析成嵌套表 brains.glm-5."3"；而整串引号
    ["brains.glm-5.3"] 会变成一个字面键（点不再分层）——正确写法是
    [brains."glm-5.3"]。调用方按逻辑段传入（脑名是一个段，其中的点
    不参与分层）。"""
    return "[" + ".".join(s if _BARE_SEGMENT_RE.match(s) else f'"{s}"'
                          for s in segs) + "]"


def render_brain_blocks(chosen: list[str]) -> list[str]:
    """每个所选大脑一个 [brains.<name>] 块（含 note/economy 提示注释、
    api_key/base_url/model、_OPTIONAL_FIELDS 透传，块尾 "" 空行）。

    chosen 已去重保序。render_agent_config（出生配置）与 agentops 的
    改参手术共用这份渲染——两处必须同源。"""
    pool = _load_pool()
    lines: list[str] = []
    for name in chosen:
        spec = pool[name]
        lines.append(_section(["brains", name]))
        # 面向智能体的使用提示（渲染注释通道，内核解析值不受影响）：note 是
        # 该脑特有事实（如"免费（自托管）"）；economy 档再补一条档位通用提示
        # （上下文受限、子 agent/批量任务优先）。"免费"不是档位的定义，各家
        # 脑用 note 自述，代码不替它说。
        note = spec.get("note")
        if isinstance(note, str) and note.strip():
            for ln in note.strip().splitlines():
                lines.append(f"# {ln.strip()}")
        if str(spec.get("tier") or "") == "economy":
            win = spec.get("context_window")
            lim = (f"（上限 {int(win)} tokens）"
                   if isinstance(win, (int, float)) and int(win) > 0 else "")
            lines.append(f"# 经济档（tier=economy）：上下文受限{lim}。")
            lines.append("# 子 agent 与批量粗活（摘要/分类/记忆分析）优先走这家；长会话主回路不适合。")
        lines.append(f"api_key = {_q(spec.get('api_key', ''))}")
        lines.append(f"base_url = {_q(spec.get('base_url', ''))}")
        lines.append(f"model = {_q(spec.get('model', ''))}")
        for k in _OPTIONAL_FIELDS:
            if k in spec and spec[k] not in ("", None):
                v = spec[k]
                lines.append(f"{k} = {_q(v)}" if isinstance(v, str) else f"{k} = {v}")
        lines.append("")
    return lines


def render_brain_section(chosen: list[str]) -> list[str]:
    """[brain] default 段 + 全部 [brains.*] 块（chosen[0] = default）。

    render_agent_config 与 agentops 改参手术共用——出生配置与手术改写
    必须逐字节同源。"""
    return ["[brain]", f"default = {_q(chosen[0])}", ""] + render_brain_blocks(chosen)


def render_agent_config(mission: str, brains: list[str], budgets: dict | None = None,
                        display_timezone: str | None = None,
                        source_version: str = "",
                        instance_id: str = "",
                        roots: list | None = None,
                        extra_config: str = "") -> str:
    """渲染 agent 的 config.toml 全文（注册表数据 → 配置文件，单向渲染）。

    ⚠ brains 列表顺序即语义：chosen[0] 渲染为 [brain] default——主回路
    首选脑与故障转移分档的锚点。

    预算只透传管理员的显式 budgets（0 = 不限）；不做推导——会话预算的
    缺省推导（同档最小窗口 − 8k）是内核自己的事务（xuseek 现场活算、显式
    值优先、热重载即时生效），管理面不替它做决策，也不烙过期快照。

    预算段的格式随 source_version（创建时选定的内核版本）走：
    - ≥2.7.5：[limits] 段只写 max_rounds——内核已删 max_seconds、
      max_context_tokens 改自动派生，这两个键收到也渲染不进配置；
    - 更早版本：[agent] 段写 max_rounds / max_seconds / max_context_tokens。
    出生配置必须匹配内核认识的 schema（写错段 = 限额静默失效）。

    roots（可选，v2.7.12+ 内核）：渲染 [[roots]] 数组表——根智能体出生
    交割键，首次启动预检时一次性交割到 workspace/playbook/根智能体.json，
    交割后即死键（版本门槛校验在 agentops._validate_roots）。

    extra_config（可选）：管理员手写的自由 TOML（[capabilities] 等内核可选段
    或未来新段）原样追加到文件末尾——xusi 不必追踪内核每个新配置段。
    落盘前整体 tomllib 校验：渲染产物必须是合法 TOML（坏段直接拒绝创建）。"""
    pool = _load_pool()
    # 去重保序：重复名会渲染出两个同名段 → 坏 TOML（validate_selection 已
    # 拦常规路径，这里防直调绕过）
    chosen = list(dict.fromkeys(b for b in brains if b in pool))
    if not chosen:
        raise ValueError("密钥池中没有任何可用的大脑（etc/brains.toml）")
    cfg = get_config()
    tz = display_timezone or cfg.display_timezone
    b = dict(budgets or {})

    lines = [
        "# ═══════════════════════════════════════════════════════════════════",
        "# 本文件由墟司（xusi 管理面）在创建时渲染一次——出生配置。",
        "# 此后归你（agent）自治：改 mission / 调预算请直接编辑（内核每个大循环",
        "# 热重载，无需重启；改前建议自行备份）。例外：大脑段 [brain] + [brains.*]",
        "# 由管理面改参按密钥池重渲染——你在这两段里的手改会被覆盖（下次呼吸生效）。",
        "# 新大脑的 api_key 可经管理邮箱向管理员索取。",
        "# ═══════════════════════════════════════════════════════════════════",
        "",
        f"mission = {_q(mission)}",
        f'display_timezone = {_q(tz)}',
        "",
    ]
    if instance_id:
        lines[-1:] = [
            "# 你的终身 id（世界唯一、迁移随行、永不改变——本文件归你自治，",
            "# 但这一行请勿修改：它是迁移/克隆时认亲的唯一凭据。",
            "# 跨实例场合（登记目录/署名/提及身份）照抄全串，不缩写不补全）",
            f'instance_id = {_q(instance_id)}',
            "",
        ]
    lines.extend(render_brain_section(chosen))
    # 预算段：格式随内核版本（schema 不匹配 = 限额静默失效，见模块头常量）。
    # 两个分支都只写管理员显式给的键（0 = 不限），不做推导；缺省不写段，
    # 由内核按自身默认（v2.7.5 按大脑窗口自动派生）处理。
    if b:
        if at_least(source_version, _LIMITS_STYLE_SINCE):
            lines.append("# 探索回路安全网（v2.7.5+：可配置限额只剩 max_rounds，0 = 不限；")
            lines.append("# max_context_tokens 由内核按大脑窗口自动派生，max_seconds 已移除；")
            lines.append("# 热重载即时生效）")
            lines.append("[limits]")
            for k in ("max_rounds",):
                if k in b:
                    lines.append(f"{k} = {int(b[k])}")
            dropped = [k for k in ("max_seconds", "max_context_tokens") if k in b]
            if dropped:
                lines.append(f"# 这些键已由内核接管/移除，渲染时忽略：{', '.join(dropped)}")
        else:
            lines.append("# 探索回路安全网（旧内核 [agent] 段：显式键优先，0 = 不限；热重载即时生效）")
            lines.append("[agent]")
            for k in ("max_rounds", "max_seconds", "max_context_tokens"):
                if k in b:
                    lines.append(f"{k} = {int(b[k])}")
        lines.append("")
    # [[roots]]（可选）：根智能体出生交割段（v2.7.12+ 内核认识；版本门槛
    # 由 agentops._validate_roots 把关，此处只负责渲染）
    lines.extend(_render_roots(roots))
    # 附加配置（可选）：管理员自由 TOML 原样追加（逃生舱，见模块 docstring）
    extra = (extra_config or "").strip()
    if extra:
        lines.extend(["", extra])
    text = "\n".join(lines)
    # 落盘前整体校验：出生配置必须是合法 TOML（内核 preflight 对坏 TOML 静默
    # 保持旧值——渲染侧必须挡住，否则坏附加配置会静默生效为零配置）
    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ValueError(f"渲染出的 config.toml 无法解析（附加配置写坏？）：{e}") from None
    return text


def _render_roots(roots: list | None) -> list[str]:
    """[[roots]] 段：每个根智能体一个数组表（address/token 已由
    _validate_roots 校验齐备、去重保序）。"""
    if not roots:
        return []
    lines = [
        "# ── 根智能体（目录服务）——互联发现的唯一方案（内核 docs/interconnect.md）──",
        "# 首次启动一次性交割到 workspace/playbook/根智能体.json；交割后此段即失效（死键）。",
        "# 重交割 = 删该文件 + 改此段 + 重启。token 可写 \"env:变量名\"。",
        "",
    ]
    for r in roots:
        lines.append("[[roots]]")
        lines.append(f"address = {_q(r['address'])}")
        lines.append(f"token = {_q(r['token'])}")
        lines.append("")
    return lines


def write_agent_config(home: Path, mission: str, brains: list[str],
                       budgets: dict | None = None,
                       source_version: str = "",
                       instance_id: str = "",
                       roots: list | None = None,
                       extra_config: str = "") -> Path:
    """渲染并写入 <home>/config.toml（chmod 600，含 api_key）。

    只在创建时调用——出生配置，首写即终写；此后该文件归 agent 自治，
    xusi 不再读回、不再重渲染。source_version = 内核版本（预算段格式
    随它走，见 render_agent_config）；instance_id = 终身 id（出生时
    交割给实例，此后它自带身份迁移，注册表只是缓存）。
    """
    text = render_agent_config(mission, brains, budgets,
                               source_version=source_version,
                               instance_id=instance_id,
                               roots=roots, extra_config=extra_config)
    p = home / "config.toml"
    p.write_text(text, encoding="utf-8")
    p.chmod(0o600)
    return p


def probe_brain(spec: dict, timeout: float = 30.0) -> dict:
    """连通性实测：GET /models（模型在册？）+ 最小 chat（max_tokens=512）。

    请求不带 temperature（各家都接受缺省；kimi 只允许服务端默认）。
    纯标准库（urllib）——CLI-only 远端机器直跑（「先测再加」固化成命令）。
    单脑失败不抛，errors 进报告——其余脑照测。"""
    base = str(spec.get("base_url") or "")
    key = str(spec.get("api_key") or "")
    model = str(spec.get("model") or "")
    rep: dict = {"model": model, "models_listed": False, "chat_status": 0,
                 "chat_latency": 0.0, "reply": "", "errors": []}
    if not (base and key and model):
        rep["errors"].append("缺 base_url/api_key/model")
        return rep
    headers = {"Authorization": f"Bearer {key}"}
    try:
        req = urllib.request.Request(base.rstrip("/") + "/models", headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = json.loads(r.read().decode())
        ids = [m.get("id") for m in body.get("data", []) if isinstance(m, dict)]
        # 只作参考，不算失败：部分厂商的 /models 不枚举别名（如 kimi coding
        # 端点认 kimi-k3 但列表里没有）——chat 200 才是可用性的硬标准。
        rep["models_listed"] = model in ids
        if not rep["models_listed"]:
            shown = ", ".join(ids[:12]) + ("…" if len(ids) > 12 else "")
            rep["models_note"] = f"/models 未列出（chat 可用即算过）" + \
                (f"（在册：{shown}）" if shown else "")
    except Exception as e:
        rep["models_note"] = f"/models 失败（chat 可用即算过）：{e}"
    payload = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": "ping（连通性测试），只回 ok 两个字母"}],
        "max_tokens": 512,
    }).encode()
    req = urllib.request.Request(base.rstrip("/") + "/chat/completions", data=payload,
                                 headers={**headers, "Content-Type": "application/json"},
                                 method="POST")
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = json.loads(r.read().decode())
        rep["chat_status"] = r.status
        rep["chat_latency"] = round(time.time() - t0, 1)
        try:
            rep["reply"] = str(body["choices"][0]["message"]["content"] or "").strip()
        except (KeyError, IndexError):
            rep["errors"].append("chat 响应结构异常（没有 choices[0].message.content）")
    except urllib.error.HTTPError as e:
        rep["chat_status"] = e.code
        try:
            detail = str(json.loads(e.read().decode()))[:160]
        except Exception:
            detail = ""
        rep["errors"].append(f"chat HTTP {e.code}" + (f"：{detail}" if detail else ""))
    except Exception as e:
        rep["errors"].append(f"chat 失败：{e}")
    return rep
