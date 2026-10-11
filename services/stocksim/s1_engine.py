#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""S1 撮合引擎 + 账本（股票模拟仓舰队地基，纯代码无 LLM）。

设计约束（final-plan v7 §三/§五/§六，铁律不动）：
- 确定性：撮合结果只认行情数据与规则参数，不认任何智能体的「解释」。
- append-only 事件溯源：signal → decision → order → fill → outcome 全链 ID 串联，
  每事件一行；审计 = 重放（持仓/现金由事件重放重建）。
- 权限白名单：决策单只收 PM token；情报单注册只收群组 token；bar 喂入只收
  feed token；freeze/circuit 只收 admin token。token 首启生成入 secrets.json（600）。
- fail-closed：行情断流冻结一切新单（含降仓单）；日亏 >3% 代码强制降仓至
  仓位下限（免签章）；总回撤 >15% 停新开仓；PM 失联由 S2 调 /admin/freeze。

stdlib-only（原则①简单）：ThreadingHTTPServer + SQLite(WAL)。
"""

import json
import os
import re
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---------------------------------------------------------------- 常量与默认参数

CN_TZ = timezone(timedelta(hours=8))          # Asia/Shanghai
DATA_DIR = os.environ.get(
    "STOCKSIM_S1_DATA", os.path.join(os.path.dirname(__file__), "data", "s1"))

DEFAULT_CONFIG = {
    "cash_initial": 1000000.0,
    # 费用（A 股，configurable）
    "commission_rate": 0.00025, "commission_min": 5.0,     # 万2.5，最低5元，双边
    "stamp_tax_rate": 0.0005,                              # 印花税 0.05%，仅卖出
    "transfer_fee_rate": 0.00001,                          # 过户费 0.001%，仅沪市
    # 硬约束（决策单可携 max_single_weight/sector_cap 覆盖，取更严者）
    "max_single_weight": 0.10,
    "sector_cap": 0.25,
    "cash_floor": 0.20,                                    # 现金下限 20% 不可破
    "max_chase_pct_default": 0.02,                         # 引擎换算限价的默认追价上限
    "decision_ttl_minutes": 30,                            # valid_until = received_at + TTL
    "signal_ttl_default_minutes": 240,                     # 情报单未带 expires_at 时的兜底
    "pm_rate_limit_seconds": 900,                          # 每 15min 至多 1 决策单（ACCEPTED 计）
    # 熔断（§五 风控规则初值）
    "daily_loss_breaker": 0.03,                            # 单日浮亏 >3% 强制降仓
    "total_drawdown_breaker": 0.15,                        # 净值自高点回撤 >15% 停新开仓
    "panic_position_floor": 0.30,                          # 降仓目标：仓位下限（按市场状态区间下限）
    # 行情断流判定
    "bar_stale_freeze_seconds": 1800,                      # 盘中 30min 无 bar → 冻结新单
    # 撮合
    "volume_cap_pct": 0.10,                                # 单笔 ≤ 该分钟 bar 成交量 10%
    "lot_size": 100,                                       # 手数（科创板 200 起、1 股递增 → star_min_lot）
    "star_min_lot": 200,
    "impact_cost": 0.0005,                                 # 对手价冲击成本（滑点）
}

BOARD_LIMIT = {"main": 0.10, "chinext": 0.20, "star": 0.20, "st": 0.05, "bse": 0.30}
POSITION_RANGES = {"trend_up": (0.60, 0.90), "range": (0.30, 0.60),
                   "downtrend": (0.00, 0.30), "panic": (0.00, 0.30)}
HORIZON_DAYS = {"1d": 1, "3d": 3, "5d": 5, "1w": 5}        # 交易日；1w≈一周5日
EVENT_TYPES = ("BOOT", "SIGNAL_REG", "SIGNAL_OUTCOME", "DECISION_ACCEPTED",
               "DECISION_REJECTED", "DECISION_VETOED", "ORDER_CREATED",
               "ORDER_FILLED", "ORDER_PART", "ORDER_EXPIRED", "ORDER_QUEUED",
               "DAY_SNAPSHOT", "CIRCUIT_TRIGGERED", "FREEZE", "RESUME", "CONFIG")


def now_cn() -> datetime:
    return datetime.now(CN_TZ)


def board_of(symbol: str, name: str = "") -> str:
    s = symbol.lower()
    if name and "st" in name.lower():
        return "st"
    if s.startswith("sh68"):
        return "star"
    if s.startswith("sz30"):
        return "chinext"
    if s.startswith(("bj8", "bj4")) or s.startswith(("8", "4")) and len(s) == 6 and not s[0] in "365":
        return "bse"
    return "main"


def is_sh(symbol: str) -> bool:
    return symbol.lower().startswith(("sh", "6"))


def round_price(p: float) -> float:
    return round(p + 1e-9, 2)


def parse_cn_ts(ts: str):
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


class Engine:
    def __init__(self, data_dir: str = DATA_DIR, config: dict | None = None):
        os.makedirs(data_dir, exist_ok=True)
        self.data_dir = data_dir
        self.lock = threading.RLock()
        self.cfg = dict(DEFAULT_CONFIG)
        if config:
            self.cfg.update(config)
        cfg_path = os.path.join(data_dir, "config.json")
        if os.path.exists(cfg_path):
            self.cfg.update(json.load(open(cfg_path)))
        self.db = sqlite3.connect(os.path.join(data_dir, "ledger.db"),
                                  check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self._init_tables()
        self._load_secrets()
        self._replay()
        self.log_event("BOOT", ref="engine", payload={
            "config": {k: v for k, v in self.cfg.items()},
            "positions": len(self.pos), "cash": self.cash})

    # ---------------------------------------------------------- 表结构与凭据

    def _init_tables(self):
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS events(
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL,
            type TEXT NOT NULL, ref TEXT, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS decisions(
            decision_id TEXT PRIMARY KEY, ts TEXT, state TEXT,
            prev_decision_id TEXT, snapshot_seq INTEGER, payload TEXT);
        CREATE TABLE IF NOT EXISTS signals(
            signal_id TEXT PRIMARY KEY, ts TEXT, group_name TEXT,
            group_version TEXT, brain TEXT, subject TEXT, symbols TEXT,
            direction TEXT, confidence INTEGER, horizon TEXT,
            market_state TEXT, invalidation TEXT, expires_at TEXT,
            ref_price REAL, status TEXT, outcome_score REAL,
            reg_day_index INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS state(
            k TEXT PRIMARY KEY, v TEXT);
        """)

    def _load_secrets(self):
        path = os.path.join(self.data_dir, "secrets.json")
        if os.path.exists(path):
            self.secrets = json.load(open(path))
        else:
            self.secrets = {k: uuid.uuid4().hex for k in
                            ("pm_token", "risk_token", "groups_token",
                             "feed_token", "admin_token")}
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(self.secrets, f, indent=1)

    # ---------------------------------------------------------- 事件与重放

    def log_event(self, etype, ref=None, payload=None, ts=None):
        cur = self.db.execute(
            "INSERT INTO events(ts,type,ref,payload) VALUES(?,?,?,?)",
            ((ts or now_cn().isoformat()), etype, ref,
             json.dumps(payload or {}, ensure_ascii=False)))
        self.db.commit()
        return cur.lastrowid

    def _replay(self):
        """审计 = 重放：从事件流重建持仓/现金/序号（BOOT 不重放撮合，只重建终态）。"""
        row = self.db.execute(
            "SELECT v FROM state WHERE k='projection'").fetchone()
        if row:
            p = json.loads(row[0])
            self.cash = p["cash"]
            self.pos = {s: dict(v) for s, v in p["positions"].items()}
            self.snapshot_seq = p["snapshot_seq"]
            self.high_water = p["high_water"]
            self.market_state = p.get("market_state")
            self.day_index = p.get("day_index", 0)
            self.marks = dict(p.get("marks") or {})
        else:
            self.cash = float(self.cfg["cash_initial"])
            self.pos = {}
            self.snapshot_seq = 0
            self.high_water = float(self.cfg["cash_initial"])
            self.market_state = None
            self.day_index = 0
            self.marks = {}
        self.frozen = False            # 行情断流 / admin 冻结（拒绝一切新单）
        self.opens_frozen = False      # PM 失联 / 总回撤 → 仅停新开仓
        self.trading_day = None
        self.bar_now = None            # bar 时钟（重放安全）：最近 bar 的时间
        self.pending_t1 = {}           # symbol → 次日释放股数（当日买入）
        self.bars = {}                 # symbol → 最近 bar
        self.prev_close = {}           # symbol → 昨收
        self.orders = {}               # order_id → order dict（挂单/排队）
        self.last_decision_ts = None
        self.day_ref_equity = self.equity()

    def _now(self):
        """引擎时钟：有 bar 时钟用 bar 时钟（重放/回测一致），否则墙钟。"""
        return self.bar_now or now_cn()

    def _parse_ts(self, ts):
        return parse_cn_ts(ts)

    def _persist_projection(self):
        self.db.execute(
            "INSERT OR REPLACE INTO state(k,v) VALUES('projection',?)",
            (json.dumps({"cash": self.cash, "positions": self.pos,
                         "snapshot_seq": self.snapshot_seq,
                         "high_water": self.high_water,
                         "market_state": self.market_state,
                         "day_index": self.day_index,
                         "marks": {s: self.last_price(s)
                                   for s in self.pos}}),))
        self.db.commit()

    # ---------------------------------------------------------- 组合计算

    def last_price(self, symbol):
        b = self.bars.get(symbol)
        if b:
            return b["close"]
        if symbol in self.marks:          # 重启恢复的最新估值（防陈旧成交价失真）
            return self.marks[symbol]
        return self.pos.get(symbol, {}).get("last_price")

    def equity(self):
        return self.cash + sum(
            p["qty"] * (self.last_price(s) or p["last_price"])
            for s, p in self.pos.items())

    def position_weight(self, symbol):
        eq = self.equity()
        if eq <= 0:
            return 0.0
        p = self.pos.get(symbol)
        if not p:
            return 0.0
        return p["qty"] * (self.last_price(symbol) or p["last_price"]) / eq

    def publish_snapshot(self, reason=""):
        self.snapshot_seq += 1
        eq = self.equity()
        self.high_water = max(self.high_water, eq)
        self.log_event("DAY_SNAPSHOT" if reason == "day" else "CONFIG",
                       ref=f"snap-{self.snapshot_seq}", payload=self.snapshot_dict())
        self._persist_projection()
        return self.snapshot_seq

    def snapshot_dict(self):
        eq = self.equity()
        return {
            "seq": self.snapshot_seq, "ts": self._now().isoformat(),
            "cash": round(self.cash, 2), "equity": round(eq, 2),
            "cash_weight": round(self.cash / eq, 4) if eq else 1.0,
            "market_state": self.market_state,
            "high_water": round(self.high_water, 2),
            "positions": [
                {"symbol": s, "qty": p["qty"], "avail": p["avail"],
                 "last": self.last_price(s) or p["last_price"],
                 "weight": round(self.position_weight(s), 4),
                 "sector": p.get("sector")}
                for s, p in sorted(self.pos.items())]}

    # ---------------------------------------------------------- T+1 与日切

    def _on_new_trading_day(self, day: str):
        self.trading_day = day
        self.day_index += 1
        # 昨收 = 上一日最后 bar close（首日无昨收则用当日开盘价近似）
        for s, b in self.bars.items():
            self.prev_close[s] = b["close"]
        # T+1 释放：昨日买入今日可卖
        for s, qty in self.pending_t1.items():
            if s in self.pos:
                self.pos[s]["avail"] += qty
        self.pending_t1 = {}
        self.day_ref_equity = self.equity()
        self.log_event("CONFIG", ref="day-open",
                       payload={"day": day, "day_index": self.day_index})

    def _on_day_close(self, day: str):
        self._expire_orders(scope="day")
        self.publish_snapshot(reason="day")

    # ---------------------------------------------------------- bar 喂入 → 撮合主入口

    def feed_bars(self, bars: list) -> dict:
        """bars: [{symbol, ts, open, high, low, close, volume, halted?}] 按时间升序。
        返回处理摘要。断流恢复（冻结中收到新 bar）→ 先补价重估再解冻（§六）。"""
        out = {"bars": 0, "fills": 0, "circuit": None, "unfrozen": False,
               "snapshots": 0, "outcomes": 0}
        with self.lock:
            for bar in bars:
                out["bars"] += 1
                sym = bar["symbol"]
                ts = bar["ts"]
                day, hms = ts.split("T")[0], ts.split("T")[1][:8]
                if self.trading_day != day:
                    self._on_new_trading_day(day)
                if self.frozen:
                    out["unfrozen"] = True
                    self._resume(reason="bar-resumed")
                self.bar_now = self._parse_ts(ts) or self.bar_now
                self.bars[sym] = bar
                if bar.get("prev_close") is not None:
                    self.prev_close[sym] = bar["prev_close"]
                elif sym not in self.prev_close:
                    self.prev_close[sym] = bar["open"]   # 首日无昨收：开盘价近似限幅基价
                # 先撮合（15:00 收盘价 print 也参与收盘集合竞价撮合），后日切
                self._match_symbol(sym, bar, out)
                self._check_invalidation(sym, bar, out)
                if hms >= "15:00:00" and getattr(self, "_day_closed", None) != day:
                    self._on_day_close(day)
                    self._day_closed = day
                    out["snapshots"] += 1
            if bars:
                out["circuit"] = self._check_breakers()
                out["outcomes"] = self._settle_outcomes()
                self._persist_projection()
        return out

    # ---------------------------------------------------------- 撮合

    def _limit_prices(self, symbol):
        """涨跌停价（昨收 × 板块限幅，0.01 取整）。"""
        pc = self.prev_close.get(symbol)
        if not pc:
            return None, None
        lim = BOARD_LIMIT[board_of(symbol)]
        return round_price(pc * (1 - lim)), round_price(pc * (1 + lim))

    def _match_symbol(self, sym, bar, out):
        if bar.get("halted"):
            return
        limit_dn, limit_up = self._limit_prices(sym)
        for oid in list(self.orders):
            o = self.orders[oid]
            if o["symbol"] != sym or o["state"] != "open":
                continue
            # 委托窗口：open_auction 等首个 bar（近似竞价 print=open），
            # close_auction 只在 14:57 后成交，intraday 即时
            if o["urgency"] == "close_auction" and bar["ts"][11:16] < "14:57":
                continue
            # 涨停封死买单排队 / 跌停封死卖单排队（开板才按时间优先成交）
            locked = (o["side"] == "buy" and limit_up and bar["low"] >= limit_up) or \
                     (o["side"] == "sell" and limit_dn and bar["high"] <= limit_dn)
            if locked:
                if not o.get("queued_at"):
                    o["queued_at"] = bar["ts"]
                    self.log_event("ORDER_QUEUED", ref=oid,
                                   payload={"reason": "limit_locked", "ts": bar["ts"]})
                continue
            qty = self._fillable_qty(o, bar)
            if qty <= 0:
                continue
            px = self._fill_price(o, bar, limit_up, limit_dn)
            if px is None:
                continue
            self._execute_fill(o, px, qty, bar, out)

    def _fillable_qty(self, o, bar):
        """本 bar 可成交量：受剩余量与 bar 成交量 10% 上限约束。
        买入必须整手（科创板 <200 股的买单直接不成立）；卖出零股不受整手约束。"""
        remain = o["qty"] - o["filled"]
        cap = int(bar["volume"] * self.cfg["volume_cap_pct"])
        lot = self.cfg["lot_size"]
        if board_of(o["symbol"]) == "star" and o["side"] == "buy" and \
                o["qty"] < self.cfg["star_min_lot"]:
            return 0
        if o["side"] == "buy":
            remain = remain // lot * lot
        return max(0, min(remain, cap // lot * lot))

    def _fill_price(self, o, bar, limit_up, limit_dn):
        """成交价 = 对手价±冲击成本；买限价（引擎换算 ref×(1+chase)）低于对手价则
        本 bar 不成交（价格跑掉），涨跌停封死已由 _match_symbol 排队逻辑前置。"""
        chase_pct = o.get("max_chase_pct")
        if chase_pct is None:
            chase_pct = self.cfg["max_chase_pct_default"]
        if o["side"] == "buy":
            ask = round_price(bar["close"] * (1 + self.cfg["impact_cost"]))
            chase = round_price(o.get("limit_price") or
                                o["ref_price"] * (1 + chase_pct))
            if ask > chase:
                return None            # 对手价高出限价：本 bar 不成交
            return ask
        bid = round_price(bar["close"] * (1 - self.cfg["impact_cost"]))
        chase = round_price(o.get("limit_price") or
                            o["ref_price"] * (1 - chase_pct))
        if bid < chase:
            return None                # 对手价低于限价：本 bar 不成交
        return bid

    def _execute_fill(self, o, px, qty, bar, out):
        fee = self._fee(o["symbol"], o["side"], px, qty)
        if o["side"] == "buy":
            cost = px * qty + fee
            if cost > self.cash:                       # 资金不足按可买量缩
                qty = int(self.cash / (px * (1 + 0.001)) ) // self.cfg["lot_size"] \
                    * self.cfg["lot_size"]
                if qty <= 0:
                    o["state"] = "expired"
                    self.log_event("ORDER_EXPIRED", ref=o["order_id"],
                                   payload={"reason": "insufficient_cash"})
                    return
                fee = self._fee(o["symbol"], o["side"], px, qty)
                cost = px * qty + fee
            self.cash -= cost
            p = self.pos.setdefault(o["symbol"], {
                "qty": 0, "avail": 0, "last_price": px, "sector": o.get("sector")})
            p["qty"] += qty
            p["last_price"] = px
            self.pending_t1[o["symbol"]] = self.pending_t1.get(o["symbol"], 0) + qty
        else:
            p = self.pos.get(o["symbol"])
            if not p or p["avail"] < qty:              # T+1：可卖不足按可卖量缩
                qty = (p["avail"] if p else 0) // self.cfg["lot_size"] \
                    * self.cfg["lot_size"]
                if qty <= 0:
                    o["state"] = "expired"
                    self.log_event("ORDER_EXPIRED", ref=o["order_id"],
                                   payload={"reason": "insufficient_avail"})
                    return
                fee = self._fee(o["symbol"], o["side"], px, qty)
            p["qty"] -= qty
            p["avail"] -= qty
            self.cash += px * qty - fee
            if p["qty"] == 0:
                del self.pos[o["symbol"]]
        o["filled"] += qty
        fully = o["filled"] >= o["qty"]
        o["state"] = "filled" if fully else "open"
        self.log_event("ORDER_FILLED" if fully else "ORDER_PART", ref=o["order_id"],
                       payload={"decision_id": o["decision_id"], "symbol": o["symbol"],
                                "side": o["side"], "price": px, "qty": qty,
                                "fee": round(fee, 2), "ts": bar["ts"],
                                "slippage_vs_bar_close": round(px / bar["close"] - 1, 5)})
        out["fills"] += 1
        self.publish_snapshot()

    def _fee(self, symbol, side, px, qty):
        amount = px * qty
        fee = max(amount * self.cfg["commission_rate"], self.cfg["commission_min"])
        if side == "sell":
            fee += amount * self.cfg["stamp_tax_rate"]
        if is_sh(symbol):
            fee += amount * self.cfg["transfer_fee_rate"]
        return fee

    def _expire_orders(self, scope="day"):
        for oid in list(self.orders):
            o = self.orders[oid]
            if o["state"] != "open":
                continue
            if scope == "day" or (o.get("valid_until") and
                                  o["valid_until"] < self._now().isoformat()):
                o["state"] = "expired"
                self.log_event("ORDER_EXPIRED", ref=oid,
                               payload={"reason": scope, "filled": o["filled"]})

    # ---------------------------------------------------------- 情报单注册

    def register_signal(self, sig: dict) -> dict:
        """只收注册元数据（invalidation 结构化 + 保鲜窗 + 计分字段），内容散文不入引擎。"""
        need = ("signal_id", "group", "group_version", "direction", "confidence",
                "horizon", "invalidation")
        missing = [k for k in need if k not in sig]
        if missing:
            return {"ok": False, "error": "missing_fields", "fields": missing}
        if sig["direction"] not in ("long", "short", "neutral"):
            return {"ok": False, "error": "bad_direction"}
        if not (isinstance(sig["confidence"], int) and 1 <= sig["confidence"] <= 10):
            return {"ok": False, "error": "confidence_must_be_int_1_10"}
        if sig["horizon"] not in HORIZON_DAYS:
            return {"ok": False, "error": "horizon_enum", "enum": list(HORIZON_DAYS)}
        inv = sig["invalidation"]
        if not isinstance(inv, dict) or not all(
                k in inv for k in ("symbol", "metric", "op", "threshold")):
            return {"ok": False, "error": "invalidation_must_be_structured",
                    "required": ["symbol", "metric", "op", "threshold"]}
        if inv["op"] not in ("<", ">"):
            return {"ok": False, "error": "invalidation_op"}
        expires = sig.get("expires_at")
        if not expires:                                   # 保鲜窗兜底，权威以引擎时钟+TTL 盖章
            expires = (self._now() + timedelta(
                minutes=self.cfg["signal_ttl_default_minutes"])).isoformat()
        ref_price = None
        subj = (sig.get("symbols") or [inv.get("symbol")])
        if subj and self.last_price(subj[0]):
            ref_price = self.last_price(subj[0])
        self.db.execute(
            "INSERT OR REPLACE INTO signals VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (sig["signal_id"], self._now().isoformat(), sig["group"],
             sig["group_version"], sig.get("brain"), sig.get("subject"),
             json.dumps(sig.get("symbols") or []), sig["direction"],
             sig["confidence"], sig["horizon"], sig.get("market_state"),
             json.dumps(inv), expires, ref_price, "active", None,
             self.day_index))
        self.db.commit()
        self.log_event("SIGNAL_REG", ref=sig["signal_id"], payload={
            "group": sig["group"], "group_version": sig["group_version"],
            "brain": sig.get("brain"), "direction": sig["direction"],
            "confidence": sig["confidence"], "horizon": sig["horizon"],
            "market_state": sig.get("market_state"), "expires_at": expires,
            "invalidation": inv, "ref_price": ref_price})
        return {"ok": True, "registered_at": self._now().isoformat(),
                "expires_at": expires, "ref_price": ref_price}

    def _check_invalidation(self, sym, bar, out):
        """invalidation 触发标注（触发时点记 flag；计分仍按方向盈亏，§四.3 防洗分）。"""
        for row in self.db.execute(
                "SELECT signal_id, invalidation, status FROM signals "
                "WHERE status='active' AND json_extract(invalidation,'$.symbol')=?",
                (sym,)):
            inv = json.loads(row["invalidation"])
            hit = False
            if inv["metric"] in ("price_low", "price"):
                hit = (bar["low"] < inv["threshold"]) if inv["op"] == "<" else \
                      (bar["low"] > inv["threshold"])
            elif inv["metric"] == "price_high":
                hit = (bar["high"] > inv["threshold"]) if inv["op"] == ">" else \
                      (bar["high"] < inv["threshold"])
            elif inv["metric"] == "volume":
                hit = (bar["volume"] < inv["threshold"]) if inv["op"] == "<" else \
                      (bar["volume"] > inv["threshold"])
            if hit:
                self.db.execute("UPDATE signals SET status='invalidated' "
                                "WHERE signal_id=?", (row["signal_id"],))
                self.db.commit()
                self.log_event("SIGNAL_OUTCOME", ref=row["signal_id"], payload={
                    "event": "invalidated", "ts": bar["ts"], "bar_low": bar["low"],
                    "note": "证伪纪律分+0.2 周演化会人工裁定，机器只记 flag"})

    def _settle_outcomes(self) -> int:
        """horizon 到期结算（交易日计数，重放安全）：s = ±2 × (c/10) × 实现度（§四.2）。
        保鲜窗 expires_at 只管 PM 可用性；结算按 reg_day_index + HORIZON 交易日。"""
        n = 0
        for row in self.db.execute(
                "SELECT * FROM signals WHERE status='active'").fetchall():
            horizon_days = HORIZON_DAYS.get(row["horizon"], 1)
            if self.day_index - (row["reg_day_index"] or 0) < horizon_days:
                continue
            n += 1
            n += 1
            sym = json.loads(row["symbols"] or "[]")
            score = None
            detail = {}
            if row["direction"] == "neutral" or not sym or not self.last_price(sym[0]):
                status, detail = "excluded", {"reason": "neutral_or_no_subject_price"}
            else:
                px0, px1 = row["ref_price"], self.last_price(sym[0])
                chg = px1 / px0 - 1 if px0 else 0.0
                realized = 1.0 if abs(chg) >= 0.03 else \
                    0.5 if abs(chg) >= 0.015 else 0.2
                correct = (chg > 0) if row["direction"] == "long" else (chg < 0)
                sign = 1 if correct else -1
                score = round(sign * 2 * (row["confidence"] / 10) * realized, 3)
                status = "settled"
                detail = {"ref_price": px0, "last_price": px1, "change": round(chg, 5),
                          "direction": row["direction"], "confidence": row["confidence"],
                          "realization": realized, "score": score}
            self.db.execute("UPDATE signals SET status=?, outcome_score=? "
                            "WHERE signal_id=?", (status, score, row["signal_id"]))
            self.db.commit()
            self.log_event("SIGNAL_OUTCOME", ref=row["signal_id"],
                           payload={"event": "settled", **detail})
        return n

    def credit_scores(self) -> dict:
        """权威账 SQL 派生（§四.4）：指数衰减加权均值，半衰期 30 交易日，4 桶，
        桶内 <10 样本向全局 shrinkage。缓存概念——审计=重放。"""
        half = 30.0
        out = {}
        rows = self.db.execute(
            "SELECT s.group_name, s.market_state, e.ts, s.outcome_score, s.status "
            "FROM signals s JOIN events e ON e.ref=s.signal_id AND e.type='SIGNAL_OUTCOME' "
            "WHERE s.outcome_score IS NOT NULL ORDER BY e.ts").fetchall()
        # 以事件 id 序为「交易日序」代理（重放口径），半衰按样本序 30 样本≈30 交易窗
        by_group = {}
        for i, r in enumerate(rows):
            w = 0.5 ** ((len(rows) - i) / half)
            g = r["group_name"]
            by_group.setdefault(g, []).append((w, r["outcome_score"], r["market_state"]))
        for g, samples in by_group.items():
            glob = sum(w * s for w, s, _ in samples) / sum(w for w, _, _ in samples)
            buckets = {}
            for state in POSITION_RANGES:
                ss = [(w, s) for w, s, ms in samples if ms == state]
                if ss:
                    cw = sum(w for w, _ in ss)
                    mean = sum(w * s for w, s in ss) / cw
                    k = len(ss)
                    buckets[state] = {
                        "score": round(mean * (k / 10) + glob * (1 - k / 10)
                                       if k < 10 else mean, 3),
                        "samples": k,
                        "shrunk": k < 10}
            out[g] = {"global": round(glob, 3), "samples": len(samples),
                      "buckets": buckets}
        return out

    # ---------------------------------------------------------- 决策单

    def _err(self, code, **kw):
        return {"ok": False, "error": code, **kw}

    def submit_decision(self, dec: dict) -> dict:
        """PM-only（token 在 HTTP 层验）。校验链：schema→链连续→快照→风控签章→
        硬约束→限流→现金下限。拒单不静默：原因码打回。"""
        with self.lock:
            need = ("decision_id", "prev_decision_id", "ref_signals",
                    "snapshot_seq", "target_positions", "cash_floor",
                    "risk_verdict")
            missing = [k for k in need if k not in dec]
            if missing:
                return self._reject(dec, "missing_fields", fields=missing)
            if dec["risk_verdict"].get("verdict") != "pass":
                return self._reject(dec, "risk_verdict_not_pass")
            if not isinstance(dec["cash_floor"], (int, float)) or \
                    dec["cash_floor"] < self.cfg["cash_floor"]:
                return self._reject(dec, "cash_floor_below_hard_floor",
                                    hard_floor=self.cfg["cash_floor"])
            # 链连续：prev 必须是最新终态链环（ACCEPTED/VETOED）；首单 prev=null
            last = self.db.execute(
                "SELECT decision_id FROM decisions WHERE state IN "
                "('ACCEPTED','VETOED') ORDER BY ts DESC, rowid DESC LIMIT 1").fetchone()
            expect = last["decision_id"] if last else None
            if dec["prev_decision_id"] != expect:
                return self._reject(dec, "chain_mismatch",
                                    expected_prev=expect)
            # 快照：决策基于的持仓快照必须仍是最新（防按旧仓位差分）
            if dec["snapshot_seq"] != self.snapshot_seq:
                return self._reject(dec, "snapshot_stale",
                                    current_seq=self.snapshot_seq)
            # ref_signals 必须已注册且未过期（无证伪条件的情报不入输入）
            for sid in dec["ref_signals"]:
                r = self.db.execute("SELECT expires_at, status FROM signals "
                                    "WHERE signal_id=?", (sid,)).fetchone()
                if not r:
                    return self._reject(dec, "signal_not_registered", signal=sid)
                if r["status"] != "active" or r["expires_at"] < self._now().isoformat():
                    return self._reject(dec, "signal_expired_or_inactive",
                                        signal=sid, status=r["status"])
            # 硬约束代码先挡
            cap = max(self.cfg["max_single_weight"],
                      dec.get("max_single_weight") or 0)
            for tp in dec["target_positions"]:
                if tp["weight"] > cap:
                    return self._reject(dec, "single_weight_over_cap",
                                        symbol=tp["symbol"], cap=cap)
            sector_cap = min(
                [x for x in (self.cfg["sector_cap"], dec.get("sector_cap")) if x],
                default=None)
            if sector_cap:
                sectors = {}
                for tp in dec["target_positions"]:
                    sec = self.pos.get(tp["symbol"], {}).get("sector") or tp.get("sector")
                    if sec:
                        sectors[sec] = sectors.get(sec, 0) + tp["weight"]
                bad = {s: w for s, w in sectors.items() if w > sector_cap}
                if bad:
                    return self._reject(dec, "sector_cap_breached", sectors=bad)
            if sum(t["weight"] for t in dec["target_positions"]) > \
                    1 - dec["cash_floor"] + 1e-9:
                return self._reject(dec, "target_exceeds_cash_floor")
            # 限流：ACCEPTED 计（拒/否不占窗）
            gap = (self._now() - self.last_decision_ts).total_seconds() \
                if self.last_decision_ts else 1e9
            if gap < self.cfg["pm_rate_limit_seconds"]:
                return self._reject(dec, "rate_limited",
                                    retry_after=round(self.cfg["pm_rate_limit_seconds"] - gap))
            if self.frozen:
                return self._reject(dec, "engine_frozen")
            if self.opens_frozen and any(
                    (self.pos.get(t["symbol"], {}).get("qty", 0) or 0) < 1e-9
                    and t["weight"] > 0 for t in dec["target_positions"]):
                return self._reject(dec, "new_opens_frozen")

            # 受理 → 差分订单
            valid_until = (self._now() + timedelta(
                minutes=self.cfg["decision_ttl_minutes"])).isoformat()
            dec["_valid_until"] = valid_until
            self.db.execute(
                "INSERT INTO decisions VALUES(?,?,?,?,?,?)",
                (dec["decision_id"], self._now().isoformat(), "ACCEPTED",
                 dec["prev_decision_id"], dec["snapshot_seq"],
                 json.dumps(dec, ensure_ascii=False)))
            self.db.commit()
            self.last_decision_ts = self._now()
            orders = self._diff_orders(dec, valid_until)
            self.log_event("DECISION_ACCEPTED", ref=dec["decision_id"], payload={
                "ref_signals": dec["ref_signals"], "orders": [o["order_id"] for o in orders],
                "valid_until": valid_until, "market_state": dec.get("market_state")})
            self.publish_snapshot()
            return {"ok": True, "decision_id": dec["decision_id"],
                    "valid_until": valid_until, "orders": [o["order_id"] for o in orders]}

    def _reject(self, dec, code, **kw):
        did = dec.get("decision_id") or "unknown"
        self.db.execute(
            "INSERT OR REPLACE INTO decisions VALUES(?,?,?,?,?,?)",
            (did, self._now().isoformat(), "REJECTED:" + code,
             dec.get("prev_decision_id"), dec.get("snapshot_seq"),
             json.dumps(dec, ensure_ascii=False)))
        self.db.commit()
        self.log_event("DECISION_REJECTED", ref=did,
                       payload={"error": code, **kw})
        return self._err(code, **kw)

    def veto_decision(self, dec_id: str, by: str, note: str = "") -> dict:
        """风控一票否决：否决单也是链上一环（prev 永指最新链环，含 veto）。"""
        with self.lock:
            r = self.db.execute("SELECT state FROM decisions WHERE decision_id=?",
                                (dec_id,)).fetchone()
            if not r or r["state"] != "ACCEPTED":
                return self._err("not_cancellable", state=r["state"] if r else None)
            # 撤销该单未成交挂单
            for oid, o in self.orders.items():
                if o["decision_id"] == dec_id and o["state"] == "open":
                    o["state"] = "cancelled"
            self.db.execute("UPDATE decisions SET state='VETOED' WHERE decision_id=?",
                            (dec_id,))
            self.db.commit()
            self.log_event("DECISION_VETOED", ref=dec_id, payload={"by": by, "note": note})
            return {"ok": True, "vetoed": dec_id}

    def _diff_orders(self, dec, valid_until):
        """订单 = 目标 − 当前 的差分（§对原方案修正 1：买卖统一）。"""
        eq = self.equity()
        orders = []
        targets = {t["symbol"]: t for t in dec["target_positions"]}
        for sym, t in targets.items():
            cur = self.pos.get(sym, {}).get("qty", 0)
            px = self.last_price(sym) or t.get("ref_price") or 0
            if not px:
                continue
            want_qty = int(round(t["weight"] * eq / px))
            delta = want_qty - cur
            lot = self.cfg["lot_size"]
            if board_of(sym) == "star" and 0 < abs(delta) < self.cfg["star_min_lot"]:
                continue
            delta = int(delta / lot) * lot if abs(delta) >= lot else \
                (delta if cur > 0 else 0)          # 已持仓的零股清仓允许
            if delta == 0:
                continue
            oid = "ord-" + uuid.uuid4().hex[:12]
            o = {"order_id": oid, "decision_id": dec["decision_id"],
                 "symbol": sym, "side": "buy" if delta > 0 else "sell",
                 "qty": abs(delta), "filled": 0, "state": "open",
                 "urgency": t.get("urgency", "intraday_event"),
                 "ref_price": px, "limit_price": None,
                 "max_chase_pct": dec.get("max_chase_pct"),
                 "valid_until": valid_until, "sector": t.get("sector")}
            self.orders[oid] = o
            self.log_event("ORDER_CREATED", ref=oid, payload={
                "decision_id": dec["decision_id"], "symbol": sym, "side": o["side"],
                "qty": o["qty"], "urgency": o["urgency"],
                "max_chase_pct": o["max_chase_pct"], "valid_until": valid_until})
            orders.append(o)
        # 目标表里没有的现有持仓 = 清仓差分
        for sym, p in self.pos.items():
            if sym in targets:
                continue
            px = self.last_price(sym) or p["last_price"]
            delta = -int(p["qty"] / self.cfg["lot_size"]) * self.cfg["lot_size"] \
                if p["qty"] >= self.cfg["lot_size"] else -p["qty"]
            if delta == 0:
                continue
            oid = "ord-" + uuid.uuid4().hex[:12]
            o = {"order_id": oid, "decision_id": dec["decision_id"], "symbol": sym,
                 "side": "sell", "qty": abs(delta), "filled": 0, "state": "open",
                 "urgency": "intraday_event", "ref_price": px, "limit_price": None,
                 "max_chase_pct": None, "valid_until": valid_until,
                 "sector": p.get("sector")}
            self.orders[oid] = o
            self.log_event("ORDER_CREATED", ref=oid, payload={
                "decision_id": dec["decision_id"], "symbol": sym, "side": "sell",
                "qty": o["qty"], "urgency": "intraday_event", "reason": "target_absent"})
            orders.append(o)
        return orders

    # ---------------------------------------------------------- 熔断与冻结

    def _check_breakers(self):
        eq = self.equity()
        if eq > self.high_water:
            self.high_water = eq
        r = None
        # 总回撤 >15% → 停新开仓（不再自动恢复，演化组复盘后 admin 解）
        if not self.opens_frozen and self.high_water > 0 and \
                eq / self.high_water - 1 < -self.cfg["total_drawdown_breaker"]:
            self.opens_frozen = True
            r = "total_drawdown"
            self.log_event("CIRCUIT_TRIGGERED", ref="total-dd", payload={
                "equity": round(eq, 2), "high_water": round(self.high_water, 2),
                "action": "new_opens_frozen"})
        # 单日浮亏 >3% → 强制降仓至仓位下限（免签章，规则先行）
        if self.day_ref_equity > 0 and \
                eq / self.day_ref_equity - 1 < -self.cfg["daily_loss_breaker"] \
                and not getattr(self, "_circuit_fired_day", None) == self.trading_day:
            self._circuit_fired_day = self.trading_day
            self._force_reduce()
            r = "daily_loss"
        return r

    def _force_reduce(self):
        floor = self.cfg["panic_position_floor"]
        eq = self.equity()
        target_value = eq * floor
        cur_value = eq - self.cash
        if cur_value <= target_value:
            return
        ratio = 1 - (cur_value - target_value) / cur_value
        dec_id = "circuit-" + self._now().strftime("%Y%m%d-%H%M%S")
        n = 0
        for sym, p in list(self.pos.items()):
            px = self.last_price(sym) or p["last_price"]
            sell = int(p["avail"] * ratio / self.cfg["lot_size"]) * self.cfg["lot_size"]
            if sell <= 0:
                continue
            oid = "ord-" + uuid.uuid4().hex[:12]
            self.orders[oid] = {
                "order_id": oid, "decision_id": dec_id, "symbol": sym, "side": "sell",
                "qty": sell, "filled": 0, "state": "open", "urgency": "intraday_event",
                "ref_price": px, "limit_price": None, "max_chase_pct": None,
                "valid_until": (self._now() + timedelta(minutes=30)).isoformat(),
                "sector": p.get("sector")}
            self.log_event("ORDER_CREATED", ref=oid, payload={
                "decision_id": dec_id, "symbol": sym, "side": "sell", "qty": sell,
                "urgency": "intraday_event", "reason": "circuit_force_reduce"})
            n += 1
        self.log_event("CIRCUIT_TRIGGERED", ref=dec_id, payload={
            "day_ref_equity": round(self.day_ref_equity, 2),
            "equity": round(eq, 2), "reduce_ratio": round(ratio, 3),
            "orders": n, "note": "免签章·规则先行·事后审计"})

    def freeze(self, scope="all", reason=""):
        with self.lock:
            if scope == "all":
                self.frozen = True
            else:
                self.opens_frozen = True
            self.log_event("FREEZE", ref=scope, payload={"reason": reason})
            return {"ok": True, "frozen": scope}

    def _resume(self, reason=""):
        self.frozen = False
        self.log_event("RESUME", ref="all", payload={"reason": reason})

    def resume(self, reason="admin"):
        with self.lock:
            self._resume(reason)
            return {"ok": True}

    # ---------------------------------------------------------- 行情断流感知

    def status(self):
        last_bar = max((b["ts"] for b in self.bars.values()), default=None)
        stale = None
        if last_bar:
            try:
                t = datetime.fromisoformat(last_bar)
                age = (now_cn() - t).total_seconds()
                hms = now_cn().strftime("%H:%M")
                in_session = "09:15" <= hms <= "15:00" and now_cn().weekday() < 5
                if in_session and age > self.cfg["bar_stale_freeze_seconds"] \
                        and not self.frozen:
                    self.freeze(scope="all", reason="bar_stale")
            except ValueError:
                pass
        return {"ok": True, "frozen": self.frozen, "opens_frozen": self.opens_frozen,
                "snapshot_seq": self.snapshot_seq,
                "day_ref_equity": round(self.day_ref_equity, 2),
                "equity": round(self.equity(), 2),
                "day_pnl": round(self.equity() / self.day_ref_equity - 1, 5)
                if self.day_ref_equity else 0.0,
                "total_dd": round(self.equity() / self.high_water - 1, 5),
                "last_bar_ts": last_bar, "open_orders":
                    sum(1 for o in self.orders.values() if o["state"] == "open"),
                "ts": now_cn().isoformat()}


# ---------------------------------------------------------------- HTTP 层

class Handler(BaseHTTPRequestHandler):
    engine: Engine = None

    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _auth(self, role):
        tok = self.headers.get("X-Api-Token")
        if not tok and self.path and "token=" in self.path:
            m = re.search(r"[?&]token=([^&]+)", self.path)
            tok = m.group(1) if m else None
        return bool(tok) and tok == self.engine.secrets.get(role)

    def do_GET(self):
        path = self.path.split("?")[0]
        e = self.engine
        if path == "/status":
            return self._json(200, e.status())
        if path == "/snapshot":
            return self._json(200, {"ok": True, **e.snapshot_dict()})
        if path == "/positions":
            return self._json(200, {"ok": True, "positions": e.snapshot_dict()["positions"],
                                    "cash": round(e.cash, 2)})
        if path == "/credit":
            return self._json(200, {"ok": True, "credit": e.credit_scores()})
        if path == "/events":
            q = dict(p.split("=", 1) for p in self.path.split("?")[1].split("&")
                     if "=" in p) if "?" in self.path else {}
            since = int(q.get("since_id", 0))
            typ = q.get("type")
            sql = "SELECT id,ts,type,ref,payload FROM events WHERE id>?"
            args = [since]
            if typ:
                sql += " AND type=?"
                args.append(typ)
            rows = e.db.execute(sql + " ORDER BY id LIMIT 500", args).fetchall()
            return self._json(200, {"ok": True, "events": [
                {"id": r["id"], "ts": r["ts"], "type": r["type"], "ref": r["ref"],
                 "payload": json.loads(r["payload"])} for r in rows]})
        if path == "/decisions":
            rows = e.db.execute("SELECT * FROM decisions ORDER BY rowid DESC "
                                "LIMIT 50").fetchall()
            return self._json(200, {"ok": True, "decisions": [
                {"decision_id": r["decision_id"], "ts": r["ts"], "state": r["state"],
                 "prev": r["prev_decision_id"]} for r in rows]})
        return self._json(404, {"ok": False, "error": "not_found"})

    def do_POST(self):
        path = self.path.split("?")[0]
        e = self.engine
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            return self._json(400, {"ok": False, "error": "bad_json"})
        role_by_path = {"/decision": "pm_token", "/veto": "risk_token",
                        "/signal": "groups_token", "/bars": "feed_token",
                        "/admin": "admin_token"}
        role = None
        for p, r in role_by_path.items():
            if path.startswith(p):
                role = r
        if role and not self._auth(role):
            return self._json(403, {"ok": False, "error": "bad_token"})
        if path == "/signal":
            return self._json(200, e.register_signal(body))
        if path == "/decision":
            return self._json(200, e.submit_decision(body))
        if path == "/veto":
            return self._json(200, e.veto_decision(
                body.get("decision_id"), body.get("by", "risk-watch"),
                body.get("note", "")))
        if path == "/bars":
            bars = body.get("bars") or []
            if not isinstance(bars, list):
                return self._json(400, {"ok": False, "error": "bars_must_be_list"})
            return self._json(200, {"ok": True, **e.feed_bars(bars)})
        if path == "/admin/freeze":
            return self._json(200, e.freeze(body.get("scope", "all"),
                                            body.get("reason", "")))
        if path == "/admin/resume":
            return self._json(200, e.resume(body.get("reason", "admin")))
        return self._json(404, {"ok": False, "error": "not_found"})


def main():
    port = int(os.environ.get("STOCKSIM_S1_PORT", "8890"))
    eng = Engine()
    Handler.engine = eng
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"S1 撮合引擎 :{port} data={DATA_DIR}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
