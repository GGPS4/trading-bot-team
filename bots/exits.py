"""Exit manager (plain code, no AI). Runs about once a minute while the market is open.

For every open trade:
  1. Protective stop: whole-share positions keep a real stop order at the broker, so a stop
     works even between checks. Fractional positions are watched here.
  2. Scale out: at the first target, sell part of the position (half by default) and move
     the stop on the rest to the entry price, so the trade can no longer lose money.
  3. Trailing stop: once a trade is up (1% by default), the stop follows the best price since
     entry at a set distance and only ever moves in the trade's favor. Winners keep running
     while gains are locked in.
Everything still gets closed before the bell by the end-of-day rule.
"""
import math

from . import journal
from .common import log, now_et, settings


def _cfg():
    return settings().get("exits", {})


def _better(side, new, old):
    """Is `new` a tighter (safer) stop than `old` for this side?"""
    if old is None:
        return True
    return new > old if side == "long" else new < old


def _place_or_update_stop(broker, e):
    """Make sure a whole-share position has a stop order at the broker matching e['stop_price']."""
    qty = int(e.get("qty_open") or 0)
    if qty < 1 or not e.get("stop_price"):
        return
    side = "sell" if e.get("side", "long") == "long" else "buy"
    sid = e.get("stop_order_id")
    if sid:
        if e.get("stop_sent") == e["stop_price"] and e.get("stop_qty_sent") == qty:
            return  # already in place (journal.sync notices if it filled or was canceled)
        try:
            r = broker.replace_order(sid, qty=qty, stop_price=e["stop_price"])
            e["stop_order_id"] = r.get("id", sid)  # a replace creates a new order id
            e["stop_sent"], e["stop_qty_sent"] = e["stop_price"], qty
        except Exception as err:
            log(f"{e['symbol']}: could not move stop: {err}")
        return
    try:
        o = broker.stop_order(e["symbol"], side, qty, e["stop_price"])
        e["stop_order_id"] = o.get("id")
        e["stop_sent"], e["stop_qty_sent"] = e["stop_price"], qty
    except Exception as err:
        # Shares already held by an older order (e.g. a bracket from before a restart): adopt its stop.
        try:
            for x in broker.open_orders(e["symbol"]):
                legs = [x] + list(x.get("legs") or [])
                for leg in legs:
                    if leg.get("type") in ("stop", "stop_limit") and leg.get("status") in ("new", "accepted", "held"):
                        e["stop_order_id"] = leg.get("id")
                        e["stop_price"] = float(leg.get("stop_price") or e["stop_price"])
                        e["stop_sent"], e["stop_qty_sent"] = e["stop_price"], qty
                        e["kind"] = "bracket" if x.get("legs") else e.get("kind")
                        log(f"{e['symbol']}: using the existing stop order at {e['stop_price']}")
                        return
        except Exception:
            pass
        if not e.get("stop_warned"):
            e["stop_warned"] = True
            log(f"{e['symbol']}: could not place stop order: {err}")


def manage(broker, prices, paused, market_open):
    """One pass over all open trades. Returns a list of what happened."""
    if not market_open:
        return []
    from .investor import _retry, _sell  # avoid an import loop
    c = _cfg()
    trail_on, scale_on = c.get("trailing_stop", True), c.get("scale_out", True)
    activate, trail = c.get("trail_activate_pct", 0.01), c.get("trail_pct", 0.008)
    frac = c.get("scale_out_fraction", 0.5)
    entries = journal.load()
    done, to_close = [], []
    for e in entries:
        if e["status"] != "open" or e.get("exit_order_id") or e.get("kind") == "bracket":
            continue
        price, entry = prices.get(e["symbol"]), e.get("entry_price")
        if not price or not entry:
            continue
        side = e.get("side", "long")
        long_ = side == "long"
        e["high"] = max(e.get("high") or entry, price)
        e["low"] = min(e.get("low") or entry, price)
        gain = (price / entry - 1) if long_ else (entry / price - 1)
        qty_open = float(e.get("qty_open") or e.get("qty") or 0)
        whole = float(qty_open).is_integer()

        # 1. stop hit (fractional positions, or a missing broker stop)
        stop = e.get("stop_price")
        if stop and (price <= stop if long_ else price >= stop) and not (whole and e.get("stop_order_id")):
            if not paused:
                to_close.append((e["team"], e["symbol"], price, stop, journal._stop_reason(e)))
            continue

        # 2. scale out at the first target
        target = e.get("take_profit_price")
        if scale_on and not e.get("scaled") and target and (price >= target if long_ else price <= target):
            part = qty_open * frac
            part = math.floor(part) if whole else round(part, 6)
            if part > 0 and part < qty_open and not paused:
                try:
                    if e.get("stop_order_id"):  # free the shares held by the stop order
                        try:
                            broker.cancel_order(e["stop_order_id"])
                        except Exception:
                            pass
                        e["stop_order_id"] = None
                    o = _retry(lambda: broker.close_position(e["symbol"], qty=part))
                    e.setdefault("partials", []).append({
                        "time": now_et().isoformat(timespec="minutes"), "qty": part, "price": price,
                        "pnl": round(journal.leg_pnl(e, part, price), 2), "order_id": o.get("id"),
                        "reason": "first target"})
                    e["qty_open"] = round(qty_open - part, 6)
                    qty_open = e["qty_open"]
                    done.append(f"{e['symbol']} sold {part} at first target")
                    log(f"{e['team']}: {e['symbol']} scaled out {part} at {price} (first target)")
                except Exception as err:
                    log(f"{e['symbol']}: scale-out failed: {err}")
            e["scaled"] = True
            if _better(side, entry, e.get("stop_price")):
                e["stop_price"] = round(entry, 2)  # the rest can't turn into a loss

        # 3. trailing stop
        if trail_on and (gain >= activate or e.get("scaled")):
            new = (e["high"] * (1 - trail)) if long_ else (e["low"] * (1 + trail))
            new = round(new, 2)
            if _better(side, new, e.get("stop_price")):
                if not e.get("trail_active"):
                    log(f"{e['team']}: {e['symbol']} trailing stop on (up {gain:.1%})")
                e["stop_price"], e["trail_active"] = new, True

        # 4. keep the broker's stop order in line
        if whole and qty_open >= 1 and not paused:
            _place_or_update_stop(broker, e)
    journal.save(entries)
    for team, sym, price, stop, reason in to_close:
        try:
            _sell(broker, team, sym, f"price {price} hit the stop {stop}", reason)
            done.append(f"{sym} {reason}")
            log(f"{team}: {sym} {reason} at {price}")
        except Exception as err:
            log(f"could not close {sym}: {err}")
    return done
