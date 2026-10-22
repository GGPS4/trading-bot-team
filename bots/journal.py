"""Trade journal: every trade, why it was made, and how it ended.

This is the team's memory. Managers learn from it, the Reporter reports
from it, and the dashboard shows it.
"""
import threading
import uuid

from .common import STATE, log, now_et, read_json, settings, today, write_json

TRADES = STATE / "journal" / "trades.json"
LOCK = threading.RLock()  # the minute-by-minute exit checker and the main loop take turns with the journal
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


def _slip():
    return float(settings().get("realism", {}).get("slippage_pct", 0) or 0)


def leg_pnl(e, qty, exit_price):
    """Profit on `qty` shares closed at `exit_price`, after slippage on both sides. Works for shorts."""
    entry, slip = e["entry_price"], _slip()
    if e.get("side") == "short":
        return (entry * (1 - slip) - exit_price * (1 + slip)) * qty
    return (exit_price * (1 - slip) - entry * (1 + slip)) * qty


def _close(e, price, reason, when):
    e["status"] = "closed"
    e["exit_price"] = price
    e["exit_reason"] = reason
    e["exit_time"] = when
    if price and e.get("entry_price"):
        partial = sum(p.get("pnl") or 0 for p in e.get("partials") or [])
        rest = e.get("qty_open", e["qty"]) or 0
        e["pnl"] = round(partial + leg_pnl(e, rest, price), 2)
        cost = e["entry_price"] * (e.get("qty") or 0)
        e["pnl_pct"] = round(e["pnl"] / cost, 4) if cost else None


def _stop_reason(e):
    return "trailing_stop" if e.get("trail_active") else "breakeven_stop" if e.get("scaled") else "stop_loss"


def sync(broker):
    """Update every unfinished trade with what actually happened at the broker."""
    entries = load()
    held = {p["symbol"] for p in broker.positions()}
    changed = 0
    for e in entries:
        if e["status"] not in ("submitted", "open"):
            continue
        if not e.get("order_id"):  # recovered position (no original order on record)
            if e["symbol"] not in held and not e.get("exit_order_id") and not e.get("stop_order_id"):
                _close(e, None, "sold (recovered position)", now_et().isoformat())
                changed += 1
                continue
            o = {"status": "filled"}
        else:
            try:
                o = broker.get_order(e["order_id"])
            except Exception as err:
                log(f"journal: could not look up {e['symbol']} order: {err}")
                continue
        if e["status"] == "submitted":
            if _f(o.get("filled_avg_price")):
                e["entry_price"] = _f(o["filled_avg_price"])
                e["qty"] = _f(o.get("filled_qty")) or e.get("qty")
                e["qty_open"] = e["qty"]
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
        # fill in prices of partial sells (scaling out)
        for part in e.get("partials") or []:
            if part.get("filled") or not part.get("order_id"):
                continue
            try:
                x = broker.get_order(part["order_id"])
                if _f(x.get("filled_avg_price")):
                    part["price"], part["filled"] = _f(x["filled_avg_price"]), True
                    part["pnl"] = round(leg_pnl(e, part["qty"], part["price"]), 2)
                    changed += 1
            except Exception:
                pass
        # status is open: did it get closed?
        if e.get("exit_order_id"):
            try:
                x = broker.get_order(e["exit_order_id"])
                if _f(x.get("filled_avg_price")):
                    _close(e, _f(x["filled_avg_price"]), e.get("exit_reason") or "manager_sell", x.get("filled_at"))
                    changed += 1
            except Exception:
                pass
            continue
        if e.get("stop_order_id"):
            try:
                x = broker.get_order(e["stop_order_id"])
                if _f(x.get("filled_avg_price")) and x.get("status") == "filled":
                    _close(e, _f(x["filled_avg_price"]), _stop_reason(e), x.get("filled_at"))
                    changed += 1
                    continue
                if x.get("status") in ("canceled", "expired", "rejected"):
                    e["stop_order_id"] = None  # the exit manager places a new one
            except Exception:
                pass
        legs_done = False
        for leg in o.get("legs") or []:  # older bracket orders
            if leg.get("status") == "filled" and _f(leg.get("filled_avg_price")):
                reason = "stop_loss" if leg.get("type") in ("stop", "stop_limit", "trailing_stop") else "take_profit"
                _close(e, _f(leg["filled_avg_price"]), reason, leg.get("filled_at"))
                changed += 1
                legs_done = True
                break
        if not legs_done and e["symbol"] not in held and e["status"] == "open":
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


def reconcile(broker, owner_of):
    """If a run was stopped before saving, Alpaca may hold positions the journal
    doesn't know about. Record them so stops, reports and the dashboard stay correct."""
    entries = load()
    known = {e["symbol"] for e in entries if e["status"] in ("submitted", "open")}
    added = 0
    for p in broker.positions():
        sym = p["symbol"]
        if sym in known:
            continue
        stop = target = stop_id = None
        try:
            for o in broker.open_orders(sym):
                if o.get("type") in ("stop", "stop_limit") and o.get("stop_price"):
                    stop, stop_id = _f(o["stop_price"]), o.get("id")
                elif o.get("type") == "limit" and o.get("limit_price"):
                    target = _f(o["limit_price"])
        except Exception:
            pass
        qty = abs(_f(p["qty"]) or 0)
        entries.append({
            "id": new_id(owner_of.get(sym, "unassigned"), sym), "date": today(), "time": now_et().strftime("%H:%M"),
            "team": owner_of.get(sym, "unassigned"), "symbol": sym,
            "side": "short" if (_f(p["qty"]) or 0) < 0 else "long",
            "kind": "bracket" if (stop and target) else "shares" if float(qty).is_integer() else "fractional",
            "qty": qty, "qty_open": qty,
            "dollars": round(abs(_f(p["market_value"]) or 0), 2), "est_price": _f(p["avg_entry_price"]),
            "entry_price": _f(p["avg_entry_price"]), "stop_price": stop, "take_profit_price": target,
            "conviction": None, "reason": "Recovered from Alpaca after a restart (original notes were not saved).",
            "order_id": None, "status": "open", "stop_order_id": stop_id,
        })
        added += 1
    if added:
        save(entries)
        log(f"journal: recovered {added} position(s) from Alpaca")
    return added
