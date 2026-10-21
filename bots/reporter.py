"""The Reporter.

Collects everything that happened across all teams, writes the daily
report for the President, keeps the report history, and feeds the dashboard.
"""
from . import journal
from .common import DOCS_DATA, STATE, now_et, read_json, settings, today, write_json

HISTORY = STATE / "history.json"

REPORT_FORMAT = """Answer in JSON:
{
  "headline": "one line, e.g. 'Up 0.8% while the market rose 0.3%; defense led'",
  "summary": "4-6 sentences for the President covering results, what drove them, and what changed",
  "best_trade": "one sentence with the reasoning behind it",
  "worst_trade": "one sentence with the reasoning behind it",
  "rule_issues": ["risk rules that blocked orders or were nearly broken, and why it matters"],
  "concerns": ["anything unusual the President should look at"],
  "patterns": "what the recent report history shows over weeks, not just today"
}"""


def _f(x, d=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return d


def with_baseline(acct):
    """Alpaca's 'last_equity' (yesterday's close) is 0 on a brand-new account.
    Fall back to our own history so today's change and the daily loss limit work."""
    acct = dict(acct)
    if _f(acct.get("last_equity")) <= 0:
        prior = [h for h in read_json(HISTORY, []) if h["date"] < today()]
        acct["last_equity"] = prior[-1]["equity"] if prior else acct["equity"]
    return acct


def update_history(equity, bench_price):
    hist = read_json(HISTORY, [])
    row = {"date": today(), "equity": round(equity, 2), "benchmark": bench_price}
    if hist and hist[-1]["date"] == row["date"]:
        hist[-1] = row
    else:
        hist.append(row)
    write_json(HISTORY, hist)
    return hist


def performance(hist):
    if not hist:
        return {}
    start, last = hist[0], hist[-1]
    peak, max_dd = 0, 0
    for h in hist:
        peak = max(peak, h["equity"])
        max_dd = max(max_dd, 1 - h["equity"] / peak if peak else 0)
    bench_ret = (last["benchmark"] / start["benchmark"] - 1) if start.get("benchmark") and last.get("benchmark") else None
    return {
        "start_date": start["date"], "trading_days": len(hist),
        "return_since_start": round(last["equity"] / start["equity"] - 1, 4) if start["equity"] else None,
        "benchmark_since_start": round(bench_ret, 4) if bench_ret is not None else None,
        "max_drawdown": round(max_dd, 4),
    }


def go_live_checklist(perf):
    rules = settings().get("go_live_checklist", {})
    days_ok = perf.get("trading_days", 0) >= rules.get("min_trading_days", 60)
    beat = (perf.get("return_since_start") is not None and perf.get("benchmark_since_start") is not None
            and perf["return_since_start"] > perf["benchmark_since_start"])
    dd_ok = perf.get("max_drawdown", 1) <= rules.get("max_drawdown_pct", 0.15)
    items = [
        {"rule": f"At least {rules.get('min_trading_days', 60)} trading days on paper",
         "ok": days_ok, "value": f"{perf.get('trading_days', 0)} days"},
        {"rule": f"Beating {settings().get('benchmark', 'SPY')} since the start", "ok": beat or not rules.get("must_beat_benchmark", True),
         "value": f"{(perf.get('return_since_start') or 0):+.1%} vs {(perf.get('benchmark_since_start') or 0):+.1%}"},
        {"rule": f"Worst drop under {rules.get('max_drawdown_pct', 0.15):.0%}", "ok": dd_ok,
         "value": f"{perf.get('max_drawdown', 0):.1%}"},
    ]
    return {"items": items, "all_ok": all(i["ok"] for i in items)}


def snapshot(broker, org, owner_of, budgets, mode):
    """Numbers only (no AI). Used during the day and as the base of the evening report."""
    acct = broker.account()
    positions = broker.positions()
    entries = journal.load()
    date = today()
    acct = with_baseline(acct)
    equity, last_equity = _f(acct["equity"]), _f(acct["last_equity"])
    bench = settings().get("benchmark", "SPY")
    try:
        bench_price = broker.latest_prices([bench]).get(bench)
    except Exception:
        bench_price = None
    open_by_symbol = {e["symbol"]: e for e in entries if e["status"] in ("open", "submitted")}

    pos_rows = []
    for p in positions:
        e = open_by_symbol.get(p["symbol"], {})
        pos_rows.append({
            "symbol": p["symbol"], "team": owner_of.get(p["symbol"], "unassigned"),
            "qty": _f(p["qty"]), "entry": round(_f(p["avg_entry_price"]), 2),
            "price": round(_f(p["current_price"]), 2), "value": round(_f(p["market_value"]), 2),
            "pnl": round(_f(p["unrealized_pl"]), 2), "pnl_pct": round(_f(p["unrealized_plpc"]), 4),
            "stop": e.get("stop_price"), "target": e.get("take_profit_price"), "reason": e.get("reason", ""),
        })

    todays = journal.trades_on(date, entries)
    blocked = journal.blocked_on(date)
    teams = {}
    for tid, t in org.items():
        mine = [r for r in pos_rows if r["team"] == tid]
        teams[tid] = {
            "name": t["name"], "kind": t["kind"], "tickers": t["tickers"],
            "budget": round(budgets.get(tid, 0), 2),
            "invested": round(sum(r["value"] for r in mine), 2),
            "unrealized_pnl": round(sum(r["pnl"] for r in mine), 2),
            "realized_today": round(sum(e.get("pnl") or 0 for e in todays
                                        if e["team"] == tid and e["status"] == "closed"), 2),
            "trades_today": sum(1 for e in todays if e["team"] == tid and e["date"] == date),
            "blocked_today": sum(1 for b in blocked if b["team"] == tid),
            "all_time": journal.team_stats(tid, entries),
        }
    hist = update_history(equity, bench_price)
    perf = performance(hist)
    return {
        "updated": now_et().isoformat(timespec="minutes"), "date": date, "mode": mode,
        "paused": bool(settings().get("paused")),
        "account": {"equity": round(equity, 2), "cash": round(_f(acct["cash"]), 2),
                    "day_change": round(equity - last_equity, 2),
                    "day_change_pct": round(equity / last_equity - 1, 4) if last_equity else 0},
        "benchmark": {"symbol": bench, "price": bench_price},
        "performance": perf, "go_live": go_live_checklist(perf),
        "teams": teams, "positions": pos_rows,
        "trades_today": [{k: e.get(k) for k in ("time", "team", "symbol", "qty", "est_price", "entry_price",
                                                  "exit_price", "exit_reason", "pnl", "pnl_pct", "status",
                                                  "conviction", "reason", "stop_price", "take_profit_price")}
                         for e in todays],
        "blocked_today": blocked,
        "recent_trades": [{k: e.get(k) for k in ("date", "team", "symbol", "entry_price", "exit_price",
                                                   "exit_reason", "pnl", "pnl_pct", "reason")}
                          for e in sorted([e for e in entries if e["status"] == "closed"],
                                          key=lambda e: e.get("exit_time") or "", reverse=True)[:25]],
    }


def write_dashboard(snap, extra=None):
    """Merge new numbers with the last AI-written parts and save for the website."""
    prev = read_json(DOCS_DATA / "dashboard.json", {})
    data = dict(snap)
    for k in ("report", "president", "executives", "reviews", "lessons", "research"):
        data[k] = (extra or {}).get(k, prev.get(k))
    data["market_watch"] = read_json(STATE / "research" / "_market_watch.json", {})
    data["overnight"] = {tid: (read_json(STATE / "research" / f"{tid}.json", {}).get("overnight") or [])[:8]
                         for tid in snap["teams"]}
    data["team_details"] = team_details(snap["teams"])
    data["strategy_log"] = read_json(STATE / "strategy_log.json", [])[-90:]
    data["reports_index"] = read_json(STATE / "reports" / "index.json", [])[-60:]
    data["blocked_recent"] = read_json(journal.BLOCKED, [])[-150:]
    write_json(DOCS_DATA / "dashboard.json", data)
    write_json(DOCS_DATA / "history.json", read_json(HISTORY, []))


def team_details(teams):
    """Everything the dashboard's team pages show: full trade history, every look the
    manager took, lessons, strategy ideas, past reviews and the latest research."""
    entries = journal.load()
    blocked = read_json(journal.BLOCKED, [])
    report_files = sorted((STATE / "reports").glob("20*.json"))[-30:]
    reports = [read_json(f, {}) for f in report_files]
    out = {}
    for tid in teams:
        mine = [e for e in entries if e["team"] == tid]
        closed = sorted([e for e in mine if e["status"] == "closed" and e.get("pnl") is not None],
                        key=lambda e: e.get("exit_time") or "")
        by_day, cum = {}, 0.0
        for e in closed:
            d = (e.get("exit_time") or e["date"])[:10]
            by_day[d] = by_day.get(d, 0) + e["pnl"]
        pnl_days = []
        for d in sorted(by_day):
            cum += by_day[d]
            pnl_days.append({"date": d, "realized": round(by_day[d], 2), "cum": round(cum, 2)})
        research = read_json(STATE / "research" / f"{tid}.json", {})
        out[tid] = {
            "trades": [{k: e.get(k) for k in ("id", "date", "time", "symbol", "kind", "qty", "dollars", "est_price",
                                               "entry_price", "exit_price", "exit_time", "exit_reason", "exit_note",
                                               "pnl", "pnl_pct", "status", "conviction", "reason", "stop_price",
                                               "take_profit_price")} for e in mine[-250:]],
            "blocked": [b for b in blocked if b["team"] == tid][-60:],
            "plans": read_json(STATE / "plans" / f"{tid}_log.json", [])[-80:],
            "lessons": read_json(STATE / "lessons" / f"{tid}.json", [])[-60:],
            "ideas": read_json(STATE / "manager_notes" / f"{tid}.json", {}),
            "reviews": [{"date": r.get("numbers", {}).get("date"), **(r.get("reviews", {}).get(tid) or {})}
                        for r in reports if (r.get("reviews") or {}).get(tid)][-20:],
            "pnl_days": pnl_days,
            "research": {k: research.get(k) for k in ("time", "news", "social", "overnight", "technicals")},
        }
    return out


def daily_report(llm, snap, reviews, exec_summaries, research_overview):
    past = read_json(STATE / "reports" / "index.json", [])[-10:]
    system = ("You are the Reporter on an AI stock trading team. You sit right below the President. You collect "
              "everything that happened across all teams and write a clear, honest, detailed end-of-day report so "
              "the President can decide what to do next. Use the numbers you are given; never invent any.")
    prompt = f"""Today's numbers:
account: {snap['account']}
performance since start: {snap['performance']}
benchmark: {snap['benchmark']}
teams: {snap['teams']}
open positions: {snap['positions']}
today's trades: {snap['trades_today']}
orders blocked by risk rules: {snap['blocked_today']}

Manager reviews: {reviews}
Executive summaries: {exec_summaries}
Research overview: {research_overview}

Previous report headlines (oldest first): {past}

{REPORT_FORMAT}"""
    rep = llm.ask_json("fast", "reporter", system, prompt)
    rep["date"] = snap["date"]
    write_json(STATE / "reports" / f"{snap['date']}.json", {"report": rep, "numbers": snap,
                                                            "reviews": reviews, "executives": exec_summaries})
    idx = [p for p in read_json(STATE / "reports" / "index.json", []) if p["date"] != snap["date"]]
    idx.append({"date": snap["date"], "headline": rep.get("headline"),
                "day_change_pct": snap["account"]["day_change_pct"], "equity": snap["account"]["equity"]})
    write_json(STATE / "reports" / "index.json", idx)
    return rep
