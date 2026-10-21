"""Intraday signals day traders use, built from today's 5-minute bars (plain code, no AI).

For each stock:
  vwap               volume-weighted average price today (big traders' "fair price" for the day)
  vs_vwap            how far the price is above (+) or below (-) VWAP
  opening_range      high/low of the first 30 minutes; breakout_up / breakdown flags
  momentum_15m/60m   change over the last 15 and 60 minutes
  rel_volume         today's volume so far vs a normal day at this time (2.0 = twice normal)
  trend_5m           "up" / "down" / "flat" from the last six 5-minute bars
"""
import datetime as dt

from .common import ET, log, now_et

OPEN = dt.time(9, 30)
SESSION_MIN = 390


def _bar_time(b):
    t = b.get("t")
    if isinstance(t, str):
        return dt.datetime.fromisoformat(t.replace("Z", "+00:00")).astimezone(ET)
    return t


def fetch(broker, symbols):
    start = now_et().replace(hour=9, minute=30, second=0, microsecond=0)
    try:
        return broker.intraday_bars(sorted(symbols), start.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    except Exception as e:
        log(f"intraday bars unavailable: {e}")
        return {}


def signals(bars, avg_volume=None, price_now=None):
    """bars: {symbol: [5-min bars]}; avg_volume: {symbol: average daily volume}."""
    avg_volume = avg_volume or {}
    now = now_et()
    elapsed = max(5, min(SESSION_MIN, (now.hour * 60 + now.minute) - (9 * 60 + 30)))
    out = {}
    for sym, rows in bars.items():
        rows = [r for r in rows if r.get("c")]
        if not rows:
            continue
        last = (price_now or {}).get(sym) or rows[-1]["c"]
        vol = sum(r.get("v") or 0 for r in rows)
        vwap = (sum((r.get("vw") or r["c"]) * (r.get("v") or 0) for r in rows) / vol) if vol else None
        first30 = [r for r in rows if _bar_time(r) and _bar_time(r).time() < dt.time(10, 0)]
        orh = max(r["h"] for r in first30) if first30 else None
        orl = min(r["l"] for r in first30) if first30 else None
        or_done = now.time() >= dt.time(10, 0)

        def back(n):
            return round(last / rows[-1 - n]["c"] - 1, 4) if len(rows) > n else None

        closes = [r["c"] for r in rows[-6:]]
        ups = sum(1 for a, b in zip(closes, closes[1:]) if b > a)
        trend = "up" if len(closes) >= 4 and ups >= len(closes) - 2 else \
                "down" if len(closes) >= 4 and ups <= 1 else "flat"
        avg = avg_volume.get(sym)
        out[sym] = {
            "vwap": round(vwap, 2) if vwap else None,
            "vs_vwap": round(last / vwap - 1, 4) if vwap else None,
            "opening_range": [round(orl, 2), round(orh, 2)] if orh else None,
            "breakout_up": bool(or_done and orh and last > orh),
            "breakdown": bool(or_done and orl and last < orl),
            "momentum_15m": back(3),
            "momentum_60m": back(12),
            "rel_volume": round(vol / (avg * elapsed / SESSION_MIN), 2) if avg and vol else None,
            "trend_5m": trend,
        }
    return out


def add_to_view(view, sig):
    """Merge signals into the live price view each manager sees."""
    for sym, v in view.items():
        if sym in sig:
            v.update(sig[sym])
    return view
