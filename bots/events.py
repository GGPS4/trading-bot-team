"""Event calendar: upcoming earnings for watched stocks and big US economic releases.

Sources (free, no keys):
  earnings  - Nasdaq's public earnings calendar (api.nasdaq.com)
  economy   - Forex Factory's weekly calendar feed (CPI, jobs report, Fed decisions, ...)
Both are cached in state/cache so each is fetched only a few times a day.
If a source is down, the bots simply trade without it.
"""
import datetime as dt
import os
import time

import requests

from .common import ET, STATE, log, now_et, read_json, settings, write_json

CACHE = STATE / "cache"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/126.0 Safari/537.36",
      "Accept": "application/json, text/plain, */*", "Origin": "https://www.nasdaq.com",
      "Referer": "https://www.nasdaq.com/"}
TIME_TEXT = {"time-pre-market": "before the open", "time-after-hours": "after the close"}


def _next_weekdays(n):
    d, out = now_et().date(), []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += dt.timedelta(days=1)
    return out


def _earnings_on(day):
    """All companies reporting on one day (cached; today's list refreshed every 6 hours)."""
    if os.environ.get("BOT_FAKE") == "1":
        return [{"symbol": "NVDA", "when": "after the close", "eps_forecast": "$1.10", "last_year_eps": "$0.81"},
                {"symbol": "LMT", "when": "before the open", "eps_forecast": "$7.49", "last_year_eps": "$6.95"}] \
            if day == now_et().date() else []
    f = CACHE / f"earnings_{day.isoformat()}.json"
    cached = read_json(f, None)
    max_age = 6 * 3600 if day == now_et().date() else 24 * 3600
    if cached is not None and time.time() - cached.get("fetched", 0) < max_age:
        return cached["rows"]
    try:
        r = requests.get("https://api.nasdaq.com/api/calendar/earnings", params={"date": day.isoformat()},
                         headers=UA, timeout=15)
        r.raise_for_status()
        rows = ((r.json().get("data") or {}).get("rows")) or []
        rows = [{"symbol": x.get("symbol"), "when": TIME_TEXT.get(x.get("time"), "time not announced"),
                 "eps_forecast": x.get("epsForecast") or None, "last_year_eps": x.get("lastYearEPS")}
                for x in rows if x.get("symbol")]
        write_json(f, {"fetched": time.time(), "rows": rows})
        return rows
    except Exception as e:
        log(f"earnings calendar unavailable for {day}: {e}")
        return (cached or {}).get("rows", [])


def earnings(symbols, days=5):
    """Watched stocks reporting earnings over the next `days` trading days."""
    want, out = set(symbols), []
    for day in _next_weekdays(days):
        for row in _earnings_on(day):
            if row["symbol"] in want:
                out.append({"date": day.isoformat(), **row})
    return out


def economy():
    """Medium/high-impact US economic events this week and next."""
    if os.environ.get("BOT_FAKE") == "1":
        t = now_et().replace(hour=10, minute=0, second=0, microsecond=0)
        return [{"time": t.isoformat(), "event": "CPI m/m (fake)", "impact": "High", "forecast": "0.3%", "previous": "0.2%"}]
    f = CACHE / "economic_calendar.json"
    cached = read_json(f, None)
    if cached is not None and time.time() - cached.get("fetched", 0) < 6 * 3600:
        return cached["events"]
    events = []
    for which in ("thisweek", "nextweek"):
        try:
            r = requests.get(f"https://nfs.faireconomy.media/ff_calendar_{which}.json", headers=UA, timeout=15)
            r.raise_for_status()
            for e in r.json():
                if e.get("country") == "USD" and e.get("impact") in ("High", "Medium"):
                    events.append({"time": e.get("date"), "event": e.get("title"), "impact": e.get("impact"),
                                   "forecast": e.get("forecast") or None, "previous": e.get("previous") or None})
        except Exception as e:
            log(f"economic calendar ({which}) unavailable: {e}")
    if events or cached is None:
        write_json(f, {"fetched": time.time(), "events": events})
    else:
        events = cached["events"]
    return sorted({(e["time"], e["event"]): e for e in events}.values(), key=lambda e: e["time"] or "")


def upcoming_economy(hours=48):
    now = now_et()
    out = []
    for e in economy():
        try:
            t = dt.datetime.fromisoformat(e["time"]).astimezone(ET)
        except Exception:
            continue
        if now - dt.timedelta(hours=1) <= t <= now + dt.timedelta(hours=hours):
            out.append({**e, "time": t.strftime("%a %b %d %I:%M %p ET")})
    return out


def buy_blackouts():
    """Windows today when new buys are paused around high-impact releases during market hours
    (e.g. Fed decisions, CPI at the open). Returns [(start, end, label)] in ET."""
    cfg = settings().get("events", {})
    before, after = cfg.get("pause_buys_minutes_before", 10), cfg.get("pause_buys_minutes_after", 5)
    today, out = now_et().date(), []
    for e in economy():
        if e.get("impact") != "High":
            continue
        try:
            t = dt.datetime.fromisoformat(e["time"]).astimezone(ET)
        except Exception:
            continue
        if t.date() == today and dt.time(9, 20) <= t.time() <= dt.time(16, 0):
            out.append((t - dt.timedelta(minutes=before), t + dt.timedelta(minutes=after), e["event"]))
    return out


def snapshot(org):
    """Everything the managers and dashboard need, built once per check."""
    syms = [s for t in org.values() for s in t["tickers"]]
    ern = earnings(syms)
    owner = {s: tid for tid, t in org.items() for s in t["tickers"]}
    data = {"time": now_et().isoformat(timespec="minutes"),
            "earnings": [{**e, "team": owner.get(e["symbol"])} for e in ern],
            "economy": upcoming_economy(hours=7 * 24)}
    write_json(STATE / "research" / "_calendar.json", data)
    return data


def for_team(cal, tid):
    if not cal:
        return "not available"
    today = now_et().date().isoformat()
    mine = [e for e in cal.get("earnings", []) if e.get("team") == tid]
    short = lambda e: {"symbol": e["symbol"], "date": e["date"], "when": e.get("when")}  # noqa: E731
    return {
        "earnings_today": [short(e) for e in mine if e["date"] == today],
        "earnings_coming": [short(e) for e in mine if e["date"] > today][:8],
        "economy_next_48h": [{"time": e["time"], "event": e["event"], "impact": e["impact"]} for e in upcoming_economy(48)][:8],
        "note": "Stocks that reported before the open often gap on heavy volume. New buys pause around high-impact releases.",
    }
