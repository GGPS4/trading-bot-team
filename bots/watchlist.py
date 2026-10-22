"""Team watchlists: the 15-20 stocks each team watches, changing as news comes out.

Each team starts from its list in config/teams.yaml. The team manager can add
stocks in its field that are moving or in the news, and drop ones that have gone
quiet. Code enforces the rules:
  - the list stays between watchlist_min and watchlist_max stocks
  - a stock belongs to only one team (no two teams trading the same name)
  - a stock the team is holding can't be dropped until it is sold
  - new stocks must be real, active, tradable US stocks at Alpaca
The team still holds at most max_open_positions_per_team at a time.
"""
import re

from .common import STATE, log, now_et, read_json, settings, write_json

SYMBOL = re.compile(r"^[A-Z]{1,5}(\.[A-Z])?$")


def path(tid):
    return STATE / "watchlists" / f"{tid}.json"


def limits():
    w = settings().get("watchlist", {})
    return int(w.get("min", 15)), int(w.get("max", 20)), int(w.get("max_changes_per_look", 3))


def load(tid, seed):
    data = read_json(path(tid), None)
    if not data or not data.get("symbols"):
        data = {"symbols": list(seed), "seed": list(seed), "log": []}
    lo, hi, _ = limits()
    for s in seed:  # top up from the starter list if the watchlist is short
        if len(data["symbols"]) >= lo:
            break
        if s not in data["symbols"]:
            data["symbols"].append(s)
    return data


def save(tid, data):
    data["log"] = data.get("log", [])[-300:]
    data["rejected"] = data.get("rejected", [])[-30:]
    write_json(path(tid), data)


def _tradable(broker, sym, cache):
    if sym in cache:
        return cache[sym]
    ok, why = False, "unknown"
    try:
        a = broker.asset(sym)
        ok = bool(a.get("tradable")) and a.get("status") == "active" and a.get("class", "us_equity") == "us_equity"
        why = "ok" if ok else "not tradable at the broker"
        if ok:
            min_price = float(settings().get("watchlist", {}).get("min_price", 5))
            price = (broker.latest_prices([sym]) or {}).get(sym)
            if not price or price < min_price:
                ok, why = False, f"price {price} is under ${min_price:.0f} (too risky / thin)"
    except Exception as e:
        why = f"not found ({str(e)[:60]})"
    cache[sym] = (ok, why)
    return cache[sym]


def apply_changes(broker, org, owner_of, tid, changes, held, source="manager"):
    """Apply a manager's {"add": [...], "remove": [...]} to the team's watchlist.
    Updates org[tid]["tickers"] and owner_of in place. Returns a list of what happened."""
    if not changes or tid not in org:
        return []
    lo, hi, max_n = limits()
    team = org[tid]
    data = load(tid, team["tickers"])
    syms = list(team["tickers"])
    held = set(held or [])
    done, cache, n = [], {}, 0
    when = now_et().isoformat(timespec="minutes")

    def item(x):
        if isinstance(x, str):
            return x.strip().upper(), ""
        return str(x.get("symbol", "")).strip().upper(), str(x.get("reason", ""))[:200]

    adds = [item(x) for x in (changes.get("add") or [])][:max_n]
    removes = [item(x) for x in (changes.get("remove") or [])][:max_n]

    # Drops first, but never below the minimum after counting this look's adds.
    valid_adds = []
    for sym, why in adds:
        if not SYMBOL.match(sym) or sym in syms:
            continue
        if owner_of.get(sym) and owner_of[sym] != tid:
            done.append({"time": when, "action": "add_rejected", "symbol": sym,
                         "reason": f"already watched by the {owner_of[sym]} team"})
            continue
        ok, msg = _tradable(broker, sym, cache)
        if not ok:
            done.append({"time": when, "action": "add_rejected", "symbol": sym, "reason": msg})
            continue
        valid_adds.append((sym, why))

    for sym, why in removes:
        if sym not in syms:
            continue
        if sym in held:
            done.append({"time": when, "action": "remove_rejected", "symbol": sym,
                         "reason": "still holding it; sell first"})
            continue
        if len(syms) - 1 + len(valid_adds) < lo:
            done.append({"time": when, "action": "remove_rejected", "symbol": sym,
                         "reason": f"list would drop below {lo} stocks"})
            continue
        syms.remove(sym)
        owner_of.pop(sym, None)
        done.append({"time": when, "action": "removed", "symbol": sym, "reason": why, "by": source})
        n += 1

    for sym, why in valid_adds:
        if len(syms) >= hi:
            done.append({"time": when, "action": "add_rejected", "symbol": sym,
                         "reason": f"list is full ({hi}); drop a stock first"})
            continue
        syms.append(sym)
        owner_of[sym] = tid
        done.append({"time": when, "action": "added", "symbol": sym, "reason": why, "by": source})

    team["tickers"] = syms
    data["symbols"] = syms
    data.setdefault("log", []).extend(d for d in done if d["action"] in ("added", "removed"))
    data["rejected"] = (data.get("rejected", []) + [d for d in done if "rejected" in d["action"]])[-30:]
    data["updated"] = when
    save(tid, data)
    for d in done:
        log(f"{tid}: watchlist {d['action']} {d['symbol']} - {d.get('reason', '')[:80]}")
    return done


def check_all(broker, org, owner_of, held):
    """Once per run: drop stocks that stopped trading (delisted, bought out, renamed)."""
    cache = {}
    for tid, team in org.items():
        bad = [s for s in team["tickers"] if s not in held and not _tradable(broker, s, cache)[0]]
        if bad:
            apply_changes(broker, org, owner_of, tid,
                          {"remove": [{"symbol": s, "reason": cache[s][1]} for s in bad]}, held, source="system")


def discovery(broker, org):
    """What's moving across the whole market right now, so managers can find new names."""
    out = {}
    try:
        m = broker.movers(top=20)
        out.update(m)
    except Exception as e:
        log(f"movers unavailable: {e}")
    try:
        news = broker.market_news(hours=6, limit=50)
        out["headlines"] = [{"symbols": n.get("symbols"), "headline": n.get("headline")} for n in news][:40]
    except Exception as e:
        log(f"market news unavailable: {e}")
    taken = {s: tid for tid, t in org.items() for s in t["tickers"]}
    out["taken_by_other_teams"] = taken
    return out


def for_team(disc, tid):
    """Discovery info trimmed for one team's prompt (hide names other teams already own)."""
    if not disc:
        return "not available"
    taken = {s for s, t in (disc.get("taken_by_other_teams") or {}).items() if t != tid}
    pick = lambda rows: [r for r in rows if (r["symbol"] if isinstance(r, dict) else r) not in taken]
    trim = lambda rows: [{"symbol": r["symbol"], "chg": r.get("change_pct")} if isinstance(r, dict) else r for r in rows]  # noqa: E731
    return {"top_gainers": trim(pick(disc.get("gainers") or [])[:8]), "top_losers": trim(pick(disc.get("losers") or [])[:8]),
            "most_active": pick(disc.get("most_active") or [])[:10],
            "headlines": [f"{','.join((h.get('symbols') or [])[:3])}: {str(h.get('headline'))[:90]}"
                          for h in (disc.get("headlines") or [])[:10]],
            "note": "stocks other teams already watch were left out"}
