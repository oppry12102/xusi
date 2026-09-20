"""机器事实账（data/facts.db）的管理面访问——与内核 xuseek/facts.py 同一条账本纪律。

内核 v2.7.79 起，事实账（SQLite WAL）是机器事实与全部异步消息的**唯一落点**：
mail（管理员投信）/ outbox（大脑外发）/ session_end（会话索引）都是账本行——
mailbox.jsonl / outbox.jsonl / sessions.jsonl 全部退役。管理面只做两件事：

- 投信：append 一条 mail 事实（与内核 `xuseek mail` / 观测台投信表单同语义，
  sender=admin；daemon 休眠中轮询位点，数秒内被唤醒）；
- 只读查询：outbox / mail / session_end 的尾部 N 行（详情页信箱与呼吸列表用）。

与内核同纪律：
- WAL + busy_timeout：多进程并发写安全（管理面投信与内核收信/外发互不阻塞）；
- 每次调用开库、用完即关：无进程级连接缓存，运行中删/换 facts.db 不分裂账本；
- autocommit（isolation_level=None）：写完即放锁；
- 表结构 = 内核 facts 表的同款 schema（n/at/type/body + idx_facts_type，
  CREATE TABLE IF NOT EXISTS 幂等——空 home 上先投信、内核首启照常用）；
- 坏行单点兜底：body 解不开返回 raw 事实（可见、可跳），不击穿读路径。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def _iso() -> str:
    """UTC ISO8601 秒级（与内核 clock.iso 同格式：2026-08-04T02:05:15Z）。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _path(home: Path) -> Path:
    return home / "data" / "facts.db"


def _conn(home: Path) -> sqlite3.Connection:
    """每次调用开库（用完由调用方 close）。表结构与内核 facts.py 逐字节同款。"""
    p = _path(home)
    p.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(p), timeout=5, isolation_level=None)
    try:
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA busy_timeout=5000")
        c.execute(
            "CREATE TABLE IF NOT EXISTS facts("
            "n INTEGER PRIMARY KEY AUTOINCREMENT,"
            "at TEXT NOT NULL, type TEXT NOT NULL, body TEXT NOT NULL)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_facts_type ON facts(type)")
        return c
    except Exception:
        c.close()
        raise


def _row(n: int, type: str, body: str) -> dict:
    """单行兜底：body 解析失败返回 raw 事实（与内核 facts._row 同口径）。"""
    try:
        fact = json.loads(body)
    except (ValueError, TypeError):
        return {"n": n, "type": type, "raw": body, "unparseable": True}
    fact["n"] = n
    return fact


def append(home: Path, type: str, **fields) -> dict:
    """追加一条事实（管理面目前只投 mail；永不删）。返回带行号 n 的事实。"""
    fact = {"type": type, "at": _iso(), **fields}
    conn = _conn(home)
    try:
        cur = conn.execute(
            "INSERT INTO facts(at, type, body) VALUES (?,?,?)",
            (fact["at"], type, json.dumps(fact, ensure_ascii=False)))
        fact["n"] = cur.lastrowid
        return fact
    finally:
        conn.close()


def tail(home: Path, type: str | None = None, limit: int | None = None) -> list[dict]:
    """指定类型（None = 全部）最近的 limit 条，按行号升序返回（时间序）。

    「最近 N 条」语义（信箱/会话列表调用方）；LIMIT 在 SQL 层做，不整表解 JSON。
    facts.db 不存在/无行 → []（年轻 agent 首口呼吸前没有账本行）。"""
    q = "SELECT n, type, body FROM facts"
    args: list = []
    if type is not None:
        q += " WHERE type = ?"
        args.append(type)
    conn = _conn(home)
    try:
        if limit:
            rows = conn.execute(q + " ORDER BY n DESC LIMIT ?",
                                args + [int(limit)]).fetchall()
            rows.reverse()
        else:
            rows = conn.execute(q + " ORDER BY n ASC", args).fetchall()
    finally:
        conn.close()
    return [_row(r, t, b) for r, t, b in rows]
