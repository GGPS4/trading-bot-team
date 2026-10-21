"""Trade journal: every trade, why it was made, and how it ended.

This is the team's memory. Managers learn from it, the Reporter reports
from it, and the dashboard shows it.
"""
import uuid

from .common import STATE, log, now_et, read_json, settings, today, write_json

TRADES = STATE / "journal" / "trades.json"
BLOCKED = STATE / "journal" / "blocked.json"


def load():
    return read_json(TRADES, [])


def save(entries):
    write_json(TRADES, entries)


def new_id(team, symbol):
    return f"{team}-{symbol}-{now_et():%Y%m%d%H%M}-{uuid.uuid4().hex[:4]}"


def add(entry):
    entries = load()
    entries.append(entry)
    save(entries)


def add_blocked(team, symbol, action, reason):
    rows = read_json(BLOCKED, [])
    rows.append({"date": today(), "time": now_et().strftime("%H:%M"), "team": team,
                 "symbol": symbol, "action": action, "reason": reason})
    write_json(BLOCKED, rows[-2000:])


def blocked_on(date):
    return [b for b in read_json(BLOCKED, []) if b["date"] == date]


def open_entries(entries=None):
    return [e for e in (entries or load()) if e["status"] in ("submitted", "open")]


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _close(e, price, reason, when):
    e["status"] = "closed"
    e["exit_price"] = price
    e["exit_reason"] = reason
    e["exit_time"] = when
    if price and e.get("entry_price"):
        # Charge realistic slippage on both the buy and the sell.
        slip = float(settings().get("realism", {}).get("slippage_pct", 0) or 0)
        buy, sell = e["entry_price"] * (1 + slip), price * (1 - slip)
        e["pnl"] = round((sell - buy) * e["qty"], 2)
        e["pnl_pct"] = round(sell / buy - 1, 4)


def sync(broker):
    """Update every unfinished trade with what actually happened at Alpaca."""
    entries = load()
    held = {p["symbol"] for p in broker.positions()}
    changed = 0
    for e in entries:
        if e["status"] not in ("submitted", "open"):
            continue
        try:
            o = broker.get_order(e["order_id"])
        except Exception as err:
            log(f"journal: could not look up {e['symbol']} order: {err}")
            continue
        if e["status"] == "submitted":
            if _f(o.get("filled_avg_price")):
                e["entry_price"] = _f(o["filled_avg_price"])
                e["qty"] = _f(o.get("filled_qty")) or e.get("qty")
                e["entry_time"] = o.get("filled_at")
                e["status"] = "open"
                changed += 1
            elif o.get("status") in ("canceled", "expired", "rejected"):
                e["status"] = "canceled"
                e["exit_reason"] = f"order {o.get('status')}"
                changed += 1
                continue
            else:
                continue
        # status is open: did it get sold?
        if e.get("exit_order_id"):
            try:
                x = broker.get_order(e["exit_order_id"])
                if _f(x.get("filled_avg_price")):
                    _close(e, _f(x["filled_avg_price"]), e.get("exit_reason") or "manager_sell", x.get("filled_at"))
                    changed += 1
            except Exception:
                pass
            continue
        for leg in o.get("legs") or []:
            if leg.get("status") == "filled" and _f(leg.get("filled_avg_price")):
                reason = "stop_loss" if leg.get("type") in ("stop", "stop_limit", "trailing_stop") else "take_profit"
                _close(e, _f(leg["filled_avg_price"]), reason, leg.get("filled_at"))
                changed += 1
                break
        else:
            if e["symbol"] not in held and e["status"] == "open":
                _close(e, None, "sold outside the bot", now_et().isoformat())
                changed += 1
    save(entries)
    if changed:
        log(f"journal: updated {changed} trade(s)")
    return entries


def trades_on(date, entries=None):
    entries = entries if entries is not None else load()
    return [e for e in entries if e["date"] == date or (e.get("exit_time") or "").startswith(date)]


def team_stats(team_id, entries):
    closed = [e for e in entries if e["team"] == team_id and e["status"] == "closed" and e.get("pnl") is not None]
    wins = [e for e in closed if e["pnl"] > 0]
    return {
        "closed_trades": len(closed),
        "wins": len(wins),
        "losses": len(closed) - len(wins),
        "win_rate": round(len(wins) / len(closed), 3) if closed else None,
        "realized_pnl": round(sum(e["pnl"] for e in closed), 2),
    }
