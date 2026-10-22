"""Team Investors. Plain code, no AI.

Takes the manager's decisions, runs each one past the risk rules, places
the orders at Alpaca, and records why every trade was made.
"""
import time

from . import journal
from .scorecard import clean_setup
from .common import log, now_et, today


def _retry(fn, tries=4):
    """Cancelling a stop order takes a moment to free the shares; retry briefly."""
    for i in range(tries):
        try:
            return fn()
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(1 + i)


def _sell(broker, team_id, symbol, reason, exit_reason="manager_sell"):
    for o in broker.open_orders(symbol):  # cancel the stop-loss / take-profit orders first
        try:
            broker.cancel_order(o["id"])
        except Exception as e:
            log(f"{team_id}: could not cancel order for {symbol}: {e}")
    order = _retry(lambda: broker.close_position(symbol))
    entries = journal.load()
    for e in entries:
        if e["symbol"] == symbol and e["status"] == "open" and not e.get("exit_order_id"):
            e["exit_order_id"] = order.get("id")
            e["exit_reason"] = exit_reason
            e["exit_note"] = reason
    journal.save(entries)
    return order


_SHORTABLE = {}


def shortable(broker, sym):
    if sym not in _SHORTABLE:
        try:
            a = broker.asset(sym)
            _SHORTABLE[sym] = bool(a.get("shortable")) and bool(a.get("easy_to_borrow"))
        except Exception:
            _SHORTABLE[sym] = False
    return _SHORTABLE[sym]


def execute(broker, desk, team, plan, prices, volatility=None):
    """Run the manager's decisions past the risk desk and place the orders.
    BUY = open a long, SHORT = open a short, SELL / COVER = close whatever the team holds in that stock."""
    volatility = volatility or {}
    results = []
    decisions = plan.get("decisions") or []
    act = lambda d: str(d.get("action", "")).upper()  # noqa: E731
    closes = [d for d in decisions if act(d) in ("SELL", "COVER")]
    entries = sorted([d for d in decisions if act(d) in ("BUY", "SHORT")], key=lambda d: -(d.get("conviction") or 0))

    for d in closes:
        sym = str(d.get("symbol", "")).upper()
        ok, why = desk.check_sell(team["id"], sym)
        if not ok:
            journal.add_blocked(team["id"], sym, act(d), why)
            results.append(f"{act(d)} {sym} blocked: {why}")
            continue
        try:
            _sell(broker, team["id"], sym, d.get("reason", ""))
            desk.positions.pop(sym, None)
            results.append(f"{act(d)} {sym}")
            log(f"{team['id']}: {act(d)} {sym} - {d.get('reason', '')[:80]}")
        except Exception as e:
            journal.add_blocked(team["id"], sym, act(d), f"broker error: {e}")
            results.append(f"{act(d)} {sym} failed: {e}")

    def enter(d, deploying=False):
        sym = str(d.get("symbol", "")).upper()
        side = "short" if act(d) == "SHORT" or str(d.get("direction", "")).lower() == "short" else "long"
        label = "SHORT" if side == "short" else "BUY"
        price = prices.get(sym)
        can_short = shortable(broker, sym) if side == "short" else True
        ok, why, p = desk.check_entry(team["id"], sym, price, d.get("conviction"), d.get("stop_loss_pct"),
                                      d.get("take_profit_pct"), side, deploying, volatility.get(sym), can_short)
        if not ok:
            journal.add_blocked(team["id"], sym, label, why)
            results.append(f"{label} {sym} blocked: {why}")
            return
        cid = journal.new_id(team["id"], sym)
        try:
            if p["kind"] == "shares":
                order = broker.market_order(sym, "buy" if side == "long" else "sell", cid[:48], qty=p["qty"])
            else:
                order = broker.market_order(sym, "buy", cid[:48], notional=p["dollars"])
        except Exception as e:
            journal.add_blocked(team["id"], sym, label, f"broker error: {e}")
            results.append(f"{label} {sym} failed: {e}")
            desk.positions.pop(sym, None)
            return
        reason = d.get("reason", "")
        if deploying:
            reason = f"[75% invested rule] {reason}"
        journal.add({
            "id": cid, "date": today(), "time": now_et().strftime("%H:%M"), "team": team["id"],
            "symbol": sym, "side": side, "kind": p["kind"], "qty": p["qty"], "qty_open": p["qty"],
            "dollars": p["dollars"], "est_price": price, "stop_price": p["stop_price"],
            "first_stop": p["stop_price"], "take_profit_price": p["take_profit_price"],
            "risk_dollars": p["risk_dollars"], "conviction": d.get("conviction"), "reason": reason,
            "setup": clean_setup(d.get("setup")), "experiment": (plan.get("experiment") or "")[:200],
            "order_id": order.get("id"), "status": "submitted", "entry_price": None,
        })
        results.append(f"{label} {sym} x{p['qty']} (~${p['dollars']})" + (" [deploy]" if deploying else ""))
        log(f"{team['id']}: {label} {sym} x{p['qty']} stop {p['stop_price']} first target {p['take_profit_price']}"
            + (" (75% rule)" if deploying else ""))

    for d in entries:
        enter(d)

    # Minimum-invested rule: if the team still holds too much cash, take the manager's next-best ideas.
    tried = {str(d.get("symbol", "")).upper() for d in entries}
    for d in plan.get("ranked_ideas") or []:
        if desk.deploy_gap(team["id"]) < 1 or desk.past_buy_cutoff() or not desk.market_open \
                or desk.team_open_count(team["id"]) >= desk.l["max_open_positions_per_team"]:
            break
        sym = str(d.get("symbol", "")).upper()
        if sym in tried or sym in desk.positions:
            continue
        tried.add(sym)
        enter(d, deploying=True)
    return results
