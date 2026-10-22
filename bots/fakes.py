"""Fake Alpaca + fake Gemini for testing without keys or real orders.

    BOT_FAKE=1 python run.py morning --force
"""
import ast
import datetime as dt
import math
import random
import re
import uuid
import zlib

from .common import STATE, now_et, read_json, write_json

FILE = STATE / "fake_broker.json"


def _base(sym):
    return 40 + zlib.crc32(sym.encode()) % 400


class FakeBroker:
    def __init__(self):
        self.s = read_json(FILE) or {"cash": 10000.0, "positions": {}, "orders": {}, "last_equity": 10000.0,
                                     "equity_date": None}

    def _save(self):
        write_json(FILE, self.s)

    def price(self, sym, when=None):
        when = when or now_et()
        day = when.date().toordinal()
        trend = math.sin(day / 7 + zlib.crc32(sym.encode()) % 10) * 0.08
        rr = random.Random(f"{sym}{when:%Y%m%d}")
        steps = max(0, min(26, ((when.hour * 60 + when.minute) - (9 * 60 + 30)) // 15))
        walk = 1 + rr.gauss(0, 0.012)          # overnight gap
        drift = rr.gauss(0, 0.002)
        for _ in range(steps):                 # 15-minute random walk during the day
            walk *= 1 + drift + rr.gauss(0, 0.006)
        return round(_base(sym) * (1 + trend) * walk, 2)

    def _process(self):
        """Fill resting stop orders (and old bracket legs) when the price crosses them."""
        for o in self.s["orders"].values():
            if o.get("type") == "stop" and o["status"] == "new":
                p = self.price(o["symbol"])
                hit = (o["side"] == "sell" and p <= o["stop_price"]) or (o["side"] == "buy" and p >= o["stop_price"])
                if hit and o["symbol"] in self.s["positions"]:
                    self._fill(o["symbol"], o["side"], o["qty"], p)
                    o.update(status="filled", filled_avg_price=str(p), filled_qty=str(o["qty"]),
                             filled_at=now_et().isoformat())
        self._save()

    def _fill(self, sym, side, qty, p):
        pos = self.s["positions"].get(sym, {"qty": 0, "avg": p})
        signed = qty if side == "buy" else -qty
        new = pos["qty"] + signed
        if pos["qty"] == 0 or (pos["qty"] > 0) == (signed > 0):  # opening / adding
            pos["avg"] = (pos["avg"] * abs(pos["qty"]) + p * qty) / (abs(pos["qty"]) + qty)
        pos["qty"] = round(new, 6)
        self.s["cash"] -= signed * p
        if abs(pos["qty"]) < 1e-9:
            self.s["positions"].pop(sym, None)
        else:
            self.s["positions"][sym] = pos

    # ----- account -----
    def clock(self):
        n = now_et()
        return {"is_open": n.weekday() < 5 and dt.time(9, 30) <= n.time() < dt.time(16, 0),
                "next_open": (n.replace(hour=9, minute=30, second=0) + dt.timedelta(days=0 if n.time() < dt.time(9, 30) else 1)).isoformat(),
                "next_close": (n.replace(hour=16, minute=0, second=0) + dt.timedelta(days=0 if n.time() < dt.time(16, 0) else 1)).isoformat()}

    def is_trading_day(self, date):
        return dt.date.fromisoformat(date).weekday() < 5

    def account(self):
        self._process()
        eq = self.s["cash"] + sum(v["qty"] * self.price(k) for k, v in self.s["positions"].items())
        if self.s["equity_date"] != now_et().date().isoformat():
            self.s["last_equity"] = self.s.get("prev_close_equity", eq)
            self.s["equity_date"] = now_et().date().isoformat()
        if now_et().hour >= 16:
            self.s["prev_close_equity"] = eq
        self._save()
        return {"equity": str(eq), "cash": str(self.s["cash"]), "last_equity": str(self.s["last_equity"])}

    def positions(self):
        self._process()
        out = []
        for sym, v in self.s["positions"].items():
            p = self.price(sym)
            pl = (p - v["avg"]) * v["qty"]
            out.append({"symbol": sym, "qty": str(v["qty"]), "avg_entry_price": str(v["avg"]),
                        "current_price": str(p), "market_value": str(v["qty"] * p),
                        "unrealized_pl": str(pl),
                        "unrealized_plpc": str((p / v["avg"] - 1) * (1 if v["qty"] > 0 else -1))})
        return out

    # ----- orders -----
    def _order(self, sym, side, qty, typ="market", **extra):
        oid = uuid.uuid4().hex
        o = {"id": oid, "symbol": sym, "side": side, "qty": qty, "type": typ, "status": "new", **extra}
        if typ == "market":
            p = self.price(sym)
            self._fill(sym, side, qty, p)
            o.update(status="filled", filled_avg_price=str(p), filled_qty=str(qty), filled_at=now_et().isoformat())
        self.s["orders"][oid] = o
        self._save()
        return o

    def market_order(self, symbol, side, client_id, qty=None, notional=None):
        qty = qty if qty else round(notional / self.price(symbol), 6)
        return self._order(symbol, side, qty)

    def stop_order(self, symbol, side, qty, stop_price, client_id=None):
        held = abs(self.s["positions"].get(symbol, {}).get("qty", 0))
        reserved = sum(o["qty"] for o in self.s["orders"].values()
                       if o["symbol"] == symbol and o.get("type") == "stop" and o["status"] == "new")
        if qty > held - reserved + 1e-9:
            raise Exception("insufficient qty available for order")
        p = self.price(symbol)
        if (side == "sell" and stop_price >= p) or (side == "buy" and stop_price <= p):
            raise Exception("stop price must be on the far side of the market price")
        return self._order(symbol, side, qty, "stop", stop_price=stop_price)

    def replace_order(self, order_id, qty=None, stop_price=None):
        o = self.s["orders"][order_id]
        if o["status"] != "new":
            raise Exception("order is not open")
        o["status"] = "replaced"
        return self._order(o["symbol"], o["side"], qty or o["qty"], "stop", stop_price=stop_price or o["stop_price"])

    def buy_bracket(self, symbol, qty, stop_price, take_profit_price, client_id):  # old API, unused
        return self._order(symbol, "buy", qty)

    def buy_notional(self, symbol, dollars, client_id):  # old API, unused
        return self.market_order(symbol, "buy", client_id, notional=dollars)

    def get_order(self, oid):
        self._process()
        return self.s["orders"][oid]

    def open_orders(self, symbol=None):
        return [o for o in self.s["orders"].values()
                if o["status"] == "new" and (symbol is None or o["symbol"] == symbol)]

    def cancel_order(self, oid):
        if oid in self.s["orders"] and self.s["orders"][oid]["status"] == "new":
            self.s["orders"][oid]["status"] = "canceled"
        self._save()

    def close_position(self, symbol, qty=None):
        pos = self.s["positions"].get(symbol)
        if not pos:
            raise Exception("position not found")
        held = abs(pos["qty"])
        reserved = sum(o["qty"] for o in self.s["orders"].values()
                       if o["symbol"] == symbol and o.get("type") == "stop" and o["status"] == "new")
        q = qty or held
        if q > held - reserved + 1e-9:
            raise Exception("insufficient qty available for order")
        return self._order(symbol, "sell" if pos["qty"] > 0 else "buy", q)

    # ----- data -----
    def latest_prices(self, symbols):
        return {s: self.price(s) for s in symbols}

    def snapshots(self, symbols):
        n = now_et()
        return {s: {"price": self.price(s), "open": self.price(s, n.replace(hour=9)),
                    "high": None, "low": None, "prev_close": self.price(s, n - dt.timedelta(days=1))}
                for s in symbols}

    def daily_bars(self, symbols, days=120):
        out = {}
        for s in symbols:
            rows = []
            for i in range(days, 0, -1):
                d = now_et() - dt.timedelta(days=i)
                if d.weekday() < 5:
                    rows.append({"t": d.date().isoformat(), "c": self.price(s, d.replace(hour=15)),
                                 "v": 1_000_000 + zlib.crc32(s.encode()) % 500_000})
            out[s] = rows
        return out

    def intraday_bars(self, symbols, start_iso, timeframe="5Min"):
        n = now_et()
        out = {}
        for s in symbols:
            rows, t = [], n.replace(hour=9, minute=30, second=0, microsecond=0)
            rr = random.Random(f"{s}{n:%Y%m%d}")
            p = self.price(s, t)
            while t <= n:
                p = round(p * (1 + rr.gauss(0.0003, 0.003)), 2)
                rows.append({"t": t.isoformat(), "o": p, "h": round(p * 1.002, 2), "l": round(p * 0.998, 2), "c": p,
                             "v": rr.randint(5_000, 40_000), "vw": p})
                t += dt.timedelta(minutes=5)
            out[s] = rows
        return out

    def asset(self, symbol):
        if symbol.startswith("ZZ"):
            raise Exception("404 asset not found")
        return {"symbol": symbol, "tradable": True, "status": "active", "class": "us_equity",
                "shortable": True, "easy_to_borrow": zlib.crc32(symbol.encode()) % 5 != 0}

    def movers(self, top=20):
        return {"gainers": [{"symbol": "SMCI", "change_pct": 9.1, "price": 40.0}, {"symbol": "PENY", "change_pct": 80.0, "price": 1.2}],
                "losers": [{"symbol": "INTC", "change_pct": -6.0, "price": 30.0}], "most_active": ["NVDA", "TSLA", "INTC"]}

    def market_news(self, hours=6, limit=50):
        return [{"symbols": ["SMCI"], "headline": "SMCI wins AI server deal"}]

    def news(self, symbols, hours=24, limit=50):
        r = random.Random(now_et().strftime("%Y%m%d%H"))
        picks = r.sample(symbols, min(3, len(symbols)))
        return [{"id": f"{s}-{now_et():%Y%m%d%H}", "symbols": [s], "headline": f"{s} announces new product line", "summary": "Test article.",
                 "source": "fake", "created_at": now_et().isoformat()} for s in picks]


class FakeLLM:
    """Returns believable answers, plus a few bad ideas so the risk rules get tested."""
    calls = 0

    def ask_json(self, tier, role, system, prompt):
        self.calls += 1
        r = random.Random(f"{role}{now_et():%Y%m%d%H}")
        companies = re.search(r"(?:Companies \(only these\)|Team companies(?: \([^)]*\))?): (.*)", prompt)
        tickers = [t.strip() for t in companies.group(1).split(",")] if companies else ["AAPL"]
        if role.startswith("political"):
            return {"findings": [{"source": "Truth Social", "post": "Plans to order more fighter jets",
                                  "market_wide": False, "affected": ["LMT"], "sentiment": 0.6, "importance": 4,
                                  "why": "More defense spending"}], "overall": "Defense-friendly (fake)."}
        if "researcher" in role:
            picks = r.sample(tickers, min(2, len(tickers)))
            return {"findings": [{"symbol": s, "sentiment": round(r.uniform(-1, 1), 2), "importance": r.randint(1, 5),
                                  "unconfirmed_rumor": False, "summary": f"Fake finding on {s}", "source": "fake"}
                                 for s in picks], "urgent": [], "overall": "Mixed mood (fake)."}
        if role.endswith("manager review"):
            return {"what_went_well": "Followed the rules.", "what_went_wrong": "Bought one stock into weakness.",
                    "lessons": [f"Fake lesson {now_et():%m%d}"], "strategy_ideas": ["Test buying only above the 20-day average"],
                    "watchlist_changes": {"add": [{"symbol": "INTC", "reason": "earnings next week (fake)"}]} if role.startswith("tech") else {}}
        if role.endswith("manager"):
            held = re.findall(r"['\"]symbol['\"]: ?['\"]([A-Z]+)['\"]", prompt.split("Current positions held by your team:")[1].split("News researcher")[0])
            buys = r.sample(tickers, min(3, len(tickers)))
            d = [{"symbol": s, "action": r.choice(["BUY", "BUY", "SHORT"]), "conviction": r.choice([2, 3, 4, 5]), "stop_loss_pct": 0.04,
                  "take_profit_pct": 0.08, "setup": r.choice(["momentum", "VWAP bounce", "opening range breakout", "news catalyst"]),
                  "reason": f"Fake reason for {s}"} for s in buys]
            d.append({"symbol": "ZZZZ", "action": "BUY", "conviction": 5, "reason": "not on our list (should be blocked)"})
            if held:
                d.append({"symbol": held[0], "action": "SELL", "conviction": 4, "reason": "Fake sell"})
            ideas = [{"symbol": s, "conviction": 3, "stop_loss_pct": 0.015, "take_profit_pct": 0.03, "setup": "momentum",
                      "direction": r.choice(["long", "short"]),
                      "reason": f"Backup idea {s}"} for s in tickers if s not in buys][:5]
            wl = {"add": [{"symbol": "SMCI", "reason": "AI server deal (fake)"}, {"symbol": "ZZTOP", "reason": "fake bad"},
                          {"symbol": "LMT", "reason": "owned by another team"}],
                  "remove": [{"symbol": tickers[-1], "reason": "quiet (fake)"}]} if "tech" in role else {}
            return {"market_view": "Fake view.", "decisions": d, "ranked_ideas": ideas, "experiment": "fake",
                    "watchlist_changes": wl}
        if "Executive" in role:
            return {"summary": "Teams followed orders (fake).", "team_notes": {}, "escalations": [], "coordination": ""}
        if role == "reporter":
            return {"headline": "Fake day: small moves", "summary": "Fake summary.", "best_trade": "-", "worst_trade": "-",
                    "rule_issues": [], "concerns": [], "patterns": "-"}
        if role == "president":
            ids = ast.literal_eval(re.search(r"Team ids: (\[.*\])", prompt).group(1))
            return {"strategy_notes": "Keep it steady (fake).", "team_focus": {i: "Focus on strong trends." for i in ids},
                    "allocations": {i: r.random() for i in ids},
                    "risk_overrides": {"max_position_pct": 0.9, "min_conviction_to_buy": 4},  # 0.9 must be ignored
                    "go_live_assessment": {"ready": False, "reason": "Too early."}, "decision_log": "Fake decision."}
        return {"ok": True}
