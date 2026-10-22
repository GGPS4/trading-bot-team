"""Pre-market scan (plain code, no AI). Runs every 15 minutes from 8:00 to 9:30 AM ET.

Finds stocks "gapping" before the open (trading well above or below yesterday's close),
usually because of overnight news or earnings. Gappers are the classic day-trading
candidates for the first hour. Two lists:
  watched  - gappers already on a team's watchlist
  outside  - gappers mentioned in market-wide news but not on any watchlist yet
             (managers can add them to their watchlist at the first look)
"""
from .common import STATE, log, now_et, read_json, settings, write_json


def _gap(s):
    return s["price"] / s["prev_close"] - 1 if s and s.get("prev_close") else None


def scan(broker, org):
    cfg = settings().get("premarket", {})
    min_gap = cfg.get("min_gap_pct", 0.02)
    owner = {s: tid for tid, t in org.items() for s in t["tickers"]}
    watched = sorted(owner)
    try:
        snaps = broker.snapshots(watched)
    except Exception as e:
        log(f"pre-market scan failed: {e}")
        return {}
    rows = []
    for s, v in snaps.items():
        g = _gap(v)
        if g is not None and abs(g) >= min_gap:
            rows.append({"symbol": s, "team": owner.get(s), "gap": round(g, 4), "price": v["price"],
                         "prev_close": v["prev_close"]})
    # gappers outside the watchlists, found through overnight market news
    outside = []
    try:
        news = broker.market_news(hours=14, limit=50)
        cand = sorted({sym for n in news for sym in (n.get("symbols") or []) if sym not in owner and sym.isalpha()})[:60]
        heads = {}
        for n in news:
            for sym in n.get("symbols") or []:
                heads.setdefault(sym, n.get("headline"))
        if cand:
            for s, v in broker.snapshots(cand).items():
                g = _gap(v)
                if g is not None and abs(g) >= max(min_gap, 0.03) and v["price"] >= settings().get("watchlist", {}).get("min_price", 5):
                    outside.append({"symbol": s, "gap": round(g, 4), "price": v["price"], "headline": heads.get(s)})
    except Exception as e:
        log(f"pre-market news scan failed: {e}")
    # attach the latest headline for watched gappers
    try:
        if rows:
            for n in broker.news([r["symbol"] for r in rows], hours=16, limit=50):
                for r in rows:
                    if r["symbol"] in (n.get("symbols") or []) and not r.get("headline"):
                        r["headline"] = n.get("headline")
    except Exception:
        pass
    rows.sort(key=lambda r: -abs(r["gap"]))
    outside.sort(key=lambda r: -abs(r["gap"]))
    out = {"time": now_et().isoformat(timespec="minutes"), "date": now_et().date().isoformat(),
           "watched": rows[:40], "outside": outside[:20]}
    write_json(STATE / "research" / "_premarket.json", out)
    log(f"Pre-market scan: {len(rows)} watched gappers, {len(outside)} outside the watchlists")
    return out


def latest():
    d = read_json(STATE / "research" / "_premarket.json", {})
    return d if d.get("date") == now_et().date().isoformat() else {}


def for_team(scan_data, tid):
    if not scan_data:
        return "no pre-market scan today"
    return {"scanned_at": scan_data.get("time"),
            "your_gappers": [r for r in scan_data.get("watched", []) if r.get("team") == tid],
            "gappers_not_on_any_watchlist": scan_data.get("outside", [])[:12],
            "note": "Gappers with real news often keep moving in the first hour. Let the first 5-15 minutes settle, "
                    "then trade breakouts of the opening range (gap up) or breakdowns (gap down, shorting). "
                    "Gaps with no news often fade back. Add strong outside gappers in your field to your watchlist."}
