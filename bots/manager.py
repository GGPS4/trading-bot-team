"""Team Managers: the day-to-day strategists.

plan():   decide what to buy and sell, with stop-loss and take-profit levels.
review(): after the close, study the losing trades and write lessons so
          the team doesn't repeat its mistakes.
"""
from .common import STATE, now_et, read_json, settings, write_json

PLAN_FORMAT = """Answer in JSON:
{
  "market_view": "2-3 sentences on what you see today",
  "decisions": [
    {"symbol": "TICKER", "action": "BUY" or "SELL",
     "conviction": 1-5, "stop_loss_pct": 0.05, "take_profit_pct": 0.10,
     "reason": "why, citing the research or price data"}
  ],
  "experiment": "which strategy idea you are testing today, or empty"
}
Only list stocks you want to BUY (not already held) or SELL (currently held). Everything else is a hold.
Only use symbols from the team's list. It is fine to return no decisions if nothing is convincing.
stop_loss_pct and take_profit_pct are fractions of the buy price (0.05 = 5%)."""

REVIEW_FORMAT = """Answer in JSON:
{
  "what_went_well": "1-2 sentences",
  "what_went_wrong": "1-2 sentences, be specific about losing trades",
  "lessons": ["short, specific rules for yourself, e.g. 'Don't buy right after a 10% jump on earnings day'"],
  "strategy_ideas": ["new strategies or tweaks to test in the coming days"]
}
Give at most 3 lessons and 2 ideas. Only write a lesson if today's trades actually taught one."""


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
    timing = ("It is the morning session, shortly after the market opened." if phase == "morning" else
              "It is the midday check. Only act if new, important information came in since the morning.")
    prompt = f"""{timing}
Team companies: {', '.join(team['tickers'])}

Orders from your executive (from the President):
{orders}

Current positions held by your team:
{holdings or 'none'}

News researcher findings:
{research.get('news')}

Social media researcher findings:
{research.get('social')}

Price data (change_* are fractions, rsi_14 > 70 = overbought, < 30 = oversold):
{research.get('technicals')}

Lessons you wrote after past trading days (follow them):
{recent_lessons(team['id']) or 'none yet'}

Strategy ideas you wanted to test:
{notes.get('strategy_ideas') or 'none yet'}

{PLAN_FORMAT}"""
    return llm.ask_json("smart", f"{team['id']} manager", system, prompt)


def review(llm, team, todays_trades, holdings, blocked):
    system = (f"You are the Team Manager of the {team['name']} team. The market has closed. Review today's "
              "trades honestly, especially the ones that lost money, and figure out what went wrong so the team "
              "learns from its mistakes instead of repeating them.")
    prompt = f"""Today's trades (entry, exit, reason, result):
{todays_trades or 'no trades today'}

Positions still open:
{holdings or 'none'}

Orders the risk rules blocked today:
{blocked or 'none'}

Your existing lessons:
{recent_lessons(team['id']) or 'none yet'}

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
