"""Talks to Alpaca (trading + market data + news) over its REST API."""
import datetime as dt
import os

import requests

from .common import now_et


class BrokerError(Exception):
    pass


class Alpaca:
    DATA = "https://data.alpaca.markets"

    def __init__(self, mode: str):
        if mode == "live":
            key, secret = os.environ.get("ALPACA_LIVE_KEY"), os.environ.get("ALPACA_LIVE_SECRET")
            self.base = "https://api.alpaca.markets"
            if not key or not secret:
                raise BrokerError("Live mode is on but ALPACA_LIVE_KEY / ALPACA_LIVE_SECRET secrets are missing. "
                                  "Refusing to trade.")
        else:
            key, secret = os.environ.get("ALPACA_PAPER_KEY"), os.environ.get("ALPACA_PAPER_SECRET")
            self.base = "https://paper-api.alpaca.markets"
            if not key or not secret:
                raise BrokerError("ALPACA_PAPER_KEY / ALPACA_PAPER_SECRET are not set (add them as GitHub secrets).")
        self.mode = mode
        self.h = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}

    def _req(self, method, url, **kw):
        r = requests.request(method, url, headers=self.h, timeout=30, **kw)
        if r.status_code >= 400:
            raise BrokerError(f"{method} {url.split('.markets')[-1]} -> {r.status_code}: {r.text[:300]}")
        return r.json() if r.text.strip() else {}

    # ---------- account ----------
    def clock(self):
        return self._req("GET", f"{self.base}/v2/clock")

    def is_trading_day(self, date: str) -> bool:
        cal = self._req("GET", f"{self.base}/v2/calendar", params={"start": date, "end": date})
        return any(d.get("date") == date for d in cal)

    def account(self):
        return self._req("GET", f"{self.base}/v2/account")

    def positions(self):
        return self._req("GET", f"{self.base}/v2/positions")

    # ---------- orders ----------
    def get_order(self, order_id):
        return self._req("GET", f"{self.base}/v2/orders/{order_id}", params={"nested": "true"})

    def open_orders(self, symbol=None):
        params = {"status": "open", "nested": "true", "limit": 500}
        if symbol:
            params["symbols"] = symbol
        return self._req("GET", f"{self.base}/v2/orders", params=params)

    def cancel_order(self, order_id):
        return self._req("DELETE", f"{self.base}/v2/orders/{order_id}")

    def close_position(self, symbol, qty=None):
        """Close all of a position (long or short), or just `qty` shares of it."""
        params = {"qty": f"{qty:g}"} if qty else None
        return self._req("DELETE", f"{self.base}/v2/positions/{symbol}", params=params)

    def market_order(self, symbol, side, client_id, qty=None, notional=None):
        """Entry order. side='buy' opens a long, side='sell' (on a stock we don't hold) opens a short.
        Fractional amounts (notional / fractional qty) only work for longs."""
        body = {"symbol": symbol, "side": side, "type": "market", "time_in_force": "day",
                "client_order_id": client_id}
        if notional:
            body["notional"] = f"{notional:.2f}"
        else:
            body["qty"] = f"{qty:g}"
        return self._req("POST", f"{self.base}/v2/orders", json=body)

    def stop_order(self, symbol, side, qty, stop_price, client_id=None):
        """Protective stop held at the broker (works even between the bots' checks).
        side='sell' protects a long, side='buy' protects a short. Whole shares only."""
        body = {"symbol": symbol, "side": side, "type": "stop", "qty": f"{int(qty)}",
                "stop_price": f"{stop_price:.2f}", "time_in_force": "gtc"}
        if client_id:
            body["client_order_id"] = client_id
        return self._req("POST", f"{self.base}/v2/orders", json=body)

    def replace_order(self, order_id, qty=None, stop_price=None):
        body = {}
        if qty:
            body["qty"] = f"{int(qty)}"
        if stop_price:
            body["stop_price"] = f"{stop_price:.2f}"
        return self._req("PATCH", f"{self.base}/v2/orders/{order_id}", json=body)

    def buy_bracket(self, symbol, qty: int, stop_price: float, take_profit_price: float, client_id: str):
        body = {
            "symbol": symbol, "qty": str(qty), "side": "buy", "type": "market",
            "time_in_force": "gtc", "order_class": "bracket",
            "take_profit": {"limit_price": f"{take_profit_price:.2f}"},
            "stop_loss": {"stop_price": f"{stop_price:.2f}"},
            "client_order_id": client_id,
        }
        return self._req("POST", f"{self.base}/v2/orders", json=body)

    def buy_notional(self, symbol, dollars: float, client_id: str):
        """Fractional-share buy. Alpaca can't attach a stop-loss to these,
        so the bot checks the stop/take-profit itself on every run."""
        body = {"symbol": symbol, "notional": f"{dollars:.2f}", "side": "buy",
                "type": "market", "time_in_force": "day", "client_order_id": client_id}
        return self._req("POST", f"{self.base}/v2/orders", json=body)

    # ---------- market data ----------
    def latest_prices(self, symbols):
        if not symbols:
            return {}
        data = self._req("GET", f"{self.DATA}/v2/stocks/trades/latest",
                         params={"symbols": ",".join(symbols), "feed": "iex"})
        return {s: t["p"] for s, t in data.get("trades", {}).items()}

    def snapshots(self, symbols):
        """Live price plus today's open/high/low and yesterday's close, for many stocks at once."""
        if not symbols:
            return {}
        data = self._req("GET", f"{self.DATA}/v2/stocks/snapshots",
                         params={"symbols": ",".join(symbols), "feed": "iex"})
        data = data.get("snapshots", data)
        out = {}
        today = now_et().date().isoformat()
        for s, v in data.items():
            if not v:
                continue
            price = (v.get("latestTrade") or {}).get("p") or (v.get("minuteBar") or {}).get("c")
            day, prev = v.get("dailyBar") or {}, v.get("prevDailyBar") or {}
            if not price:
                continue
            if str(day.get("t", ""))[:10] >= today:   # today's session has started
                out[s] = {"price": price, "open": day.get("o"), "high": day.get("h"), "low": day.get("l"),
                          "prev_close": prev.get("c")}
            else:                                       # before the open: "dailyBar" is yesterday's
                out[s] = {"price": price, "open": None, "high": None, "low": None, "prev_close": day.get("c")}
        return out

    def daily_bars(self, symbols, days=120):
        start = (dt.datetime.utcnow() - dt.timedelta(days=days)).strftime("%Y-%m-%dT00:00:00Z")
        bars, token = {}, None
        while True:
            params = {"symbols": ",".join(symbols), "timeframe": "1Day", "start": start,
                      "feed": "iex", "limit": 10000, "adjustment": "split"}
            if token:
                params["page_token"] = token
            data = self._req("GET", f"{self.DATA}/v2/stocks/bars", params=params)
            for s, b in (data.get("bars") or {}).items():
                bars.setdefault(s, []).extend(b)
            token = data.get("next_page_token")
            if not token:
                return bars

    def news(self, symbols, hours=24, limit=50):
        start = (dt.datetime.utcnow() - dt.timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
        data = self._req("GET", f"{self.DATA}/v1beta1/news",
                         params={"symbols": ",".join(symbols), "start": start, "limit": limit,
                                 "sort": "desc", "include_content": "false"})
        return data.get("news", [])

    def intraday_bars(self, symbols, start_iso, timeframe="5Min"):
        """Today's bars (default 5-minute) for many stocks: o/h/l/c, volume v and volume-weighted price vw."""
        bars, token = {}, None
        while symbols:
            params = {"symbols": ",".join(symbols), "timeframe": timeframe, "start": start_iso,
                      "feed": "iex", "limit": 10000}
            if token:
                params["page_token"] = token
            data = self._req("GET", f"{self.DATA}/v2/stocks/bars", params=params)
            for s, b in (data.get("bars") or {}).items():
                bars.setdefault(s, []).extend(b)
            token = data.get("next_page_token")
            if not token:
                break
        return bars

    # ---------- discovery ----------
    def asset(self, symbol):
        """Is this a real US stock we can trade? (tradable, active, fractionable...)"""
        return self._req("GET", f"{self.base}/v2/assets/{symbol}")

    def movers(self, top=20):
        """Today's biggest gainers/losers and most-traded stocks across the whole market."""
        out = {"gainers": [], "losers": [], "most_active": []}
        try:
            m = self._req("GET", f"{self.DATA}/v1beta1/screener/stocks/movers", params={"top": top})
            out["gainers"] = [{"symbol": x["symbol"], "change_pct": round(x.get("percent_change", 0), 2),
                               "price": x.get("price")} for x in m.get("gainers", [])]
            out["losers"] = [{"symbol": x["symbol"], "change_pct": round(x.get("percent_change", 0), 2),
                              "price": x.get("price")} for x in m.get("losers", [])]
        except BrokerError:
            pass
        try:
            a = self._req("GET", f"{self.DATA}/v1beta1/screener/stocks/most-actives",
                          params={"by": "volume", "top": top})
            out["most_active"] = [x["symbol"] for x in a.get("most_actives", [])]
        except BrokerError:
            pass
        return out

    def market_news(self, hours=6, limit=50):
        """Market-wide headlines (not limited to a team's list), used to discover new names."""
        start = (dt.datetime.utcnow() - dt.timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
        data = self._req("GET", f"{self.DATA}/v1beta1/news",
                         params={"start": start, "limit": limit, "sort": "desc", "include_content": "false"})
        return data.get("news", [])
