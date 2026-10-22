"""Strategy scorecard (plain code, no AI).

Every buy is tagged with the setup it used (from a fixed list) and the experiment the
manager was testing. This adds up closed trades by setup and by experiment so managers
see what works for them and the President moves money toward what's working.
"""
from . import journal
from .common import STATE, now_et, write_json

SETUPS = ["opening range breakout", "VWAP bounce", "momentum", "news catalyst", "earnings gap",
          "pullback in uptrend", "mean reversion", "sector rotation", "other"]
MIN_TRADES = 5   # below this, results are too few to judge


def clean_setup(x):
    x = str(x or "").strip().lower()
    for s in SETUPS:
        if s.lower() == x or s.lower() in x:
            return s
    return "other" if x else "untagged"


def _stats(rows):
    if not rows:
        return None
    wins = [e for e in rows if e["pnl"] > 0]
    pcts = [e.get("pnl_pct") or 0 for e in rows]
    gross_win = sum(e["pnl"] for e in wins)
    gross_loss = -sum(e["pnl"] for e in rows if e["pnl"] < 0)
    return {
        "trades": len(rows),
        "win_rate": round(len(wins) / len(rows), 3),
        "avg_return": round(sum(pcts) / len(pcts), 4),
        "total_pnl": round(sum(e["pnl"] for e in rows), 2),
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else None,
        "enough_data": len(rows) >= MIN_TRADES,
    }


def _group(rows, key):
    groups = {}
    for e in rows:
        groups.setdefault(key(e), []).append(e)
    return {k: _stats(v) for k, v in sorted(groups.items(), key=lambda kv: -len(kv[1]))}


def build(days=30):
    """Scorecard over the last `days` days of closed trades. Saved for the dashboard."""
    cutoff = (now_et().date().toordinal() - days)
    closed = [e for e in journal.load() if e["status"] == "closed" and e.get("pnl") is not None
              and _ordinal(e) >= cutoff]
    teams = sorted({e["team"] for e in closed})
    card = {
        "updated": now_et().isoformat(timespec="minutes"), "days": days,
        "overall": _stats(closed),
        "by_setup": _group(closed, lambda e: clean_setup(e.get("setup"))),
        "by_team": {t: {"overall": _stats([e for e in closed if e["team"] == t]),
                        "by_setup": _group([e for e in closed if e["team"] == t], lambda e: clean_setup(e.get("setup"))),
                        "by_experiment": dict(list(_group([e for e in closed if e["team"] == t and e.get("experiment")],
                                                          lambda e: e["experiment"][:120]).items())[:8])}
                    for t in teams},
        "by_exit": _group(closed, lambda e: e.get("exit_reason") or "unknown"),
    }
    write_json(STATE / "scorecard.json", card)
    return card


def _ordinal(e):
    try:
        import datetime as dt
        return dt.date.fromisoformat((e.get("exit_time") or e["date"])[:10]).toordinal()
    except Exception:
        return 0


def for_manager(card, tid):
    """Short version for a manager's prompt."""
    t = (card or {}).get("by_team", {}).get(tid)
    if not t:
        return "No closed trades yet."
    short = lambda g: {k: f"{v['trades']} trades, {v['win_rate']:.0%} won, avg {v['avg_return'] * 100:+.2f}%, PF {v['profit_factor']}"  # noqa: E731
                       for k, v in (g or {}).items() if v}
    return {"overall": short({"all": t["overall"]}).get("all"), "by_setup": short(t["by_setup"]),
            "by_experiment": short(dict(list(t["by_experiment"].items())[:4])),
            "note": f"Under {MIN_TRADES} trades is too few to judge. Favor setups with PF (profit factor) above 1.2."}


def for_president(card):
    if not card or not card.get("overall"):
        return "No closed trades yet."
    return {"overall": card["overall"], "by_setup_all_teams": card["by_setup"],
            "by_team": {t: {"overall": v["overall"], "by_setup": v["by_setup"]} for t, v in card["by_team"].items()},
            "by_exit": card["by_exit"]}
