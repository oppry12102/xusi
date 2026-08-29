"""互联信箱（mailroom）：agent 经管理邮箱发布的互联声明与目录申请处理。

通道与方向（唯一接口 = 管理邮箱）：
- agent → xusi：内核 send_mail 工具写 <home>/data/outbox.jsonl（sender=brain）；
  xusi 的后台扫描线程每 5s 增量读取（字节偏移持久化到 etc/outbox_state.json）。
- xusi → agent：复用 agentops.mail() 追加 mailbox.jsonl（sender=admin，内核同语义）。

信封协议（text 内嵌 JSON，宽容解析——agent 是 LLM，可能把信封包在散文里）：

    {"xusi":"publish","port":8765,"token":"<agent 自签互联 token>","host":"10.0.0.5"}
    {"xusi":"request_directory"}
    {"xusi":"directory","generated_at":"…","entries":[
        {"id","name","host","port","token","published_at"}]}

- publish：幂等 = 覆盖更新（published_at 刷新）。port 必填 int、token 必填非空、
  host 可选（缺省 127.0.0.1，跨机互联由 agent 自己填 LAN 可达地址）。
- request_directory：回执只含「已发布互联且非申请者自身」的条目；无人发布则
  entries=[]（agent 由此知道当前无人可联）。
- directory：只 audit 忽略（防环——agent 把回执转回来不产生新动作）。
- 未知 kind：audit 后忽略（前向兼容）。

身份规则：发送者 = outbox 文件归属的 agent（扫描循环已知），信封不自报 id——
防止 agent A 冒充 B 发布。

偏移持久化独立于注册表（etc/outbox_state.json，600，原子写）：偏移是 xusi 的
纯簿记，塞注册表会让每轮扫描抖动 agent 行的 updated_at 且与 API 争锁。
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from . import agentops, registry
from .config import get_config

_SCAN_INTERVAL_S = 5.0
ENVELOPE_KINDS = ("publish", "request_directory", "directory")

_LOCK = threading.RLock()
_last_err: dict[str, str] = {}   # 每 agent 最近一次扫描错误（审计去重，防 5s 刷屏）


# ── 信封解析 ─────────────────────────────────────────────────────────

def parse_envelope(text: str) -> dict | None:
    """从一封信的 text 里宽容解析信封：dict 且含 "xusi" 键才算。

    先整段 json.loads；失败则逐 '{' raw_decode，取第一个含 "xusi" 键的 dict
    （借鉴旧 capabilities.py 扫 '[' 的手法）——散文包裹也能识别。
    全失败返回 None（非信封，静默忽略）。"""
    t = (text or "").strip()
    if not t:
        return None
    dec = json.JSONDecoder()
    try:
        obj, end = dec.raw_decode(t)
        if isinstance(obj, dict) and "xusi" in obj and end == len(t):
            return obj
    except ValueError:
        pass
    for i, ch in enumerate(t):
        if ch != "{":
            continue
        try:
            obj, _ = dec.raw_decode(t, i)
        except ValueError:
            continue
        if isinstance(obj, dict) and "xusi" in obj:
            return obj
    return None


def directory_envelope(entries: list[dict]) -> dict:
    return {"xusi": "directory", "generated_at": registry.now_iso(), "entries": entries}


# ── outbox 增量扫描（偏移持久化）─────────────────────────────────────

def _state_path() -> Path:
    return get_config().outbox_state_file


def _load_state() -> dict:
    try:
        d = json.loads(_state_path().read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save_state(st: dict) -> None:
    p = _state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
    try:
        tmp.chmod(0o600)
    except OSError:
        pass
    tmp.replace(p)


def forget(agent_id: str) -> None:
    """agent 删除时清其扫描偏移（卫生；不做也不出错）。"""
    with _LOCK:
        st = _load_state()
        if agent_id in st:
            del st[agent_id]
            _save_state(st)


def scan_agent_outbox(agent: dict) -> list[dict]:
    """增量读 outbox 新字节，返回新解析出的信封列表；推进持久化偏移。

    规则：
    - 首见 agent → offset = 当前文件大小（跳过历史——升级前旧信不触发，
      即「互联需重新发布」）；
    - size < offset（文件被截断/轮换）→ 归 0 全量重扫（信封幂等，自愈）；
    - 尾部半行（torn write）留下轮；坏行/非信封行消费掉跳过。
    """
    aid = agent["id"]
    p = agentops._home(agent) / "data" / "outbox.jsonl"
    try:
        size = p.stat().st_size
    except FileNotFoundError:
        # 文件尚不存在（新 agent 还没写过信）：记录 0 偏移——文件一出现即从头
        # 扫起。不能等文件出现再做首见（那会把 agent 的第一封信当"历史"跳过，
        # 首封 publish 就永远登记不上）。
        with _LOCK:
            st = _load_state()
            if aid not in st:
                st[aid] = {"offset": 0, "size": 0}
                _save_state(st)
        return []
    with _LOCK:
        st = _load_state()
        rec = st.get(aid)
        if not isinstance(rec, dict):
            st[aid] = {"offset": size, "size": size}
            _save_state(st)
            return []
        offset = int(rec.get("offset", 0))
        if size < offset:
            offset = 0
        if offset >= size:
            return []
        # 按字节读与计数（seek 是字节语义；文本行里中文多字节字符会让
        # 字符数 ≠ 字节数，混用会把偏移漂进多字节序列中段）。
        with p.open("rb") as f:
            f.seek(offset)
            chunk = f.read()
        consumed = offset
        out: list[dict] = []
        for raw in chunk.split(b"\n")[:-1]:
            # 不以 \n 结尾的最后一截是未写完的半行（torn write），留给下轮；
            # 以 \n 结尾时 split 的尾元素恒为空串，天然不进循环
            consumed += len(raw) + 1
            try:
                obj = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                continue           # 坏行/脏字节：消费掉跳过
            if not isinstance(obj, dict):
                continue
            env = parse_envelope(str(obj.get("text") or ""))
            if env is not None:
                out.append(env)
        st[aid] = {"offset": consumed, "size": size}
        _save_state(st)
        return out


# ── 信封处理 ─────────────────────────────────────────────────────────

def handle_envelope(agent: dict, env: dict) -> str | None:
    """处理一枚信封。需要回信时返回回信文字（directory JSON 串）；否则 None。"""
    aid = agent["id"]
    kind = env.get("xusi")

    if kind == "publish":
        try:
            port = int(env.get("port"))
        except (TypeError, ValueError):
            agentops.audit("mailroom.bad_publish", agent=aid, reason="port 非整数")
            return None
        token = str(env.get("token") or "").strip()
        if not token:
            agentops.audit("mailroom.bad_publish", agent=aid, reason="token 为空")
            return None
        if not 1 <= port <= 65535:
            agentops.audit("mailroom.bad_publish", agent=aid, reason=f"port {port} 越界")
            return None
        host = str(env.get("host") or "").strip() or "127.0.0.1"
        cfg = get_config()
        if cfg.port_lo <= port <= cfg.port_hi:
            # 互联端口落在管理面分配池内只警告不拒绝（agent 自治；分配池
            # 建议避开 8700-8799 写进回信与 docs）
            agentops.audit("mailroom.publish", agent=aid, port=port, host=host,
                           warn=f"port 在管理面分配池 {cfg.port_lo}-{cfg.port_hi} 内")
        registry.update_agent(aid, {"interconnect": {
            "token": token, "port": port, "host": host,
            "published_at": registry.now_iso()}})
        agentops.audit("mailroom.publish", agent=aid, port=port, host=host)
        return None

    if kind == "request_directory":
        entries: list[dict] = []
        for a in registry.list_agents():
            if a["id"] == aid:
                continue
            ic = a.get("interconnect")
            if not isinstance(ic, dict) or not ic.get("token"):
                continue
            entries.append({
                "id": a["id"],
                "name": (a.get("name") or a["id"]).strip() or a["id"],
                "host": ic.get("host") or "127.0.0.1",
                "port": ic.get("port"),
                "token": ic.get("token"),
                "published_at": ic.get("published_at", ""),
            })
        agentops.audit("mailroom.directory", agent=aid, peers=len(entries))
        return json.dumps(directory_envelope(entries), ensure_ascii=False)

    if kind == "directory":
        agentops.audit("mailroom.directory_echo", agent=aid)
        return None

    agentops.audit("mailroom.unknown", agent=aid, kind=str(kind))
    return None


# ── 扫描线程 ─────────────────────────────────────────────────────────

def run_forever(stop: threading.Event) -> None:
    """主循环：每 5s 逐 agent 增量扫 outbox，识别信封 → 登记/回信。

    daemon 线程；外层 try/except 防线程死亡；每 agent 独立隔离，坏文件不拖垮
    整轮；同类错误审计去重（防 5s 周期刷爆 audit.jsonl）。"""
    while not stop.wait(_SCAN_INTERVAL_S):
        for agent in registry.list_agents():
            try:
                for env in scan_agent_outbox(agent):
                    reply = handle_envelope(agent, env)
                    if reply:
                        agentops.mail(agent["id"], reply)
            except Exception as e:
                aid = agent["id"]
                if _last_err.get(aid) != repr(e):
                    _last_err[aid] = repr(e)
                    agentops.audit("mailroom.error", agent=aid, error=str(e))


def state_snapshot() -> dict[str, Any]:
    """doctor / 调试用：各 agent 的扫描偏移概况。"""
    return dict(_load_state())
