#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""S2 调度器（xusi 侧纯代码，无 LLM）。

职责（final-plan v7 §七，铁律：只投信与验收，绝不改任何 agent 状态、不代写单据）：
- 时间轴编排（Asia/Shanghai）：08:30 盘前唤醒 env-lead/theme-lead；
  15:05 盘后验快照、催 attr-book 日账、催 liaison 汇报；周六 09:00 催 evo-lab。
- 心跳看门狗：常态 60min / 盘中 15min，缺 2 次判失联 → §六 降级动作
  （PM 失联 → S1 freeze new_opens；其余 → 告警 + 记录）。
- 信件路由：POST /letters 中继——走管理面 mail 通道（内核账本行 + 门铃，
  不依赖各员自建 inbox；凭据现取现用，值不打印不落日志）。
- 预算降频：日费超帽 → 事件推送减半 flag（超帽降频不瘫痪）。

stdlib-only。roster 配置 agents.json（name→agent_id+port+role），值不打印。
"""

import json
import os
import threading
import time
import tomllib
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CN_TZ = timezone(timedelta(hours=8))
DATA_DIR = os.environ.get(
    "STOCKSIM_S2_DATA", os.path.join(os.path.dirname(__file__), "data", "s2"))
S1_URL = os.environ.get("STOCKSIM_S1_URL", "http://127.0.0.1:8890")
XUSI_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DEFAULT_TIMELINE = {          # 交易日时间轴（cron 语义内联，60s tick 扫描）
    "08:30": "preopen",       # 唤醒 env-lead / theme-lead
    "09:26": "auction_check",
    "11:35": "midday",
    "15:05": "postclose",     # 验快照 + 催日账 + 催汇报
    "15:40": "attr_check",
}
WEEKEND_TIMELINE = {"09:00": "weekly"}   # 周六：催演化周会（P3 前 evo-lab 备用应答待命）


def timeline_for(t):
    """按星期选时间轴：周日全空（周会仅周六）；周六周末表；其余平日表。
    （原 weekday()==5 漏周日、改 >=5 又把周日带进周末表——2026-10-11 两度实踩）"""
    wd = t.weekday()
    if wd == 6:
        return {}
    return WEEKEND_TIMELINE if wd == 5 else DEFAULT_TIMELINE

DEFAULT_ROLES = {             # 心跳期望间隔（秒）：常态 60min / 盘中 15min
    "pm": 3600, "risk": 3600, "env": 3600, "theme": 3600, "attr": 3600,
    "keeper": 3600, "liaison": 3600, "stock": 3600, "evo": 3600,
}
MISS_LIMIT = 2


def now_cn():
    return datetime.now(CN_TZ)


def hms():
    return now_cn().strftime("%H:%M")


def is_trading_time():
    t = now_cn()
    if t.weekday() >= 5:
        return False
    return "09:15" <= hms() <= "15:00"


class Scheduler:
    def __init__(self, data_dir=DATA_DIR):
        os.makedirs(data_dir, exist_ok=True)
        self.data_dir = data_dir
        self.lock = threading.Lock()
        self.roster_path = os.path.join(data_dir, "agents.json")
        self.roster = {}
        if os.path.exists(self.roster_path):
            self.roster = json.load(open(self.roster_path))
        self.heartbeats = {}      # name → iso ts
        self.missed = {}          # name → 连续缺席次数
        self.down = set()         # 判失联的岗
        self.started_at = time.time()   # P-20261011-01：零心跳宽限锚点（重启重起算）
        self.fired = {}           # 当日已触发的时间轴任务（落盘防重启重发）
        self.state_path = os.path.join(data_dir, "state.json")
        self._load_state()
        self.budget = {"daily_cap": 40.0, "spent": 0.0, "throttle": False}
        self.alert_log = os.path.join(data_dir, "alerts.log")

    # ---------------------------------------------------------- 状态落盘

    def _load_state(self):
        """重启续防重发：只接当日 fired（evo-lab 周会三连发实踩——fired 在内存，
        S2 每次重启都把当天已触发任务再发一遍）。"""
        try:
            if os.path.exists(self.state_path):
                st = json.load(open(self.state_path))
                today = now_cn().strftime("%Y-%m-%d")
                self.fired = {k: v for k, v in st.get("fired", {}).items()
                              if k.startswith(today)}
        except Exception:
            self.fired = {}

    def _save_state(self):
        try:
            today = now_cn().strftime("%Y-%m-%d")
            live = {k: v for k, v in self.fired.items() if k.startswith(today)}
            tmp = self.state_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"fired": live}, f, ensure_ascii=False)
            os.replace(tmp, self.state_path)
        except Exception:
            pass

    # ---------------------------------------------------------- 信件投递

    def _mgmt_auth(self):
        """管理面 admin 凭据现取现用：先 env STOCKSIM_MGMT_KEY_FILE 指向的
        JSON（{"url","secret"}——容器部署缝，base-keeper 侧）；回落
        etc/xusi.toml [admin].secret（宿主直跑）。值不返回给调用方、不落日志。"""
        kf = os.environ.get("STOCKSIM_MGMT_KEY_FILE")
        if kf:
            try:
                d = json.load(open(kf))
                return str(d.get("secret", "")).strip(), str(d.get("url", "")).strip()
            except Exception:
                pass
        try:
            raw = tomllib.load(open(os.path.join(XUSI_ROOT, "etc", "xusi.toml"), "rb"))
            host = str(raw.get("server", {}).get("host", "127.0.0.1"))
            port = int(raw.get("server", {}).get("port", 8701))
            sec = str(raw.get("admin", {}).get("secret", "")).strip()
            return (sec, f"http://{host}:{port}") if sec else (None, None)
        except Exception:
            return None, None

    def send_letter(self, to_name, subject, body, from_name="s2-scheduler",
                    notify_on_fail=True):
        """投信 = 管理面 mail API（内核追加账本行并按铃唤醒）。
        text 面贯通 letter 语义：主题首行 + 空行 + 正文；from 署名入文。"""
        entry = self.roster.get(to_name)
        if not entry or not entry.get("agent_id"):
            return {"ok": False, "error": "not_in_roster", "to": to_name}
        sec, base = self._mgmt_auth()
        if not sec:
            if notify_on_fail:
                self.alert(f"letter_to_{to_name}_failed: no_mgmt_secret")
            return {"ok": False, "error": "no_mgmt_secret"}
        text = f"[{from_name}] {subject}\n\n{body}"
        req = urllib.request.Request(
            f"{base}/api/agents/{entry['agent_id']}/mail",
            data=json.dumps({"text": text}).encode(), method="POST",
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {sec}"})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return {"ok": True, "status": resp.status}
        except Exception as exc:
            if notify_on_fail:
                self.alert(f"letter_to_{to_name}_failed: {exc}")
            return {"ok": False, "error": str(exc)}

    def alert(self, msg):
        line = f"{now_cn().isoformat()} {msg}\n"
        with open(self.alert_log, "a") as f:
            f.write(line)
        # 告警主人通道：北斗在册（notify_on_fail=False 防告警递归）
        if "beidou" in self.roster:
            self.send_letter("beidou", "[告警] stocksim-s2", msg,
                             from_name="s2-scheduler", notify_on_fail=False)

    # ---------------------------------------------------------- S1 交互

    def s1_get(self, path):
        try:
            with urllib.request.urlopen(S1_URL + path, timeout=5) as r:
                return json.loads(r.read())
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def s1_admin(self, action, body):
        admin_token = self._s1_admin_token()
        if not admin_token:
            return {"ok": False, "error": "no_s1_admin_token"}
        req = urllib.request.Request(
            f"{S1_URL}/admin/{action}",
            data=json.dumps(body).encode(), method="POST",
            headers={"Content-Type": "application/json",
                     "X-Api-Token": admin_token})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return json.loads(r.read())
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def _s1_admin_token(self):
        p = os.path.join(os.path.dirname(DATA_DIR.rstrip("/")),
                         "s1", "secrets.json")
        if os.path.exists(p):
            return json.load(open(p)).get("admin_token")
        return None

    # ---------------------------------------------------------- 时间轴

    def tick(self):
        t = now_cn()
        day = t.strftime("%Y-%m-%d")
        timeline = timeline_for(t)
        fired_new = False
        for hhmm, job in timeline.items():
            key = f"{day}/{hhmm}"
            if hms() >= hhmm and self.fired.get(key) != job:
                self.fired[key] = job
                fired_new = True
                try:
                    self.run_job(job)
                except Exception as exc:
                    self.alert(f"job_{job}_error: {exc}")
        if fired_new:
            self._save_state()
        self.watchdog()

    def run_job(self, job):
        if job == "preopen":
            for to in ("env-lead", "theme-lead"):
                self.send_letter(to, "[钟点] 盘前开工",
                                 "08:30 盘前时点到：刷隔夜数据，出晨报，投 PM。")
        elif job == "auction_check":
            st = self.s1_get("/status")
            if not st.get("ok"):
                self.alert("S1 无响应于竞价检查时点——引擎失联，全 fleet 停止决策（§六）")
        elif job == "midday":
            for to in ("env-lead", "theme-lead"):
                self.send_letter(to, "[钟点] 午评", "11:30 午评时点：出午评情报单。")
        elif job == "postclose":
            st = self.s1_get("/status")
            snap = self.s1_get("/snapshot")
            if snap.get("ok"):
                self.send_letter("attr-book", "[钟点] 盘后日账",
                                 "15:00 收盘：引擎已出快照 seq=%s，请出当日归因账。"
                                 % snap.get("seq"))
                self.send_letter("liaison", "[钟点] 每日汇报",
                                 "收盘：请向 a019 汇报当日盈亏/交易摘要/异常（断联走 mail）。")
            else:
                self.alert("盘后 S1 快照不可得")
        elif job == "attr_check":
            pass  # P1: 检查 attr 日账投信，超时催办
        elif job == "weekly":
            self.send_letter("evo-lab", "[钟点] 演化周会",
                             "周六：请出周提案/回测/影子排期（P3 前处于备用待命，回执即可）。")

    # ---------------------------------------------------------- 心跳看门狗

    def watchdog(self):
        with self.lock:
            now = time.time()
            for name, entry in self.roster.items():
                if name == "beidou" or entry.get("role") == "infra":
                    continue
                expect = DEFAULT_ROLES.get(entry.get("role"), 3600)
                if is_trading_time():
                    expect = min(expect, 900)
                hb = self.heartbeats.get(name)
                if hb is None:
                    # P-20261011-01（keeper 提案·PM+风控双签·主人终批）：
                    # 查无此跳≠在岗——启动后满 expect×2 仍零心跳判失联。
                    # 纯告警路径：PM freeze 与一切降级动作零触碰（主路径不动）。
                    if now - self.started_at >= expect * 2 and name not in self.down:
                        self.down.add(name)
                        self.alert(f"零心跳 {int((now - self.started_at) // expect)} 周期："
                                   f"{name} 查无此跳判失联（role={entry.get('role')}）")
                    continue
                age = now - hb
                misses = int(age // expect)
                if misses >= MISS_LIMIT and name not in self.down:
                    self.down.add(name)
                    self.alert(f"心跳缺席 {misses} 次：{name} 判失联（role={entry.get('role')}）")
                    if entry.get("role") == "pm":
                        self.s1_admin("freeze", {"scope": "new_opens",
                                                 "reason": "PM 失联·代码规则冻结（§六）"})
                elif misses < MISS_LIMIT and name in self.down:
                    self.down.discard(name)      # 恢复（含零心跳岗首跳到达）
                    self.alert(f"{name} 心跳恢复")

    def beat(self, name):
        with self.lock:
            self.heartbeats[name] = time.time()
        return {"ok": True, "beats": len(self.heartbeats)}

    def status(self):
        with self.lock:
            return {"ok": True, "roster": list(self.roster),
                    "heartbeats": {k: datetime.fromtimestamp(
                        v, CN_TZ).isoformat() for k, v in self.heartbeats.items()},
                    "down": sorted(self.down),
                    "budget": self.budget,
                    "trading_time": is_trading_time(),
                    "ts": now_cn().isoformat()}


class Handler(BaseHTTPRequestHandler):
    sched: Scheduler = None

    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/status":
            return self._json(200, self.sched.status())
        if path == "/s1-proxy/status":     # 只读代理，方便一个口看全局
            return self._json(200, self.sched.s1_get("/status"))
        return self._json(404, {"ok": False, "error": "not_found"})

    def do_POST(self):
        path = self.path.split("?")[0]
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            return self._json(400, {"ok": False, "error": "bad_json"})
        if path.startswith("/heartbeat/"):
            name = path.rsplit("/", 1)[-1]
            return self._json(200, self.sched.beat(name))
        if path == "/letters":
            r = self.sched.send_letter(body.get("to"), body.get("subject", ""),
                                       body.get("body", ""),
                                       body.get("from", "unknown"))
            return self._json(200 if r.get("ok") else 502, r)
        if path == "/budget":
            self.sched.budget.update({k: body[k] for k in
                                      ("daily_cap", "spent") if k in body})
            self.sched.budget["throttle"] = \
                self.sched.budget["spent"] > self.sched.budget["daily_cap"]
            return self._json(200, {"ok": True, "budget": self.sched.budget})
        return self._json(404, {"ok": False, "error": "not_found"})


def main():
    port = int(os.environ.get("STOCKSIM_S2_PORT", "8891"))
    s = Scheduler()
    Handler.sched = s

    def loop():
        while True:
            try:
                s.tick()
            except Exception:
                pass
            time.sleep(60)

    threading.Thread(target=loop, daemon=True).start()
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"S2 调度器 :{port}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
