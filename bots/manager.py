"""Team Managers: the day-to-day strategists.

plan():   decide what to buy and sell, with stop-loss and take-profit levels.
review(): after the close, study the losing trades and write lessons so
          the team doesn't repeat its mistakes.
"""
import json

from .common import STATE, now_et, read_json, settings, write_json
from .scorecard import SETUPS

PLAN_FORMAT = """Answer in JSON:
{
  "market_view": "2-3 sentences on what you see today",
  "decisions": [
    {"symbol": "TICKER", "action": "BUY" or "SHORT" or "SELL" or "COVER",
     "conviction": 1-5, "stop_loss_pct": 0.05, "take_profit_pct": 0.10, "setup": "one setup name (BUY/SHORT)",
     "reason": "why, citing the research or price data"}
  ],
  "ranked_ideas": [
    {"symbol": "TICKER", "direction": "long" or "short", "conviction": 1-5, "stop_loss_pct": 0.015,
     "take_profit_pct": 0.03, "setup": "one setup name", "reason": "short reason"}
  ],
  "experiment": "which strategy idea you are testing today, or empty",
  "watchlist_changes": {
    "add": [{"symbol": "TICKER", "reason": "why it belongs on the watchlist now (news, unusual move, volume)"}],
    "remove": [{"symbol": "TICKER", "reason": "why it no longer deserves a spot (quiet, no catalyst)"}]
  }
}
Actions: BUY opens a long (profit if the price rises). SHORT opens a short (profit if the price falls;
whole shares of easy-to-borrow stocks only). SELL closes a long and COVER closes a short you hold.
Only list stocks you want to open (not already held) or close (currently held). Everything else is a hold.
Exits are automatic: stop_loss_pct sets the first stop and take_profit_pct the FIRST target. At the first
target half the position is sold and the stop on the rest moves to the entry price; once a trade is up
about 1% a trailing stop follows the best price. So set take_profit_pct where you'd take partial profits.
Position size is set by code from the stop distance and the stock's volatility (about equal dollar risk
per trade), and is cut in a weak market (see market mood).
ranked_ideas: your next 5 best stocks to buy (not held, not already in decisions), best first. If the
team is below its minimum invested level after your decisions, the investor buys from this list.
Only use symbols from the team's list. It is fine to return no decisions if nothing is convincing.
stop_loss_pct and take_profit_pct are fractions of the buy price (0.05 = 5%).
setup must be one of: """ + ", ".join(SETUPS) + """. It is used to score which setups make money for you.
WATCHLIST: you watch 15-20 stocks and hold at most 5. Keep the watchlist full of the stocks in your
team's field with the best day-trading setups right now: add companies that are in the news, moving
unusually, or trading heavy volume (see the market discovery data), and drop ones that have gone quiet.
Only add companies that truly belong to your team's field. At most 3 adds and 3 removes per look; leave
both lists empty if the watchlist is fine. New stocks can be bought starting with your next look."""

REVIEW_FORMAT = """Answer in JSON:
{
  "what_went_well": "1-2 sentences",
  "what_went_wrong": "1-2 sentences, be specific about losing trades",
  "lessons": ["short, specific rules for yourself, e.g. 'Don't buy right after a 10% jump on earnings day'"],
  "strategy_ideas": ["new strategies or tweaks to test in the coming days"],
  "watchlist_changes": {"add": [{"symbol": "TICKER", "reason": "..."}], "remove": [{"symbol": "TICKER", "reason": "..."}]}
}
Give at most 3 lessons and 2 ideas. Only write a lesson if today's trades actually taught one.
watchlist_changes: set up tomorrow's 15-20 stock watchlist for day trading. Add companies in your field
with news or catalysts coming (earnings, launches, contracts, approvals); drop ones with nothing going on.
At most 3 adds and 3 removes; empty lists are fine."""


def lessons_path(team_id):
    return STATE / "lessons" / f"{team_id}.json"


def recent_lessons(team_id):
    n = settings().get("lessons_to_remember", 15)
    return [f"{x['date']}: {x['lesson']}" for x in read_json(lessons_path(team_id), [])[-n:]]


