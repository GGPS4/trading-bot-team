"""Runs the bot team.

    python run.py check     # test that your keys work (no trades)
    python run.py morning   # research -> managers plan -> investors trade
    python run.py midday    # quick news check, act only on important news
    python run.py evening   # reviews -> executives -> Reporter -> President -> dashboard

Add --force to run even when the market is closed (orders are still blocked
while the market is closed; useful for testing reports).
"""
import argparse
import os
import sys

from bots import journal
from bots.common import STATE, log, read_json, settings, today, write_json
from bots.executive import build_org, evening_summary, orders_for_team
from bots.investor import check_software_stops, execute
from bots.manager import plan as manager_plan, recent_lessons, review as manager_review
from bots.president import decide, load_strategy, normalize_allocations
from bots.reporter import daily_report, snapshot, write_dashboard
from bots.research import run_research
from bots.risk import RiskDesk, effective_limits


def clients(mode):
    if os.environ.get("BOT_FAKE") == "1":
        from bots.fakes import FakeBroker, FakeLLM
        return FakeBroker(), FakeLLM()
    from bots.broker import Alpaca
    from bots.llm import Gemini
    return Alpaca(mode), Gemini()


class Ctx:
    def __init__(self):
        self.cfg = settings()
        self.mode = self.cfg.get("mode", "paper")
        self.paused = bool(self.cfg.get("paused"))
        self.broker, self.llm = clients(self.mode)
        self.org, self.owner_of, self.overlaps = build_org()
        self.team_ids = list(self.org)
        self.strategy = load_strategy(self.team_ids)
        self.alloc = normalize_allocations(self.strategy.get("allocations", {}), self.team_ids)
        self.limits = effective_limits(self.cfg["risk"], self.strategy.get("risk_overrides"))
        self.account = self.broker.account()
        equity = float(self.account["equity"])
        self.capital = min(equity, float(self.cfg["live"]["max_capital_usd"])) if self.mode == "live" else equity
        spendable = self.capital * (1 - self.limits["cash_reserve_pct"])
        self.budgets = {t: self.alloc[t] * spendable for t in self.team_ids}
        log(f"mode={self.mode} capital=${self.capital:,.2f} teams={self.team_ids}"
            + (" PAUSED" if self.paused else ""))


def team_holdings(positions, owner_of, team_id):
    return [{"symbol": p["symbol"], "qty": p["qty"], "entry": p["avg_entry_price"],
             "price": p["current_price"], "pnl_pct": p["unrealized_plpc"]}
            for p in positions if owner_of.get(p["symbol"]) == team_id]


def worth_a_midday_look(research, holdings):
    news = (research.get("news") or {}).get("findings") or []
    if any((f.get("importance") or 0) >= 4 for f in news) or (research.get("news") or {}).get("urgent"):
        return True
    return any(abs(float(h["pnl_pct"] or 0)) >= 0.03 for h in holdings)


def trading_cycle(ctx: Ctx, phase: str, force: bool):
    b = ctx.broker
    clock = b.clock()
    market_open = bool(clock.get("is_open"))
    if not market_open and not force:
        log("Market is closed. Nothing to do.")
        return
    journal.sync(b)
    positions = b.positions()
    all_symbols = sorted(set(ctx.owner_of) | {p["symbol"] for p in positions})
    prices = b.latest_prices(all_symbols)
    stopped = check_software_stops(b, prices, ctx.paused, market_open)
    if stopped:
        positions = b.positions()

    entries = journal.load()
    trades_today = {}
    for e in entries:
        if e["date"] == today():
            trades_today[e["team"]] = trades_today.get(e["team"], 0) + 1
    desk = RiskDesk(ctx.limits, b.account(), ctx.capital, positions, ctx.owner_of, ctx.budgets,
                    trades_today, market_open, ctx.paused)

    for tid, team in ctx.org.items():
        try:
            research = run_research(ctx.llm, b, team, phase)
            holdings = team_holdings(positions, ctx.owner_of, tid)
            if phase == "midday" and not worth_a_midday_look(research, holdings):
                log(f"{tid}: nothing important since the morning; manager skips midday")
                continue
            orders = orders_for_team(team, ctx.strategy, ctx.budgets[tid], ctx.limits)
            p = manager_plan(ctx.llm, team, orders, research, holdings, phase)
            write_json(STATE / "plans" / f"{tid}.json", {"date": today(), "phase": phase, **p})
            results = execute(b, desk, team, p, prices)
            log(f"{tid}: {results or 'no trades'}")
        except Exception as e:
            log(f"{tid}: cycle failed, skipping this team: {e}")

    snap = snapshot(b, ctx.org, ctx.owner_of, ctx.budgets, ctx.mode)
    write_dashboard(snap)


