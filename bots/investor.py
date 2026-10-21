"""Team Investors. Plain code, no AI.

Takes the manager's decisions, runs each one past the risk rules, places
the orders at Alpaca, and records why every trade was made.
"""
from . import journal
from .common import log, now_et, today


def _sell(broker, team_id, symbol, reason, exit_reason="manager_sell"):
    for o in broker.open_orders(symbol):  # cancel the stop-loss / take-profit orders first
        try:
            broker.cancel_order(o["id"])
        except Exception as e:
            log(f"{team_id}: could not cancel order for {symbol}: {e}")
    order = broker.close_position(symbol)
    entries = journal.load()
    for e in entries:
        if e["symbol"] == symbol and e["status"] == "open" and not e.get("exit_order_id"):
            e["exit_order_id"] = order.get("id")
            e["exit_reason"] = exit_reason
            e["exit_note"] = reason
    journal.save(entries)
    return order


def execute(broker, desk, team, plan, prices):
    results = []
    decisions = plan.get("decisions") or []
    sells = [d for d in decisions if str(d.get("action", "")).upper() == "SELL"]
    buys = sorted([d for d in decisions if str(d.get("action", "")).upper() == "BUY"],
                  key=lambda d: -(d.get("conviction") or 0))

    for d in sells:
        sym = str(d.get("symbol", "")).upper()
        ok, why = desk.check_sell(team["id"], sym)
        if not ok:
            journal.add_blocked(team["id"], sym, "SELL", why)
            results.append(f"SELL {sym} blocked: {why}")
            continue
        try:
            _sell(broker, team["id"], sym, d.get("reason", ""))
            desk.positions.pop(sym, None)
            results.append(f"SELL {sym}")
            log(f"{team['id']}: SELL {sym} - {d.get('reason', '')[:80]}")
        except Exception as e:
            journal.add_blocked(team["id"], sym, "SELL", f"broker error: {e}")
            results.append(f"SELL {sym} failed: {e}")

    def buy(d, deploying=False):
        sym = str(d.get("symbol", "")).upper()
        price = prices.get(sym)
        ok, why, p = desk.check_buy(team["id"], sym, price, d.get("conviction"),
                                    d.get("stop_loss_pct"), d.get("take_profit_pct"), deploying=deploying)
        if not ok:
            journal.add_blocked(team["id"], sym, "BUY", why)
            results.append(f"BUY {sym} blocked: {why}")
            return
        cid = journal.new_id(team["id"], sym)
        try:
            if p["kind"] == "bracket":
                order = broker.buy_bracket(sym, p["qty"], p["stop_price"], p["take_profit_price"], cid[:48])
            else:
                order = broker.buy_notional(sym, p["dollars"], cid[:48])
        except Exception as e:
            journal.add_blocked(team["id"], sym, "BUY", f"broker error: {e}")
            results.append(f"BUY {sym} failed: {e}")
            return
        reason = d.get("reason", "")
        if deploying:
            reason = f"[75% invested rule] {reason}"
        journal.add({
            "id": cid, "date": today(), "time": now_et().strftime("%H:%M"), "team": team["id"],
            "symbol": sym, "side": "buy", "kind": p["kind"], "qty": p["qty"], "dollars": p["dollars"],
            "est_price": price, "stop_price": p["stop_price"], "take_profit_price": p["take_profit_price"],
            "conviction": d.get("conviction"), "reason": reason,
            "order_id": order.get("id"), "status": "submitted", "entry_price": None,
        })
        results.append(f"BUY {sym} x{p['qty']} (~${p['dollars']})" + (" [deploy]" if deploying else ""))
        log(f"{team['id']}: BUY {sym} x{p['qty']} stop {p['stop_price']} target {p['take_profit_price']}"
            + (" (75% rule)" if deploying else ""))

    for d in buys:
        buy(d)

    # Minimum-invested rule: if the team still holds too much cash, buy the manager's next-best ideas.
    tried = {str(d.get("symbol", "")).upper() for d in buys}
    for d in plan.get("ranked_ideas") or []:
        if desk.deploy_gap(team["id"]) < 1 or desk.past_buy_cutoff() or not desk.market_open:
            break
        sym = str(d.get("symbol", "")).upper()
        if sym in tried or sym in desk.positions:
            continue
        tried.add(sym)
        buy(d, deploying=True)
    return results


def check_software_stops(broker, prices, paused, market_open):
    """Fractional-share positions have no stop orders at Alpaca, so check them here."""
    if paused or not market_open:
        return []
    done = []
    for e in journal.load():
        if e["status"] != "open" or e.get("kind") != "fractional" or e.get("exit_order_id"):
            continue
        price = prices.get(e["symbol"])
        if not price or not e.get("stop_price") or not e.get("take_profit_price"):
            continue
        reason = None
        if price <= e["stop_price"]:
            reason = "stop_loss"
        elif price >= e["take_profit_price"]:
            reason = "take_profit"
        if reason:
            try:
                _sell(broker, e["team"], e["symbol"], f"price {price} hit {reason}", exit_reason=reason)
                done.append(f"{e['symbol']} {reason}")
                log(f"{e['team']}: {e['symbol']} hit {reason} at {price}")
            except Exception as err:
                log(f"could not close {e['symbol']}: {err}")
    return done