def plan(llm, team, orders, research, holdings, phase):
    notes = read_json(STATE / "manager_notes" / f"{team['id']}.json", {})
    system = (f"You are the Team Manager of the {team['name']} team on an AI stock trading team. You take orders "
              "from your executive, use your two researchers' findings, and decide which companies to buy or sell. "
              "You are a careful, disciplined trader: you look for clear reasons, respect your rules, and keep "
              "trying new strategies to build a more successful portfolio. "
              + (f"Team focus: {team['focus']}" if team.get("focus") else ""))
    style = settings().get("intraday", {}).get("strategy")
    risk = settings().get("risk", {})
    if style == "day_trading":
        strategy_text = (
            "TEAM STRATEGY: DAY TRADING. Make money from moves within today's session. Look for intraday "
            "momentum, news catalysts, strong moves off the open, breakouts above today's high and bounces "
            "with volume. Short weak stocks too: breakdowns below the opening range or VWAP on bad news, "
            "especially when the market mood is risk-off. Use tight stops (about 1-2%) and set the first "
            "target about 2-4% away (half is sold there, the rest trails). Rotate money: "
            "sell positions that have stalled or hit their idea's target and redeploy into the best current "
            "setup. Everything is sold before the close, so never buy for 'the long run'. "
            f"RULE: keep at least {risk.get('min_invested_pct', 0):.0%} of the team budget invested while the "
            "market is open. Sitting in cash is not allowed; if nothing is perfect, pick the best available setups.")
    else:
        strategy_text = ""
    if phase == "intraday":
        n = now_et()
        mins_left = max(0, (16 * 60) - (n.hour * 60 + n.minute))
        eod = settings().get("intraday", {}).get("close_positions_before_close")
        timing = (f"It is {n:%I:%M %p} ET; the market closes in {mins_left} minutes. You check in on the market "
                  "all day. Act when there is a real reason (news, a strong move, a stock hitting your levels); "
                  "doing nothing is often right. Don't open new positions in the last 15 minutes."
                  + (" All positions will be sold automatically just before the close, so only buy for "
                     "moves you expect today." if eod else ""))
    elif phase == "morning":
        timing = "It is the morning session, shortly after the market opened."
    else:
        timing = "It is the midday check. Only act if new, important information came in since the morning."
    prompt = f"""{timing}
{strategy_text}
Team companies (your watchlist, {len(team['tickers'])} stocks): {', '.join(team['tickers'])}
Money: {orders.get('deployment', '')}
Orders from your executive (from the President): {_j({k: v for k, v in orders.items() if k != 'deployment'})}

Current positions held by your team:
{_j(holdings) if holdings else 'none'}

Market mood (regime affects position sizes and the conviction needed for longs): {_mood(research.get('mood'))}

Live prices and day-trading signals (one line per stock): day = change vs yesterday's close, open = vs today's
open, look = since your last check, vwap = price vs today's volume-weighted average price, OR = opening range
(first 30 min) with BREAKOUT/BREAKDOWN, m15/m60 = momentum, rvol = volume vs a normal day at this hour.
Good long setups usually trade above VWAP with an opening-range breakout or strong 15m momentum and rvol > 1.5.
Be wary of buying far above VWAP after a big run with fading momentum.
{_intraday(research.get('intraday'))}

Daily history (5d/20d change, RSI 14: >70 overbought, <30 oversold, 50d = above the 50-day average):
{_tech(research.get('technicals'))}

News researcher: {_findings(research.get('news'), 10)}
Reddit researcher: {_findings(research.get('social'), 5)}
Truth Social / X watch: {_findings(research.get('market_watch'), 5)}
Overnight findings: {_j([_f1(f) for f in (research.get('overnight') or [])[:8]]) or 'none'}
Pre-market scan: {_j(research.get('premarket') or 'none')}
Event calendar: {_j(research.get('calendar') or 'not available')}
Market discovery (for new watchlist names): {_j(research.get('discovery') or 'not available')}
Your scorecard (last 30 days): {_j(research.get('scorecard') or 'no closed trades yet')}
Lessons you wrote after past trading days (follow them): {_j(recent_lessons(team['id']) or 'none yet')}
Strategy ideas you wanted to test: {_j(notes.get('strategy_ideas') or 'none yet')}

{PLAN_FORMAT}"""
    return llm.ask_json("smart", f"{team['id']} manager", system, prompt)


