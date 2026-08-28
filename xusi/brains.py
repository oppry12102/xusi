"""主密钥池：etc/brains.toml —— 管理员维护的大脑（LLM 厂商）模板。

创建/改参时从这里取模板，连同 api_key 直写进 agent 的 config.toml（600），
agent 保持「目录即自主体」的自洽性。key 轮换：改本文件 + PATCH 触发重渲染，
agent 每个大循环热重载 config，无需重启。

config.toml 整文件重渲染时的**保真义务**（capabilities 契约二）：墟司只写自己
认识的段；内核/大脑写入的段（如 [capabilities]，及未来一切内核段）原样保真
回传。能力包分工裁决：墟司只负责种子（内核 init 无条件播），启用与否、依赖
安装归大脑——墟司不写 [capabilities]，只读它来观察。
"""
from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path
from typing import Any

from .config import get_config

# 渲染进 agent config.toml 时允许透传的可选字段（v2 config 认识的）
_OPTIONAL_FIELDS = ("temperature", "timeout", "tier", "price_prompt", "price_completion",
                    "context_window")

# 上下文护栏的输出余量（tokens）：vLLM 对 prompt == max_model_len 直接 400，
# 内核「超顶优雅结束」又按上次成功调用的 prompt_tokens 事后判定——不留余量
# 的话护栏永远晚一步。预留 8k 让 80% 提醒/优雅收尾先于硬错触发。
_CONTEXT_RESERVE_TOKENS = 8_192

# config.toml 里墟司拥有（每次渲染重写）的段；其余段一律保真回传。
# "" = 顶层键（mission/display_timezone）。[brains.*] 用前缀匹配（大脑名动态）。
_OWNED_SECTIONS = {"", "brain", "agent"}
_OWNED_PREFIXES = ("brains.",)

_SECTION_RE = re.compile(r"^\s*\[\[?\s*([A-Za-z0-9_.\-]+)\s*\]?\]?\s*(?:#.*)?$")


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
            spec = dict(spec)
            # context_window 类型归一："190000" → 190000。字符串会让护栏推导
            # 静默跳过，而内核侧仍按 int 解析——管理面与内核两套规则必须同源。
            # 接受正整数串（含 "+190000" / " 190000 "），以及小数位全 0 / 科学计数
            # 表示的整数（"190000.0" / "1.9e5"）；非整数浮点串（"190000.5"）拒绝——按
            # 整数语义，避免静默丢精度。
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
            out[str(name)] = spec
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


def validate_selection(bl: list[str]) -> None:
    """校验大脑选择：非空、都在密钥池里、都有 key。失败抛 ValueError（带用户可读信息）。

    create / patch / restore 共用同一份断言——恢复侧原先手抄了一份等价检查，
    两处会各自漂移，收敛到这里。"""
    if not bl:
        raise ValueError("至少选择一家大脑")
    dups = sorted({b for b in bl if bl.count(b) > 1})
    if dups:
        raise ValueError(f"大脑列表有重复（渲染会产生坏 TOML）：{', '.join(dups)}")
    pool = {b["name"]: b for b in pool_summary()}
    unknown = [b for b in bl if b not in pool]
    if unknown:
        raise ValueError(f"密钥池中没有这些大脑：{', '.join(unknown)}")
    no_key = [b for b in bl if not pool[b]["has_key"]]
    if no_key:
        raise ValueError(f"这些大脑没配 api_key（etc/brains.toml）：{', '.join(no_key)}")


def _q(s: Any) -> str:
    """TOML 基本字符串：JSON 字符串转义规则与 TOML 兼容。"""
    return json.dumps(str(s), ensure_ascii=False)


