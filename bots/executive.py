"""Executives (Sector Teams and Special Teams).

Morning: pass the President's orders and budget down to each team (code).
Evening: summarize their teams' results for the Reporter and flag issues (AI).
"""
from .common import teams_config


def build_org():
    """Load teams and apply the overlap rule: special teams own shared companies."""
    cfg = teams_config()
    teams, owner_of, overlaps = {}, {}, []
    for kind in ("special", "sector"):
        for t in cfg.get(f"{kind}_teams", []):
            if not t.get("enabled"):
                continue
            tickers = []
            for s in [x.upper() for x in t.get("tickers", [])]:
                if s in owner_of:
                    overlaps.append({"symbol": s, "kept_by": owner_of[s], "skipped_by": t["id"]})
                    continue
                owner_of[s] = t["id"]
                tickers.append(s)
            if not tickers:
                continue
            teams[t["id"]] = {"id": t["id"], "name": t["name"], "kind": kind,
                              "focus": (t.get("focus") or "").strip(), "tickers": tickers}
    return teams, owner_of, overlaps


def orders_for_team(team, strategy, budget, limits):
    """What the executive hands the team manager each morning."""
    return {
        "president_strategy": strategy.get("strategy_notes", ""),
        "team_orders": strategy.get("team_focus", {}).get(team["id"], ""),
        "budget_usd": round(budget, 2),
        "rules": {k: limits[k] for k in ("max_position_pct", "max_open_positions_per_team",
                                         "max_trades_per_team_per_day", "min_conviction_to_buy",
                                         "min_stop_loss_pct", "max_stop_loss_pct")},
    }


EXEC_FORMAT = """Answer in JSON:
{
  "summary": "3-4 sentences on how your teams did today",
  "team_notes": {"team_id": "one sentence each"},
  "escalations": ["issues too big for a manager that the President should decide on"],
  "coordination": "anything about overlapping companies or teams stepping on each other, or empty"
}"""


def evening_summary(llm, kind, team_results, overlaps):
    if not team_results:
        return None
    title = "Executive of Special Teams" if kind == "special" else "Executive of Sector Teams"
    system = (f"You are the {title} on an AI stock trading team. You oversee several team managers, keep track "
              "of every action and result, make sure teams follow the President's rules and budgets, and handle "
              "problems too big for a manager but not important enough for the President.")
    prompt = (f"Today's results for your teams:\n{team_results}\n\nCompanies shared between teams "
              f"(special team keeps them):\n{overlaps}\n\n{EXEC_FORMAT}")
    out = llm.ask_json("fast", title, system, prompt)
    out["title"] = title
    return out
