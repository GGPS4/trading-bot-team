"""Operations (plain code, no AI): the daily system check and the order cleanups.

preflight()      once a day before the open: is everything the bots need working?
cleanup_start()  at the start of a session: cancel leftover orders (e.g. from before a restart)
verify_flat()    after the end-of-day close: is everything really sold? Retry if not.
"""
import os

from . import journal
from .common import ROOT, STATE, log, now_et, read_json, today, write_json

PREFLIGHT = STATE / "research" / "_preflight.json"


def _check(name, fn):
    try:
        detail = fn()
        return {"name": name, "ok": True, "detail": str(detail)[:140]}
    except Exception as e:
        return {"name": name, "ok": False, "detail": str(e)[:180]}


def preflight(ctx, force=False):
    """Test every part the trading day depends on and save the results for the dashboard."""
    prev = read_json(PREFLIGHT, {})
    if prev.get("date") == today() and not force:
        return prev
    b = ctx.broker
    checks = [
        _check("Alpaca account", lambda: f"equity ${float(b.account()['equity']):,.0f}"),
        _check("Market clock", lambda: "open" if b.clock().get("is_open") else "closed (opens "
               + str(b.clock().get("next_open", ""))[11:16] + ")"),
        _check("Live prices", lambda: f"SPY ${b.snapshots(['SPY'])['SPY']['price']}"),
        _check("Company news", lambda: f"{len(b.news(['SPY', 'AAPL'], hours=24))} recent articles"),
    ]

    def movers():
        m = b.movers(top=5)
        if not (m.get("gainers") or m.get("most_active")):
            raise RuntimeError("no data")
        return "top movers available"
    checks.append(_check("Market movers", movers))

    def calendar():
        from . import events
        cal = events.snapshot(ctx.org)
        return f"{len(cal.get('earnings', []))} earnings, {len(cal.get('economy', []))} economic events"
    checks.append(_check("Event calendar", calendar))

    providers = getattr(ctx.llm, "providers", {}) or {}
    working = 0
    for name, p in providers.items():
        if not p.available:
            continue

        def ask(p=p):
            res, model = p.ask("fast", "You are a test.", 'Return the JSON {"ok": true}.')
            if not res.get("ok"):
                raise RuntimeError(f"odd answer from {model}")
            return model
        c = _check(f"AI: {name}", ask)
        working += c["ok"]
        checks.append(c)
    if providers:
        checks.append({"name": "AI providers working", "ok": working > 0, "detail": f"{working} of "
                       f"{sum(1 for p in providers.values() if p.available)}"})

    def saving():
        if os.environ.get("BOT_GIT_PUSH") == "1" and not (ROOT / "save.sh").exists():
            raise RuntimeError("save.sh is missing")
        return "ready"
    checks.append(_check("Saving to GitHub", saving))

    out = {"date": today(), "time": now_et().isoformat(timespec="minutes"), "checks": checks,
           "all_ok": all(c["ok"] for c in checks)}
    write_json(PREFLIGHT, out)
    bad = [c["name"] for c in checks if not c["ok"]]
    log("System check: all good" if not bad else f"System check failed: {', '.join(bad)}")
    return out


def _flatten(orders):
    for o in orders or []:
        yield o
        for leg in o.get("legs") or []:
            yield leg


def cleanup_start(broker, owner_of):
    """Cancel orders that no longer make sense: exits for stocks we don't hold, extra stop
    orders on the same stock, and entry orders left over from an earlier day."""
    try:
        orders = list(_flatten(broker.open_orders()))
        held = {p["symbol"]: p for p in broker.positions()}
    except Exception as e:
        log(f"Order cleanup skipped: {e}")
        return 0
    keep = {e.get("stop_order_id") for e in journal.load() if e["status"] in ("open", "submitted")}
    stops_seen, cancelled = set(), 0
    for o in orders:
        if o.get("status") not in ("new", "accepted", "held", "pending_new", "partially_filled"):
            continue
        sym = o.get("symbol")
        if sym not in owner_of and sym not in held:
            continue  # not one of ours
        is_exit = o.get("type") in ("stop", "stop_limit", "limit", "trailing_stop")
        reason = None
        if sym not in held and is_exit:
            reason = "exit order for a stock no longer held"
        elif o.get("type") == "market" and str(o.get("submitted_at") or o.get("created_at") or "")[:10] < today():
            reason = "entry order left over from an earlier day"
        elif o.get("type") in ("stop", "stop_limit"):
            if sym in stops_seen and o.get("id") not in keep:
                reason = "duplicate stop order"
            stops_seen.add(sym)
        if reason:
            try:
                broker.cancel_order(o["id"])
                cancelled += 1
                log(f"Cleanup: canceled {sym} {o.get('type')} order ({reason})")
            except Exception as e:
                log(f"Cleanup: could not cancel {sym} order: {e}")
    if cancelled:
        log(f"Order cleanup: canceled {cancelled} leftover order(s)")
    return cancelled


def verify_flat(broker, owner_of, sell):
    """After the end-of-day close: anything the teams still hold is sold again (returns what's left)."""
    try:
        left = [p["symbol"] for p in broker.positions() if owner_of.get(p["symbol"])]
    except Exception as e:
        log(f"End-of-day check failed: {e}")
        return []
    for sym in left:
        try:
            with journal.LOCK:
                sell(broker, owner_of[sym], sym, "end of day (retry)", "end_of_day")
            log(f"End-of-day check: {sym} was still held; sold it again")
        except Exception as e:
            log(f"End-of-day check: could not sell {sym}: {e}")
    return left
