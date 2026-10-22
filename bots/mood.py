"""Market mood check (plain code, no AI).

Looks at the whole market before any single stock:
  SPY  (S&P 500)        - the broad market
  QQQ  (Nasdaq 100)     - big tech / growth
  VIXY (VIX futures ETF) - a fear gauge: it jumps when investors get scared
  breadth               - share of all watched stocks that are up today

Regimes:
  normal    - trade as usual
  caution   - market sliding: long positions sized 25% smaller
  risk_off  - sharp drop or fear spike: longs half size and need conviction +1; shorts favored
"""
from .common import STATE, now_et, settings, write_json

SYMBOLS = ["SPY", "QQQ", "VIXY"]


def _chg(snap):
    if not snap or not snap.get("prev_close"):
        return None
    return snap["price"] / snap["prev_close"] - 1


def compute(snaps, watched):
    c = settings().get("market_mood", {})
    spy, qqq, vix = (_chg(snaps.get(s)) for s in SYMBOLS)
    moves = [_chg(snaps.get(s)) for s in watched]
    moves = [m for m in moves if m is not None]
    breadth = round(sum(1 for m in moves if m > 0) / len(moves), 2) if moves else None
    regime, why = "normal", []
    worst = min([x for x in (spy, qqq) if x is not None], default=None)
    if worst is not None and worst <= -c.get("risk_off_index_drop", 0.012):
        regime = "risk_off"
        why.append(f"index down {worst:.1%}")
    if vix is not None and vix >= c.get("risk_off_fear_jump", 0.06):
        regime = "risk_off"
        why.append(f"fear gauge (VIXY) up {vix:.1%}")
    if regime == "normal":
        if worst is not None and worst <= -c.get("caution_index_drop", 0.006):
            regime = "caution"
            why.append(f"index down {worst:.1%}")
        if breadth is not None and breadth <= c.get("caution_breadth", 0.3):
            regime = "caution"
            why.append(f"only {breadth:.0%} of watched stocks are up")
    note = {
        "normal": "Market is calm or rising. Trade your best setups as usual.",
        "caution": "Market is sliding. Long positions are sized 25% smaller; be pickier with longs.",
        "risk_off": "Market is falling hard or fear is spiking. Longs are half size and need conviction +1. "
                    "Shorting weak stocks (below VWAP, breaking down) is favored.",
    }[regime]
    out = {"time": now_et().isoformat(timespec="minutes"), "regime": regime,
           "spy": round(spy, 4) if spy is not None else None, "qqq": round(qqq, 4) if qqq is not None else None,
           "fear_vixy": round(vix, 4) if vix is not None else None, "breadth": breadth,
           "why": ", ".join(why) or "no warning signs", "note": note}
    write_json(STATE / "research" / "_mood.json", out)
    return out
