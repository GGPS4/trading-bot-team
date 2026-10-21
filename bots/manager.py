"""Team Managers: the day-to-day strategists.

plan():   decide what to buy and sell, with stop-loss and take-profit levels.
review(): after the close, study the losing trades and write lessons so
          the team doesn't repeat its mistakes.
"""
from .common import STATE, now_et, read_json, settings, write_json
from .scorecard import SETUPS

PLAN_FORMAT = """Answer in JSON:
{
  "market_view": "2-3 sentences on what you see today",
  "decisions": [
    {"symbol": "TICKER", "action": "BUY" or "SELL",
     "conviction": 1-5, "stop_loss_pct": 0.05, "take_profit_pct": 0.10, "setup": "one setup name (BUY only)",
     "reason": "why, citing the research or price data"}
  ],
  "ranked_ideas": [
    {"symbol": "TICKER", "conviction": 1-5, "stop_loss_pct": 0.015, "take_profit_pct": 0.03,
     "setup": "one setup name", "reason": "short reason"}
  ],
  "experiment": "which strategy idea you are testing today, or empty",
  "watchlist_changes": {
    "add": [{"symbol": "TICKER", "reason": "why it belongs on the watchlist now (news, unusual move, volume)"}],
    "remove": [{"symbol": "TICKER", "reason": "why it no longer deserves a spot (quiet, no catalyst)"}]
  }
}
Only list stocks you want to BUY (not already held) or SELL (currently held). Everything else is a hold.
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
            "with volume. Use tight stops (about 1-2%) and take profits quickly (about 2-4%). Rotate money: "
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
Event calendar (earnings for your stocks, big US economic releases):
{research.get('calendar') or 'not available'}
Market discovery (whole market, for finding new watchlist names):
{research.get('discovery') or 'not available'}
Money: {orders.get('deployment', '')}

Orders from your executive (from the President):
{orders}

Current positions held by your team:
{holdings or 'none'}

News researcher findings:
{research.get('news')}

Social media researcher findings (Reddit):
{research.get('social')}

Political & social media watch (Truth Social, X), posts that may move your stocks or the whole market:
{research.get('market_watch') or 'nothing notable'}

After-hours / overnight findings (news and filings since the last close):
{research.get('overnight') or 'none'}

Live prices and day-trading signals right now (fractions unless noted):
  change_today vs yesterday's close, from_open vs today's open, since_last_look vs your previous check;
  vwap = today's volume-weighted average price, vs_vwap = price above(+)/below(-) it;
  opening_range = [low, high] of the first 30 minutes, breakout_up / breakdown = price outside it;
  momentum_15m / momentum_60m = change over the last 15 / 60 minutes; trend_5m from recent 5-minute bars;
  rel_volume = today's volume vs a normal day at this hour (2.0 = twice normal; high volume confirms a move).
  Good long setups usually have price above VWAP, an opening-range breakout or strong 15m momentum, and
  rel_volume above 1.5. Be wary of buying far above VWAP after a big run with fading momentum.
{research.get('intraday') or 'not available'}

Daily price history (change_* are fractions, rsi_14 > 70 = overbought, < 30 = oversold):
{research.get('technicals')}

Your scorecard (your closed trades over the last 30 days, by setup and by experiment):
{research.get('scorecard') or 'no closed trades yet'}

Lessons you wrote after past trading days (follow them):
{recent_lessons(team['id']) or 'none yet'}

Strategy ideas you wanted to test:
{notes.get('strategy_ideas') or 'none yet'}

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