def _failover_class(spec: dict) -> str:
    """大脑的经济分档（[brains.X] tier；未打标签视同 power——与内核 v2.5.5+
    的 _tier_of 同义：历史存量的未标注脑都是主力型号）。

    内核事实（xuseek-v2 v2.5.5+ llm.py）：故障转移**同档循环**——主循环与
    llm_call(tier=) 一样只在同档大脑之间转移，跨档切换走管理员（PATCH 换
    default，重渲染即生效）。更早内核（≤v2.5.3）主循环是全池轮转，跨档也会
    接盘——但小窗脑超窗 400/预检跳过，接不住胖会话。预算按 default 同档取
    最小对两类内核都成立：同档是全部可能接盘者，且大窗脑不该被跨档小窗脑
    拖累。"""
    return str(spec.get("tier") or "power")


def _owned(section: str) -> bool:
    return section in _OWNED_SECTIONS or section.startswith(_OWNED_PREFIXES)


def extract_foreign_sections(text: str) -> str:
    """从旧 config.toml 里逐字抽出墟司不认识的段（内核所有，如 [capabilities]）。

    文本级抽取（非 parse→re-serialize）：段头（含 [[array]]）到下一个段头之间的
    全部行原样保留——键、注释、空行、书写顺序一个不动。顶层键区与墟司自己的段
    （[brain]/[brains.*]/[agent]）不抽（那些本就该被重渲染覆盖）。

    已知边界：多行字符串里出现形如段头的行会被误切。当前内核段（如
    [capabilities]）是扁平 k=v、无多行字符串，假设成立；内核若引入多行字符串
    需要同步这里（改 tomllib round-trip 或按语法跳过字符串体）。
    """
    blocks: list[str] = []
    cur: list[str] = None  # type: ignore[assignment]
    for line in text.splitlines():
        m = _SECTION_RE.match(line)
        if m:
            if cur is not None:
                blocks.append("\n".join(cur).rstrip())
            cur = [line] if not _owned(m.group(1)) else None
        elif cur is not None:
            cur.append(line)
    if cur is not None:
        blocks.append("\n".join(cur).rstrip())
    return "\n\n".join(b for b in blocks if b.strip())


