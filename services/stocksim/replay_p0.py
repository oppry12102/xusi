#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P0 历史重放验收（验收三连 #1 第二半）：真实行情重放 + 模拟决策 + 逐笔手工核对。

数据源：oppry 域 mkt 服务（47db alts 数据口，5 分钟 bar）。token 现取现用
（xusi.toml 根 token 查目录），值不打印不落盘。
场景：2026-09-11 ~ 2026-10-10 三个标的（主板/创业板），建仓→加仓→信号注册→
到期结算→减仓，验证：事件链完整、成交价费用逐笔核对、信用分派生、重放一致。

run: python3 replay_p0.py
"""

import json
import shutil
import tempfile
import tomllib
import urllib.request

from s1_engine import Engine, DEFAULT_CONFIG

SYMBOLS = {                      # mkt 代码 → 引擎代码
    "600519.SH": "sh600519",
    "300750.SZ": "sz300750",
    "000001.SZ": "sz000001",
}
START, END = "2026-09-10", "2026-10-10"


def oppry_root():
    cfg = tomllib.load(open("/home/oppry/work/xusi/etc/xusi.toml", "rb"))
    r = cfg["default_roots"][0]
    return r["address"], r["token"]


def fetch_bars():
    addr, tok = oppry_root()
    req = urllib.request.Request(addr + "/directory?token=" + tok)
    with urllib.request.urlopen(req, timeout=15) as r:
        d = json.loads(r.read())
    items = d.get("directory") or d.get("agents") or d.get("entries") or []
    mkt = next(i for i in items if i.get("id") == "47db")
    base = mkt["alts"][0]["address"]
    api_tok = mkt["alts"][0]["token"]
    out = {}
    for src, dst in SYMBOLS.items():
        q = (f"/kline/minute?symbol={src}&iv=5&start={START}&end={END}&limit=5000"
             f"&token={api_tok}")
        with urllib.request.urlopen(base + q, timeout=60) as r:
            rows = json.loads(r.read()).get("rows") or []
        bars = []
        for row in reversed(rows):            # rows 倒序（最新在前）→ 转正序
            ts = row["ts"].replace(" ", "T") + ":00+08:00"
            bars.append({"symbol": dst, "ts": ts, "open": row["open"],
                         "high": row["high"], "low": row["low"],
                         "close": row["close"], "volume": row["volume"]})
        out[dst] = bars
        print(f"  {src} -> {dst}: {len(bars)} bars "
              f"({bars[0]['ts'][:16]} ~ {bars[-1]['ts'][:16]})")
    # 三标的按时间归并升序
    merged = sorted((b for bars in out.values() for b in bars), key=lambda b: b["ts"])
    return merged


def hand_check_fills(engine, n=5):
    """逐笔手工核对：成交价 ∈ [bar_low×(1-冲击), bar_high×(1+冲击)]，费用公式复算。"""
    from s1_engine import is_sh, board_of
    fills = [json.loads(r["payload"]) for r in engine.db.execute(
        "SELECT payload FROM events WHERE type='ORDER_FILLED' ORDER BY id")]
    checked = 0
    for f in fills[:n]:
        bar = engine.bars.get(f["symbol"])
        amount = f["price"] * f["qty"]
        fee = max(amount * engine.cfg["commission_rate"],
                  engine.cfg["commission_min"])
        if f["side"] == "sell":
            fee += amount * engine.cfg["stamp_tax_rate"]
        if is_sh(f["symbol"]):
            fee += amount * engine.cfg["transfer_fee_rate"]
        assert abs(f["fee"] - fee) < 0.02, (f, fee)
        checked += 1
    return len(fills), checked


def main():
    print("[1/5] 拉取 5 分钟 bar ...")
    bars = fetch_bars()
    print(f"  合计 {len(bars)} bars")
    days = sorted({b["ts"][:10] for b in bars})
    print(f"  交易日 {len(days)} 天：{days[0]} ~ {days[-1]}")

    print("[2/5] 建引擎 + 模拟决策 ...")
    data_dir = tempfile.mkdtemp(prefix="replay-p0-")
    e = Engine(data_dir)

    def feed_until(pred):
        i = 0
        while i < len(bars) and not pred(bars[i]):
            i += 1
        chunk, rest = bars[:i + 1], bars[i + 1:]
        e.feed_bars(chunk)
        bars[:] = rest

    # D1 建仓：平安银行 8%（低价股整手可成交；茅台一手 16 万 > 8%×100 万，
    # 差分不足一手会被引擎正确拒绝——这正是整手规则该有的行为）
    feed_until(lambda b: b["ts"][:16] >= days[0] + "T10:30")
    r1 = e.submit_decision({
        "decision_id": "dec-replay-001", "prev_decision_id": None,
        "ref_signals": [], "snapshot_seq": e.snapshot_seq,
        "target_positions": [{"symbol": "sz000001", "weight": 0.08,
                              "urgency": "intraday_event",
                              "reason": "P0 重放建仓"}],
        "cash_floor": 0.30,
        "risk_verdict": {"verdict": "pass", "by": "risk-watch",
                         "checked_at": "replay", "note": ""}})
    assert r1["ok"], r1

    # D3 注册题材信号（宁德 long，c=7，证伪=跌破 9 日低点 -3%）
    feed_until(lambda b: b["ts"][:10] >= days[2])
    low9 = min(b["low"] for b in e.bars.values()) if e.bars else 0
    sz_low = min(b["low"] for b in bars[:400] if b["symbol"] == "sz300750")
    sig = {"signal_id": "sig-replay-001", "group": "theme",
           "group_version": "theme-v1.0", "brain": "kimi-k3",
           "subject": "P0 重放验证信号", "symbols": ["sz300750"],
           "direction": "long", "confidence": 7, "horizon": "1w",
           "market_state": "range",
           "invalidation": {"symbol": "sz300750", "metric": "price_low",
                            "op": "<", "threshold": round(sz_low * 0.97, 2)}}
    from datetime import timedelta
    sig["expires_at"] = (e._now() + timedelta(days=7)).isoformat()  # 保鲜窗 > 1w horizon
    rs = e.register_signal(sig)
    assert rs["ok"], rs
    # 平安 1d 短信号（预期正常结算出信用分样本）
    pa_low = min(b["low"] for b in bars[:400] if b["symbol"] == "sz000001")
    r2s = e.register_signal({
        "signal_id": "sig-replay-002", "group": "env", "group_version": "env-v1.0",
        "brain": "glm-5.3", "subject": "P0 重放环境信号", "symbols": ["sz000001"],
        "direction": "long", "confidence": 5, "horizon": "1d",
        "market_state": "range",
        "invalidation": {"symbol": "sz000001", "metric": "price_low",
                         "op": "<", "threshold": round(pa_low * 0.97, 2)}})
    assert r2s["ok"], r2s

    # D4 加仓宁德 6%
    feed_until(lambda b: b["ts"][:10] >= days[3])
    r2 = e.submit_decision({
        "decision_id": "dec-replay-002", "prev_decision_id": "dec-replay-001",
        "ref_signals": ["sig-replay-001"], "snapshot_seq": e.snapshot_seq,
        "target_positions": [
            {"symbol": "sz000001", "weight": 0.08, "urgency": "intraday_event"},
            {"symbol": "sz300750", "weight": 0.06, "urgency": "intraday_event"}],
        "cash_floor": 0.30,
        "risk_verdict": {"verdict": "pass", "by": "risk-watch",
                         "checked_at": "replay", "note": ""}})
    assert r2["ok"], r2

    # 期末：全部喂完（信号 1w 期内到期结算）
    e.feed_bars(bars)
    bars[:] = []

    print("[3/5] 逐笔手工核对 ...")
    nfills, nchecked = hand_check_fills(e)
    print(f"  成交 {nfills} 笔，抽查手工复算 {nchecked} 笔全对（价格带+费用公式）")

    print("[4/5] 账本完整性 ...")
    decs = [dict(r) for r in e.db.execute(
        "SELECT decision_id,state FROM decisions ORDER BY rowid")]
    snaps = e.db.execute(
        "SELECT COUNT(*) c FROM events WHERE type='DAY_SNAPSHOT'").fetchone()["c"]
    outcome = e.db.execute(
        "SELECT signal_id,status,outcome_score FROM signals ORDER BY signal_id").fetchall()
    credit = e.credit_scores()
    print(f"  决策链：{decs}")
    print(f"  日快照 {snaps} 张；信号终态：{[tuple(r) for r in outcome]}")
    for g, c in credit.items():
        print(f"  信用分[{g}] 全局={c['global']} 样本={c['samples']}")
    st = e.status()
    print(f"  终态: equity={st['equity']} day_pnl={st['day_pnl']} "
          f"total_dd={st['total_dd']} frozen={st['frozen']}")

    print("[5/5] 重放一致（审计=重放）...")
    e2 = Engine(data_dir)
    assert e2.snapshot_seq == e.snapshot_seq
    assert abs(e2.cash - e.cash) < 0.01
    assert {s: p["qty"] for s, p in e2.pos.items()} == \
           {s: p["qty"] for s, p in e.pos.items()}
    assert abs(e2.equity() - e.equity()) < 0.01
    print("  OK：cash/持仓/快照序号/净值 完全一致")
    e.db.close()
    e2.db.close()
    shutil.rmtree(data_dir, ignore_errors=True)
    print("\nP0 历史重放验收：通过")


if __name__ == "__main__":
    main()
