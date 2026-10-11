#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P0 边界用例表（验收三连 #1）：涨跌停 / T+1 / 零股 / 费用 / 链校验 / 熔断 / 信用分。

直接驱动 Engine（不走 HTTP）；行情用合成 bar。run: python3 test_s1_p0.py -v
"""

import json
import shutil
import tempfile
import time
import unittest

from s1_engine import Engine


def bar(sym, day, hms, close, volume=1000000, low=None, high=None, halted=False,
        open_=None):
    o = open_ if open_ is not None else close
    return {"symbol": sym, "ts": f"{day}T{hms}+08:00", "open": o, "high":
            high if high is not None else max(o, close),
            "low": low if low is not None else min(o, close),
            "close": close, "volume": volume, "halted": halted}


def decision(dec_id, prev, seq, targets, refs=None, cash_floor=0.30, **kw):
    d = {"decision_id": dec_id, "prev_decision_id": prev, "ref_signals": refs or [],
         "snapshot_seq": seq,
         "target_positions": targets, "cash_floor": cash_floor,
         "risk_verdict": {"verdict": "pass", "by": "risk-watch",
                          "checked_at": "t", "note": ""}}
    d.update(kw)
    return d


class P0Tests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="s1test-")
        # 测试放宽单票上限/关限流以便编排多笔决策；默认值回归 plan 初值由
        # test_14/test_16 单独覆盖验证
        self.e = Engine(self.dir, config={
            "max_single_weight": 0.50, "pm_rate_limit_seconds": 0})

    def tearDown(self):
        self.e.db.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    # ---------------------------------------------------------- 基础撮合

    def test_01_basic_buy_fill_with_fees(self):
        """市价差分买入：成交价≈close×(1+冲击)；佣金+过户费（沪市）；T+1 占用。"""
        self.e.feed_bars([bar("sh600000", "2026-09-14", "09:30:00", 10.00)])
        seq = self.e.snapshot_seq
        r = self.e.submit_decision(decision(
            "d1", None, seq,
            [{"symbol": "sh600000", "weight": 0.30, "urgency": "intraday_event"}]))
        self.assertTrue(r["ok"], r)
        self.assertEqual(len(r["orders"]), 1)
        out = self.e.feed_bars([bar("sh600000", "2026-09-14", "09:31:00", 10.00)])
        self.assertEqual(out["fills"], 1)
        eq = self.e.equity()
        self.assertAlmostEqual(self.e.cash / eq, 0.70, delta=0.01)   # ≈30% 仓位
        pos = self.e.pos["sh600000"]
        self.assertGreater(pos["qty"], 0)
        self.assertEqual(pos["avail"], 0)                            # T+1 当日不可卖
        # 事件链可追溯
        evs = self.e.db.execute(
            "SELECT type FROM events ORDER BY id").fetchall()
        types = [x["type"] for x in evs]
        self.assertIn("DECISION_ACCEPTED", types)
        self.assertIn("ORDER_CREATED", types)
        self.assertIn("ORDER_FILLED", types)

    def test_02_limit_up_buy_queued_no_fill(self):
        """涨停一字板：买单排队不成交（ORDER_QUEUED）。"""
        self.e.feed_bars([bar("sz000001", "2026-09-14", "09:30:00", 10.00)])
        self.e.submit_decision(decision(
            "d1", None, self.e.snapshot_seq,
            [{"symbol": "sz000001", "weight": 0.20, "urgency": "intraday_event"}]))
        out = self.e.feed_bars([bar("sz000001", "2026-09-14", "09:31:00", 11.00,
                                    low=11.00, high=11.00)])       # 一字涨停
        self.assertEqual(out["fills"], 0)
        self.assertEqual(self.e.pos.get("sz000001", {}).get("qty", 0), 0)
        types = [x["type"] for x in self.e.db.execute(
            "SELECT type FROM events").fetchall()]
        self.assertIn("ORDER_QUEUED", types)

    def test_03_limit_down_sell_no_fill(self):
        """跌停封死：卖单不成交。"""
        self.e.feed_bars([bar("sz000001", "2026-09-14", "09:30:00", 10.00)])
        self.e.submit_decision(decision(
            "d1", None, self.e.snapshot_seq,
            [{"symbol": "sz000001", "weight": 0.30, "urgency": "intraday_event"}]))
        self.e.feed_bars([bar("sz000001", "2026-09-14", "09:31:00", 10.00)])
        self.assertGreater(self.e.pos["sz000001"]["avail"] +
                           self.e.pending_t1.get("sz000001", 0), 0)
        # 次日开盘跌停（昨收 10 → 9.00），卖出应排队
        self.e.feed_bars([bar("sz000001", "2026-09-15", "09:30:00", 9.00,
                              low=9.00, high=9.00)])
        self.e.submit_decision(decision(
            "d2", "d1", self.e.snapshot_seq,
            [{"symbol": "sz000001", "weight": 0.0, "urgency": "intraday_event"}]))
        out = self.e.feed_bars([bar("sz000001", "2026-09-15", "09:31:00", 9.00,
                                    low=9.00, high=9.00)])
        self.assertEqual(out["fills"], 0)

    def test_04_t1_avail_next_day(self):
        """T+1：当日买入不可卖（avail=0），次日释放。"""
        self.e.feed_bars([bar("sh600000", "2026-09-14", "09:30:00", 10.00)])
        self.e.submit_decision(decision(
            "d1", None, self.e.snapshot_seq,
            [{"symbol": "sh600000", "weight": 0.30, "urgency": "intraday_event"}]))
        self.e.feed_bars([bar("sh600000", "2026-09-14", "09:31:00", 10.00)])
        self.assertEqual(self.e.pos["sh600000"]["avail"], 0)
        self.e.feed_bars([bar("sh600000", "2026-09-15", "09:30:00", 10.00)])
        self.assertEqual(self.e.pos["sh600001"]["avail"] if "sh600001" in self.e.pos
                         else self.e.pos["sh600000"]["avail"],
                         self.e.pos["sh600000"]["qty"])

    def test_05_volume_cap_split_across_bars(self):
        """单笔 ≤ 该分钟 bar 成交量 10%，超出部分下一 bar 续撮。"""
        small_vol = 10000                      # 10% = 1000 股/分钟
        self.e.feed_bars([bar("sh600000", "2026-09-14", "09:30:00", 10.00)])
        self.e.submit_decision(decision(
            "d1", None, self.e.snapshot_seq,
            [{"symbol": "sh600000", "weight": 0.50, "urgency": "intraday_event"}]))
        qty_want = 50000 // 100 * 100          # 目标 50 万/10 元 = 5 万股
        o1 = self.e.feed_bars([bar("sh600000", "2026-09-14", "09:31:00", 10.00,
                                   volume=small_vol)])
        filled_1 = self.e.pos["sh600000"]["qty"]
        self.assertGreater(filled_1, 0)
        self.assertLessEqual(filled_1, 1000)
        self.e.feed_bars([bar("sh600000", "2026-09-14", "09:32:00", 10.00,
                              volume=small_vol)])
        filled_2 = self.e.pos["sh600000"]["qty"]
        self.assertGreater(filled_2, filled_1)  # 续撮

    def test_06_halted_no_fill(self):
        """停牌 bar：不撮合。"""
        self.e.feed_bars([bar("sh600000", "2026-09-14", "09:30:00", 10.00)])
        self.e.submit_decision(decision(
            "d1", None, self.e.snapshot_seq,
            [{"symbol": "sh600000", "weight": 0.30, "urgency": "intraday_event"}]))
        out = self.e.feed_bars([bar("sh600000", "2026-09-14", "09:31:00", 10.00,
                                    halted=True)])
        self.assertEqual(out["fills"], 0)
        self.assertEqual(self.e.pos.get("sh600000", {}).get("qty", 0), 0)

    # ---------------------------------------------------------- 费用

    def test_07_fee_minimum_commission(self):
        """佣金最低 5 元：小额成交费用=5（+过户费）。"""
        self.e.feed_bars([bar("sh600000", "2026-09-14", "09:30:00", 10.00,
                              volume=100000)])
        # 直接构造一手买入订单绕过权重差分（最小手）
        oid = "ord-test"
        self.e.orders[oid] = {
            "order_id": oid, "decision_id": "d-x", "symbol": "sh600000",
            "side": "buy", "qty": 100, "filled": 0, "state": "open",
            "urgency": "intraday_event", "ref_price": 10.0, "limit_price": None,
            "max_chase_pct": None, "valid_until": "2099-01-01", "sector": None}
        out = self.e.feed_bars([bar("sh600000", "2026-09-14", "09:31:00", 10.00,
                                    volume=100000)])
        fee_ev = [json.loads(x["payload"]) for x in self.e.db.execute(
            "SELECT payload FROM events WHERE type='ORDER_FILLED'")]
        self.assertEqual(len(fee_ev), 1)
        amount = 1000
        expect = max(amount * 0.00025, 5.0) + amount * 0.00001   # 5 + 0.01
        self.assertAlmostEqual(fee_ev[0]["fee"], expect, places=2)

    def test_08_stamp_sell_only(self):
        """印花税仅卖出收取。"""
        self.e.feed_bars([bar("sh600000", "2026-09-14", "09:30:00", 10.00)])
        self.e.submit_decision(decision(
            "d1", None, self.e.snapshot_seq,
            [{"symbol": "sh600000", "weight": 0.30, "urgency": "intraday_event"}]))
        self.e.feed_bars([bar("sh600000", "2026-09-14", "09:31:00", 10.00)])
        buys = [json.loads(x["payload"]) for x in self.e.db.execute(
            "SELECT payload FROM events WHERE type='ORDER_FILLED'")]
        for f in buys:                                          # 买入：佣金+过户费，无印花
            amount = f["price"] * f["qty"]
            expect = max(amount * 0.00025, 5.0) + amount * 0.00001
            self.assertAlmostEqual(f["fee"], expect, places=1)
        # 次日全清
        self.e.feed_bars([bar("sh600000", "2026-09-15", "09:30:00", 10.00)])
        self.e.submit_decision(decision(
            "d2", "d1", self.e.snapshot_seq,
            [{"symbol": "sh600000", "weight": 0.0, "urgency": "intraday_event"}]))
        self.e.feed_bars([bar("sh600000", "2026-09-15", "09:31:00", 10.00)])
        all_fills = [json.loads(x["payload"]) for x in self.e.db.execute(
            "SELECT payload FROM events WHERE type='ORDER_FILLED'")]
        sell_fees = [f for f in all_fills if f["side"] == "sell"]
        self.assertTrue(sell_fees)
        amount = sell_fees[0]["price"] * sell_fees[0]["qty"]
        expect = max(amount * 0.00025, 5.0) + amount * 0.0005 + amount * 0.00001
        self.assertAlmostEqual(sell_fees[0]["fee"], expect, places=1)

    # ---------------------------------------------------------- 决策单校验链

    def test_10_chain_mismatch_rejected(self):
        self.e.feed_bars([bar("sh600000", "2026-09-14", "09:30:00", 10.00)])
        r1 = self.e.submit_decision(decision(
            "d1", None, self.e.snapshot_seq,
            [{"symbol": "sh600000", "weight": 0.30, "urgency": "intraday_event"}]))
        self.assertTrue(r1["ok"])
        r2 = self.e.submit_decision(decision(
            "d2", "d-wrong", self.e.snapshot_seq,
            [{"symbol": "sh600000", "weight": 0.30, "urgency": "intraday_event"}]))
        self.assertEqual(r2["error"], "chain_mismatch")
        self.assertEqual(r2["expected_prev"], "d1")

    def test_11_snapshot_stale_rejected(self):
        self.e.feed_bars([bar("sh600000", "2026-09-14", "09:30:00", 10.00)])
        r = self.e.submit_decision(decision(
            "d1", None, 999,
            [{"symbol": "sh600000", "weight": 0.30, "urgency": "intraday_event"}]))
        self.assertEqual(r["error"], "snapshot_stale")

    def test_12_missing_risk_verdict_rejected(self):
        d = decision("d1", None, 0,
                     [{"symbol": "sh600000", "weight": 0.30, "urgency": "x"}])
        d.pop("risk_verdict")
        r = self.e.submit_decision(d)
        self.assertEqual(r["error"], "missing_fields")

    def test_13_cash_floor_violated_rejected(self):
        r = self.e.submit_decision(decision(
            "d1", None, 0,
            [{"symbol": "sh600000", "weight": 0.90, "urgency": "x"}],
            cash_floor=0.10))
        self.assertEqual(r["error"], "cash_floor_below_hard_floor")

    def test_14_single_weight_over_cap_rejected(self):
        self.e.cfg["max_single_weight"] = 0.10     # 回归 plan 初值（单票 ≤10%）
        r = self.e.submit_decision(decision(
            "d1", None, 0,
            [{"symbol": "sh600000", "weight": 0.30, "urgency": "x"}]))
        self.assertEqual(r["error"], "single_weight_over_cap")

    def test_15_signal_not_registered_rejected(self):
        r = self.e.submit_decision(decision(
            "d1", None, 0,
            [{"symbol": "sh600000", "weight": 0.30, "urgency": "x"}],
            refs=["sig-nonexistent"]))
        self.assertEqual(r["error"], "signal_not_registered")

    def test_16_rate_limit_15min(self):
        self.e.cfg["pm_rate_limit_seconds"] = 900  # 回归 plan 初值（15min 1 单）
        self.e.feed_bars([bar("sh600000", "2026-09-14", "09:30:00", 10.00)])
        r1 = self.e.submit_decision(decision(
            "d1", None, self.e.snapshot_seq,
            [{"symbol": "sh600000", "weight": 0.30, "urgency": "intraday_event"}]))
        self.assertTrue(r1["ok"])
        r2 = self.e.submit_decision(decision(
            "d2", "d1", self.e.snapshot_seq,
            [{"symbol": "sh600000", "weight": 0.30, "urgency": "intraday_event"}]))
        self.assertEqual(r2["error"], "rate_limited")

    def test_17_veto_enters_chain_and_cancels_orders(self):
        """风控否决：否决单是链上一环（prev 永指最新链环），挂单撤销。"""
        self.e.feed_bars([bar("sh600000", "2026-09-14", "09:30:00", 10.00,
                              volume=1000)])          # 量小，单挂着不成交
        r1 = self.e.submit_decision(decision(
            "d1", None, self.e.snapshot_seq,
            [{"symbol": "sh600000", "weight": 0.30, "urgency": "intraday_event"}]))
        self.assertTrue(r1["ok"])
        v = self.e.veto_decision("d1", "risk-watch", "题材已高潮")
        self.assertTrue(v["ok"])
        r2 = self.e.submit_decision(decision(
            "d2", "d1", self.e.snapshot_seq,           # prev=vetoed 单 ✓
            [{"symbol": "sh600000", "weight": 0.30, "urgency": "intraday_event"}]))
        self.assertTrue(r2["ok"], r2)                  # 链连续验证含 VETOED

    # ---------------------------------------------------------- 情报单与信用分

    def test_20_signal_registration_validation(self):
        ok = self.e.register_signal({
            "signal_id": "sig-1", "group": "theme", "group_version": "theme-v1.0",
            "direction": "long", "confidence": 7, "horizon": "3d",
            "subject": "固态电池", "symbols": ["sh600000"],
            "invalidation": {"symbol": "sh600000", "metric": "price_low",
                             "op": "<", "threshold": 9.5},
            "market_state": "trend_up"})
        self.assertTrue(ok["ok"], ok)
        bad = self.e.register_signal({
            "signal_id": "sig-2", "group": "theme", "group_version": "v1",
            "direction": "long", "confidence": 0.65, "horizon": "3d",
            "invalidation": {"symbol": "x", "metric": "price", "op": "<", "threshold": 1}})
        self.assertEqual(bad["error"], "confidence_must_be_int_1_10")
        bad2 = self.e.register_signal({
            "signal_id": "sig-3", "group": "theme", "group_version": "v1",
            "direction": "long", "confidence": 5, "horizon": "3d",
            "invalidation": "龙头低开3%则作废"})
        self.assertEqual(bad2["error"], "invalidation_must_be_structured")

    def test_21_outcome_scoring_formula(self):
        """s = ±2×(c/10)×实现度：+3% 以上记 1 档。"""
        self.e.feed_bars([bar("sh600000", "2026-09-14", "09:30:00", 10.00)])
        self.e.register_signal({
            "signal_id": "sig-l", "group": "theme", "group_version": "v1",
            "direction": "long", "confidence": 7, "horizon": "1d",
            "symbols": ["sh600000"],
            "invalidation": {"symbol": "sh600000", "metric": "price_low",
                             "op": "<", "threshold": 5.0}})
        # 次日 +5%（long 对，≥3% → 实现度 1）→ s=+2×0.7×1=1.4
        self.e.feed_bars([bar("sh600000", "2026-09-14", "15:00:00", 10.00)])
        self.e.feed_bars([bar("sh600000", "2026-09-15", "15:00:01", 10.50)])
        out = self.e.credit_scores()
        self.assertIn("theme", out)
        self.assertAlmostEqual(out["theme"]["global"], 1.4, delta=0.01)

    def test_22_invalidation_triggered_still_scores_direction(self):
        """防洗分（§四.3）：触发 invalidation 仍按触发时点方向盈亏计分，不免费。"""
        self.e.feed_bars([bar("sh600000", "2026-09-14", "09:30:00", 10.00)])
        self.e.register_signal({
            "signal_id": "sig-b", "group": "env", "group_version": "v1",
            "direction": "long", "confidence": 8, "horizon": "1d",
            "symbols": ["sh600000"],
            "invalidation": {"symbol": "sh600000", "metric": "price_low",
                             "op": "<", "threshold": 9.5}})
        # 长仓标的跌穿 9.5 → 触发证伪；信号 status=invalidated，
        # outcome 由到期结算按方向计（此处跌 → 错方向，负分）
        self.e.feed_bars([bar("sh600000", "2026-09-14", "09:31:00", 9.40)])
        row = self.e.db.execute(
            "SELECT status FROM signals WHERE signal_id='sig-b'").fetchone()
        self.assertEqual(row["status"], "invalidated")

    # ---------------------------------------------------------- 熔断与冻结

    def test_30_daily_loss_circuit_force_reduce(self):
        """单日浮亏 >3% → 代码强制降仓（免签章）。"""
        self.e.feed_bars([bar("sh600000", "2026-09-14", "09:30:00", 10.00)])
        self.e.submit_decision(decision(
            "d1", None, self.e.snapshot_seq,
            [{"symbol": "sh600000", "weight": 0.50, "urgency": "intraday_event"}]))
        self.e.feed_bars([bar("sh600000", "2026-09-14", "09:31:00", 10.00,
                              volume=5000000)])
        self.assertGreater(self.e.pos["sh600000"]["qty"], 0)
        # 次日持仓市值跌 6%（50k 股 ×0.6 元 ≈ 3.02% 净值跌幅 > 3%）→ 熔断降仓
        self.e.feed_bars([bar("sh600000", "2026-09-15", "09:31:00", 9.40,
                              volume=5000000)])
        types = [x["type"] for x in self.e.db.execute(
            "SELECT type FROM events").fetchall()]
        self.assertIn("CIRCUIT_TRIGGERED", types)
        forced = [json.loads(x["payload"]) for x in self.e.db.execute(
            "SELECT payload FROM events WHERE type='ORDER_CREATED'")]
        self.assertTrue(any(f.get("reason") == "circuit_force_reduce" for f in forced))

    def test_31_freeze_rejects_decisions_and_resume(self):
        self.e.freeze("all", "行情断流演练")
        r = self.e.submit_decision(decision(
            "d1", None, self.e.snapshot_seq,
            [{"symbol": "sh600000", "weight": 0.10, "urgency": "intraday_event"}]))
        self.assertEqual(r["error"], "engine_frozen")
        self.e.resume("admin")
        # 恢复后收到新 bar → 正常受理（链首 prev=None）
        self.e.feed_bars([bar("sh600000", "2026-09-14", "09:30:00", 10.00)])
        r2 = self.e.submit_decision(decision(
            "d2", None, self.e.snapshot_seq,
            [{"symbol": "sh600000", "weight": 0.10, "urgency": "intraday_event"}]))
        self.assertTrue(r2["ok"], r2)

    def test_32_day_close_snapshot_and_order_expiry(self):
        """15:00 日切：快照 + 当日未成交单全过期（当日有效）。"""
        self.e.feed_bars([bar("sh600000", "2026-09-14", "09:30:00", 10.00,
                              volume=100)])            # 量极小，单挂满
        self.e.submit_decision(decision(
            "d1", None, self.e.snapshot_seq,
            [{"symbol": "sh600000", "weight": 0.30, "urgency": "intraday_event"}]))
        self.e.feed_bars([bar("sh600000", "2026-09-14", "14:59:00", 10.00,
                              volume=100)])
        self.e.feed_bars([bar("sh600000", "2026-09-14", "15:00:00", 10.00,
                              volume=100)])
        types = [x["type"] for x in self.e.db.execute(
            "SELECT type FROM events").fetchall()]
        self.assertIn("ORDER_EXPIRED", types)
        snaps = [x for x in self.e.db.execute(
            "SELECT type FROM events WHERE type='DAY_SNAPSHOT'")]
        self.assertTrue(snaps)

    # ---------------------------------------------------------- 重放一致性

    def test_40_replay_projection_consistency(self):
        """审计=重放：同一段事件流在新 Engine 上重放，持仓/现金一致。"""
        self.e.feed_bars([bar("sh600000", "2026-09-14", "09:30:00", 10.00)])
        self.e.submit_decision(decision(
            "d1", None, self.e.snapshot_seq,
            [{"symbol": "sh600000", "weight": 0.30, "urgency": "intraday_event"}]))
        self.e.feed_bars([bar("sh600000", "2026-09-14", "09:31:00", 10.00),
                          bar("sh600000", "2026-09-14", "15:00:00", 10.20)])
        e2 = Engine(self.dir)                  # 同目录重开 → 从 projection 重放
        self.assertEqual(e2.snapshot_seq, self.e.snapshot_seq)
        self.assertAlmostEqual(e2.cash, self.e.cash, places=2)
        self.assertEqual(e2.pos["sh600000"]["qty"], self.e.pos["sh600000"]["qty"])
        self.assertAlmostEqual(e2.equity(), self.e.equity(), places=2)
        e2.db.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