def render_agent_config(mission: str, brains: list[str], budgets: dict | None = None,
                        display_timezone: str | None = None) -> str:
    """渲染 agent 的 config.toml 全文（注册表数据 → 配置文件，单向渲染）。

    ⚠ brains 列表顺序即语义：chosen[0] 渲染为 [brain] default——它既是主
    回路首选脑，也是预算推导（同档取最小）与故障转移分档的锚点。管理员
    调换 brains 顺序 = 静默换默认大脑（PATCH brains 原样写回也会重渲染换锚），
    换锚后预算随新档重算。

    budgets 为 None 且 default 大脑的同类（tier 相同，未打标签视同 power）都
    未声明 context_window 时，[agent] 预算段一个键都不写（内核默认 = 全不限）；
    否则只写给出的/推导出的键（0 = 不限）。"""
    pool = _load_pool()
    # 去重保序：重复名会渲染出两个同名段 → 坏 TOML（validate_selection 已
    # 拦常规路径，这里防直调绕过）
    chosen = list(dict.fromkeys(b for b in brains if b in pool))
    if not chosen:
        raise ValueError("密钥池中没有任何可用的大脑（etc/brains.toml）")
    cfg = get_config()
    tz = display_timezone or cfg.display_timezone
    b = dict(budgets or {})

    # 上下文护栏：内核缺省 max_context_tokens=1M，小于此的服务（如 190k 级
    # 自托管 vLLM）会在护栏触发前撞硬错。取 default 同档（tier 相同，未打
    # 标签视同 power）已声明 context_window 的最小值，扣输出余量折进预算：
    # 内核 v2.5.4+ 故障转移同档循环，同档即全部自动接盘者；更早内核全池轮转
    # 时跨档小窗脑也接不住胖会话（超窗 400/预检被跳过）——两种情况下预算都
    # 不该被跨档小窗脑拖累。人工换档走 PATCH 重渲染，预算随新 default 重算。
    # 显式预算优先（0=不限是管理员的显式意志，渲染注释承诺的语义不破坏）；
    # 推导只补缺。显式值宽于物理窗口时不静默收紧——事实写进渲染注释，硬墙
    # 由内核按脑预检兜底，注册表回显与实际生效值保持一致。同档都没声明则不动。
    cls = _failover_class(pool[chosen[0]])
    declared = [int(pool[n]["context_window"]) for n in chosen
                if _failover_class(pool[n]) == cls
                and isinstance(pool[n].get("context_window"), (int, float))
                and int(pool[n]["context_window"]) > 0]
    cap = min(declared) - _CONTEXT_RESERVE_TOKENS if declared else 0
    if cap > 0 and "max_context_tokens" not in b:
        b["max_context_tokens"] = cap

    lines = [
        "# ═══════════════════════════════════════════════════════════════════",
        "# 本文件由墟司（xusi 管理面）渲染生成 —— 参数的唯一事实源是管理面注册表。",
        "# 手工改动会在下次改参时被覆盖；新增大脑请编辑管理面的 etc/brains.toml。",
        "# 内核所有的段（如 [capabilities]）由墟司保真回传，不在此渲染范围内。",
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
    # 预算段：缺省一个都不写（xuseek 自身默认 = 全不限，LLM 完全自主）；
    # 仅写给出的键（0 = 不限）。max_context_tokens 可能来自上面的物理护栏推导
    if b:
        lines.append("# 探索回路安全网（显式键优先，0 = 不限；缺省补窗口推导；热重载即时生效）")
        lines.append("[agent]")
        for k in ("max_rounds", "max_seconds", "max_context_tokens"):
            if k in b:
                lines.append(f"{k} = {int(b[k])}")
        if cap > 0:
            lines.append(f"# 物理护栏参考：default 同档最小窗口 ≈ {cap}（context_window 推导，扣 8k 余量）")
            # 显式 max_context_tokens 超过物理窗口：渲染里给醒目告警。
            # 真要收紧：把 b[k] 改写成 min(int(b[k]), cap)，并在 #4
            # 那段设计注释里去掉"硬墙由内核按脑预检兜底"那一句。
            explicit_mct = b.get("max_context_tokens")
            if isinstance(explicit_mct, (int, float)) and int(explicit_mct) > cap:
                lines.append(
                    f"# ⚠ max_context_tokens={int(explicit_mct)} 超过 default 物理护栏 {cap}，"
                    f"按设计不收紧；硬墙依赖内核按脑预检，若预检未生效会超窗 400。"
                )
        lines.append("")
    return "\n".join(lines)


def write_agent_config(home: Path, mission: str, brains: list[str],
                       budgets: dict | None = None) -> Path:
    """渲染并写入 <home>/config.toml（chmod 600，含 api_key）。

    整文件重渲染前先读旧文件，把墟司不认识的段（内核所有，如 [capabilities]）
    逐字保真接回文末——保真义务（capabilities 契约二）。首次写入（init 刚生成）
    时旧文件里的 [capabilities] 由 init --capability 写入，同样被接续保留。
    """
    text = render_agent_config(mission, brains, budgets)
    p = home / "config.toml"
    try:
        old = p.read_text(encoding="utf-8")
    except Exception:
        old = ""
    foreign = extract_foreign_sections(old)
    if foreign:
        text = text.rstrip("\n") + "\n\n# ── 以下段为内核所有（墟司保真回传，不渲染）──\n" + foreign + "\n"
    p.write_text(text, encoding="utf-8")
    p.chmod(0o600)
    return p


# ── [capabilities] 段的只读观察（契约二：段是实例能力的唯一事实源，归内核/大脑）──

def read_capabilities(home: Path) -> dict[str, bool]:
    """读 <home>/config.toml 的 [capabilities]（缺段/坏文件 = 全关）。"""
    try:
        with (home / "config.toml").open("rb") as f:
            raw = tomllib.load(f)
    except Exception:
        return {}
    caps = raw.get("capabilities")
    if not isinstance(caps, dict):
        return {}
    return {str(k): bool(v) for k, v in caps.items() if isinstance(v, bool)}