def review(llm, team, todays_trades, holdings, blocked, overnight=None, discovery=None, calendar=None):
    system = (f"You are the Team Manager of the {team['name']} team. The market has closed. Review today's "
              "trades honestly, especially the ones that lost money, and figure out what went wrong so the team "
              "learns from its mistakes instead of repeating them.")
    prompt = f"""Your watchlist ({len(team['tickers'])} stocks): {', '.join(team['tickers'])}
Team focus: {team.get('focus') or team['name']}

Today's trades (entry, exit, reason, result):
{todays_trades or 'no trades today'}

Positions still open:
{holdings or 'none'}

Orders the risk rules blocked today:
{blocked or 'none'}

Your existing lessons:
{recent_lessons(team['id']) or 'none yet'}

News and filings since the close:
{overnight or 'none'}

What moved across the whole market today:
{discovery or 'not available'}

Upcoming earnings and economic events:
{calendar or 'not available'}

{REVIEW_FORMAT}"""
    out = llm.ask_json("smart", f"{team['id']} manager review", system, prompt)
    date = now_et().date().isoformat()
    lessons = read_json(lessons_path(team["id"]), [])
    for lesson in (out.get("lessons") or [])[:3]:
        lessons.append({"date": date, "lesson": lesson})
    write_json(lessons_path(team["id"]), lessons[-200:])
    write_json(STATE / "manager_notes" / f"{team['id']}.json",
               {"date": date, "strategy_ideas": (out.get("strategy_ideas") or [])[:2]})
    return out


# ---------- compact prompt pieces (keeps prompts small so free AI providers accept them) ----------
def _j(x):
    if isinstance(x, str):
        return x
    return json.dumps(x, separators=(",", ":"), default=str)


def _p(v):
    return f"{v * 100:+.1f}%" if isinstance(v, (int, float)) else "-"


def _intraday(view):
    if not view:
        return "not available"
    out = []
    for sym, v in view.items():
        parts = [f"{sym} ${v.get('price')}", f"day {_p(v.get('change_today'))}", f"open {_p(v.get('from_open'))}"]
        if v.get("since_last_look") is not None:
            parts.append(f"look {_p(v['since_last_look'])}")
        if v.get("vs_vwap") is not None:
            parts.append(f"vwap {_p(v['vs_vwap'])}")
        if v.get("opening_range"):
            lo, hi = v["opening_range"]
            parts.append(f"OR {lo}-{hi}" + (" BREAKOUT" if v.get("breakout_up") else " BREAKDOWN" if v.get("breakdown") else ""))
        if v.get("momentum_15m") is not None:
            parts.append(f"m15 {_p(v['momentum_15m'])}")
        if v.get("momentum_60m") is not None:
            parts.append(f"m60 {_p(v['momentum_60m'])}")
        if v.get("rel_volume") is not None:
            parts.append(f"rvol {v['rel_volume']}")
        if v.get("trend_5m"):
            parts.append(f"trend {v['trend_5m']}")
        out.append(", ".join(parts))
    return "\n".join(out)


def _tech(t):
    if not t:
        return "not available"
    return "\n".join(f"{s}: 5d {_p(v.get('change_5d'))}, 20d {_p(v.get('change_20d'))}, RSI {v.get('rsi_14')}, "
                     f"50d {'above' if v.get('above_50d_avg') else 'below'}, daily move {_p(v.get('daily_volatility'))}"
                     for s, v in t.items())


def _f1(f):
    keep = {k: f.get(k) for k in ("symbol", "sentiment", "importance", "unconfirmed_rumor") if f.get(k) is not None}
    for k in ("source", "market_wide", "affected"):
        if f.get(k):
            keep[k] = f[k]
    keep["summary"] = str(f.get("summary") or f.get("post") or "")[:160]
    if f.get("why"):
        keep["why"] = str(f["why"])[:100]
    return keep


def _findings(r, n):
    if not r:
        return "none"
    items = [_f1(f) for f in (r.get("findings") or [])[:n]]
    return _j({"overall": str(r.get("overall") or "")[:200], "urgent": r.get("urgent") or [], "findings": items})


def _mood(m):
    if not m:
        return "not available"
    return (f"{m.get('regime')} ({m.get('why')}); SPY {_p(m.get('spy'))}, QQQ {_p(m.get('qqq'))}, "
            f"fear gauge VIXY {_p(m.get('fear_vixy'))}, {_p(m.get('breadth')).lstrip('+')} of watched stocks up. "
            f"{m.get('note', '')}")
