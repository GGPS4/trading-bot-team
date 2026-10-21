"""The President: top of the bot tree.

Reads the Reporter's daily report and decides tomorrow's strategy, each
team's focus, how much money each team gets, and whether rules should be
stricter. Also says whether it thinks the team is ready for real money,
but only a human can actually switch to live trading.
"""
from .common import STATE, clamp, now_et, read_json, settings, write_json
from .risk import effective_limits

STRATEGY = STATE / "strategy.json"

FORMAT = """Answer in JSON:
{
  "strategy_notes": "3-5 sentences: the overall plan every team should follow tomorrow",
  "team_focus": {"team_id": "specific orders for that team's manager"},
  "allocations": {"team_id": 0.5},
  "risk_overrides": {"max_position_pct": 0.15},
  "go_live_assessment": {"ready": false, "reason": "why or why not"},
  "decision_log": "1-2 sentences on what you changed today and why"
}
allocations: share of the money each team gets; they must add up to 1.0. Give more to teams that are
performing well, but don't starve a team after one bad day.
risk_overrides: optional. You may only make rules STRICTER than the current limits (code ignores anything looser).
Use only these keys: max_position_pct, max_daily_loss_pct, max_open_positions_per_team,
max_trades_per_team_per_day, min_conviction_to_buy, cash_reserve_pct, max_stop_loss_pct.
Leave it empty {} to use the normal limits."""


def load_strategy(team_ids):
    s = read_json(STRATEGY)
    if not s:
        s = {"strategy_notes": "First day. Trade carefully, only on clear signals, and build a record to learn from.",
             "team_focus": {}, "allocations": {}, "risk_overrides": {}}
    return s


def normalize_allocations(alloc, team_ids):
    """Every enabled team gets 5%-60%, total = 100%. New teams start with an equal share."""
    if not team_ids:
        return {}
    equal = 1 / len(team_ids)
    if any(t not in alloc for t in team_ids):
        alloc = {}  # a team was added or removed: start everyone from an equal split
    raw = {t: float(alloc.get(t, equal) or equal) for t in team_ids}
    lo, hi = (0.05, 0.6) if len(team_ids) > 1 else (1.0, 1.0)
    raw = {t: clamp(v, lo, hi) for t, v in raw.items()}
    total = sum(raw.values())
    return {t: round(v / total, 4) for t, v in raw.items()}


def decide(llm, report, snap, exec_summaries, reviews, team_ids):
    current = load_strategy(team_ids)
    base = settings()["risk"]
    past = read_json(STATE / "reports" / "index.json", [])[-20:]
    system = ("You are the President, the highest level of an AI stock trading team. Each evening you read the "
              "Reporter's report and decide the strategy, what each team should look for, how much money each team "
              "gets, and whether the risk rules should be stricter. You only handle the most important decisions. "
              "You think long-term: protecting the money matters more than chasing a big day.")
    prompt = f"""Treat this money as real. Every decision counts.

Reporter's report:
{report}

Key numbers: account {snap['account']}, performance {snap['performance']}
Teams: {snap['teams']}
Go-live checklist (code-checked): {snap['go_live']}

Executive summaries and escalations: {exec_summaries}
Manager reviews: {reviews}

Report history (last 20 days): {past}

Your current strategy: {current}
Normal risk limits: {base}
Team ids: {team_ids}

{FORMAT}"""
    out = llm.ask_json("smart", "president", system, prompt)
    new = {
        "updated": now_et().isoformat(timespec="minutes"),
        "strategy_notes": out.get("strategy_notes") or current.get("strategy_notes"),
        "team_focus": {t: v for t, v in (out.get("team_focus") or {}).items() if t in team_ids},
        "allocations": normalize_allocations(out.get("allocations") or current.get("allocations") or {}, team_ids),
        "risk_overrides": {k: v for k, v in effective_limits(base, out.get("risk_overrides")).items()
                           if v != base.get(k)},  # only rules that got stricter
        "go_live_assessment": out.get("go_live_assessment") or {},
        "decision_log": out.get("decision_log", ""),
    }
    write_json(STRATEGY, new)
    log = read_json(STATE / "strategy_log.json", [])
    log.append({"date": snap["date"], "decision": new["decision_log"], "allocations": new["allocations"],
                "risk_overrides": new["risk_overrides"]})
    write_json(STATE / "strategy_log.json", log[-365:])
    return new