def evening(ctx: Ctx, force: bool):
    b = ctx.broker
    if not b.is_trading_day(today()) and not force:
        log("Not a trading day. Nothing to report.")
        return
    entries = journal.sync(b)
    positions = b.positions()
    blocked = journal.blocked_on(today())
    reviews = {}
    for tid, team in ctx.org.items():
        mine = [e for e in journal.trades_on(today(), entries) if e["team"] == tid]
        try:
            reviews[tid] = manager_review(ctx.llm, team, mine, team_holdings(positions, ctx.owner_of, tid),
                                          [x for x in blocked if x["team"] == tid])
        except Exception as e:
            log(f"{tid}: review failed: {e}")

    snap = snapshot(b, ctx.org, ctx.owner_of, ctx.budgets, ctx.mode)
    execs = {}
    for kind in ("sector", "special"):
        results = {tid: {**snap["teams"][tid], "review": reviews.get(tid)}
                   for tid, t in ctx.org.items() if t["kind"] == kind}
        try:
            s = evening_summary(ctx.llm, kind, results, ctx.overlaps)
            if s:
                execs[kind] = s
        except Exception as e:
            log(f"{kind} executive failed: {e}")

    research_overview = {}
    for tid in ctx.org:
        r = read_json(STATE / "research" / f"{tid}.json", {})
        research_overview[tid] = {"news": (r.get("news") or {}).get("overall"),
                                  "social": (r.get("social") or {}).get("overall"),
                                  "findings": ((r.get("news") or {}).get("findings") or [])[:8]}

    report, new_strategy = None, None
    try:
        report = daily_report(ctx.llm, snap, reviews, execs, research_overview)
    except Exception as e:
        log(f"Reporter failed: {e}")
    if report:
        try:
            new_strategy = decide(ctx.llm, report, snap, execs, reviews, ctx.team_ids)
        except Exception as e:
            log(f"President failed, keeping yesterday's strategy: {e}")

    write_dashboard(snap, extra={
        "report": report, "president": new_strategy or ctx.strategy, "executives": execs, "reviews": reviews,
        "lessons": {tid: recent_lessons(tid)[-5:] for tid in ctx.org}, "research": research_overview,
    })
    log(f"Evening done. AI calls this run: {getattr(ctx.llm, 'calls', '?')}")


def check(ctx: Ctx):
    a = ctx.account
    log(f"Alpaca OK: {ctx.mode} account, equity ${float(a['equity']):,.2f}, cash ${float(a['cash']):,.2f}")
    log(f"Market open now: {ctx.broker.clock().get('is_open')}")
    for t in ctx.org.values():
        log(f"Team {t['id']}: {len(t['tickers'])} companies, budget ${ctx.budgets[t['id']]:,.2f}")
    for o in ctx.overlaps:
        log(f"Overlap: {o['symbol']} handled by {o['kept_by']}, skipped by {o['skipped_by']}")
    if hasattr(ctx.llm, "resolve"):
        for tier in ("fast", "smart"):
            log(f"Gemini {tier}: {ctx.llm.resolve(ctx.cfg['models'][tier])}")
    ans = ctx.llm.ask_json("fast", "check", "You are a test.", 'Reply with {"ok": true}')
    log(f"Gemini OK: {ans}")
    snap = snapshot(ctx.broker, ctx.org, ctx.owner_of, ctx.budgets, ctx.mode)
    write_dashboard(snap)
    log("Everything works. Dashboard data written.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("phase", choices=["check", "morning", "midday", "evening"])
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    ctx = Ctx()
    if args.phase == "check":
        check(ctx)
    elif args.phase in ("morning", "midday"):
        trading_cycle(ctx, args.phase, args.force)
    else:
        evening(ctx, args.force)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"FAILED: {e}")
        sys.exit(1)
