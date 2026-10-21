"""Runs the bot team.

    python run.py check                  # test that your keys work (no trades)
    python run.py session --until 16:00  # trade all day: watch, re-plan, trade until that time (ET)
    python run.py morning / midday       # single trading passes (older, still work)
    python run.py evening                # reviews -> executives -> Reporter -> President -> dashboard
    python run.py watch --minutes 330    # after hours: keep reading news, filings, Truth Social / X

Add --force to run even when the market is closed (orders are still blocked
while the market is closed; useful for testing reports).
"""
import argparse
import datetime as dt
import os
import subprocess
import sys
import time

from bots import journal
from bots.common import ET, STATE, log, now_et, read_json, settings, today, write_json
from bots.executive import build_org, evening_summary, orders_for_team
from bots.investor import _sell, check_software_stops, execute
from bots.manager import plan as manager_plan, recent_lessons, review as manager_review
from bots.president import decide, load_strategy, normalize_allocations
from bots.reporter import daily_report, snapshot, write_dashboard
from bots import market_watch
from bots import llm as llm_mod
from bots.research import after_hours_news, fresh_news, load_seen, run_research, save_seen
from bots.risk import RiskDesk, effective_limits


def clients(mode):
    if os.environ.get("BOT_FAKE") == "1":
        from bots.fakes import FakeBroker, FakeLLM
        return FakeBroker(), FakeLLM()
    from bots.broker import Alpaca
    from bots.llm import Router
    return Alpaca(mode), Router()


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


def log_plan(tid, trigger, plan, results):
    """Keep a history of every look a manager took, for the team pages on the dashboard."""
    path = STATE / "plans" / f"{tid}_log.json"
    rows = read_json(path, [])
    rows.append({"date": today(), "time": now_et().strftime("%H:%M"), "trigger": trigger,
                 "market_view": plan.get("market_view", ""), "experiment": plan.get("experiment", ""),
                 "decisions": [{k: d.get(k) for k in ("symbol", "action", "conviction", "reason",
                                                       "stop_loss_pct", "take_profit_pct")}
                               for d in (plan.get("decisions") or [])],
                 "results": results})
    write_json(path, rows[-400:])


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
            log_plan(tid, phase, p, results)
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
    if (STATE / "reports" / f"{today()}.json").exists() and not force:
        log("Today's evening report is already done.")
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

    mw = read_json(STATE / "research" / "_market_watch.json", {})
    research_overview["political_and_social_media"] = {"overall": mw.get("overall"),
                                                       "findings": (mw.get("findings") or [])[:8]}
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


# ------------------------------------------------------------------ all-day trading
def save_progress(msg):
    """On GitHub, commit the bots' memory + dashboard so the website updates during the day."""
    if os.environ.get("BOT_GIT_PUSH") != "1":
        return
    try:
        subprocess.run(["git", "add", "state", "docs/data"], check=True)
        if subprocess.run(["git", "diff", "--cached", "--quiet"]).returncode == 0:
            return
        subprocess.run(["git", "commit", "-qm", msg], check=True)
        subprocess.run(["git", "pull", "--rebase", "-q"], check=True)
        subprocess.run(["git", "push", "-q"], check=True)
        log("Saved progress to GitHub (dashboard updated).")
    except subprocess.CalledProcessError as e:
        log(f"Could not save progress: {e}")


def _parse_time(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(ET)


def intraday_view(snaps, last_prices):
    view = {}
    for sym, v in snaps.items():
        p = v["price"]
        view[sym] = {
            "price": round(p, 2),
            "change_today": round(p / v["prev_close"] - 1, 4) if v.get("prev_close") else None,
            "from_open": round(p / v["open"] - 1, 4) if v.get("open") else None,
            "day_high": v.get("high"), "day_low": v.get("low"),
            "since_last_look": round(p / last_prices[sym] - 1, 4) if last_prices.get(sym) else None,
        }
    return view


def session(ctx: Ctx, until: str, force: bool):
    """Watch the market from open until `until` (ET), trading whenever there's a reason."""
    b, cfg = ctx.broker, ctx.cfg.get("intraday", {})
    every = max(1, cfg.get("check_every_minutes", 5)) * 60
    replan = max(5, cfg.get("replan_every_minutes", 30)) * 60
    trigger = cfg.get("price_move_trigger_pct", 0.02)
    hh, mm = (int(x) for x in until.split(":"))
    end = now_et().replace(hour=hh, minute=mm, second=0, microsecond=0)
    fake = os.environ.get("BOT_FAKE") == "1"

    # 1. wait for the opening bell (if we started a bit early)
    while not force and not fake:
        clock = b.clock()
        if clock.get("is_open"):
            break
        nxt = _parse_time(clock["next_open"]) if clock.get("next_open") else None
        if not nxt or nxt.date() != now_et().date() or nxt >= end:
            log("Market isn't open again before this session ends. Nothing to do.")
            return
        wait = (nxt - now_et()).total_seconds()
        log(f"Waiting {wait / 60:.0f} min for the market to open...")
        time.sleep(min(300, max(5, wait)))

    # 2. full research at the start of the session
    research, seen = {}, load_seen()
    try:
        market_watch.check(ctx.llm, ctx.org)
    except Exception as e:
        log(f"Market watch failed: {e}")
    for tid, team in ctx.org.items():
        research[tid] = run_research(ctx.llm, b, team, "morning")
        research[tid]["market_watch"] = market_watch.for_team(team)
    last_plan = {tid: 0.0 for tid in ctx.org}
    last_plan_prices = {tid: {} for tid in ctx.org}
    last_save, last_social, closed_for_day, ticks = time.time(), time.time(), False, 0

    # 3. the all-day loop
    while now_et() < end:
        started = time.time()
        ticks += 1
        is_open = bool(b.clock().get("is_open"))
        if not is_open and not force:
            log("Market closed.")
            break
        try:
            journal.sync(b)
            positions = b.positions()
            all_symbols = sorted(set(ctx.owner_of) | {p["symbol"] for p in positions})
            snaps = b.snapshots(all_symbols)
            prices = {s: v["price"] for s, v in snaps.items()}
            if check_software_stops(b, prices, ctx.paused, is_open):
                positions = b.positions()

            n = now_et()
            mins_left = 16 * 60 - (n.hour * 60 + n.minute)
            if cfg.get("close_positions_before_close") and mins_left <= 10 and not closed_for_day:
                for p in positions:
                    if ctx.owner_of.get(p["symbol"]) and not ctx.paused:
                        _sell(b, ctx.owner_of[p["symbol"]], p["symbol"], "end of day", "end_of_day")
                closed_for_day = True
                log("Closed all positions before the bell.")
                positions = b.positions()

            trades_today = {}
            for e in journal.load():
                if e["date"] == today():
                    trades_today[e["team"]] = trades_today.get(e["team"], 0) + 1
            desk = RiskDesk(ctx.limits, b.account(), ctx.capital, positions, ctx.owner_of, ctx.budgets,
                            trades_today, is_open, ctx.paused or closed_for_day)

            refresh_social = time.time() - last_social > 2 * 3600
            try:
                watch_new = market_watch.check(ctx.llm, ctx.org)
            except Exception as e:
                log(f"Market watch failed: {e}")
                watch_new = []
            for tid, team in ctx.org.items():
                try:
                    r = research[tid]
                    if refresh_social:
                        from bots.research import social_researcher
                        r["social"] = social_researcher(ctx.llm, team)
                    new = fresh_news(ctx.llm, b, team, r, seen)
                    r["market_watch"] = market_watch.for_team(team)
                    hot_posts = [f for f in watch_new if (f.get("importance") or 0) >= 4 and
                                 (f.get("market_wide") or set(f.get("affected") or []) & set(team["tickers"]))]
                    r["intraday"] = intraday_view({s: snaps[s] for s in team["tickers"] if s in snaps},
                                                  last_plan_prices[tid])
                    moved = [s for s, v in r["intraday"].items()
                             if v["since_last_look"] is not None and abs(v["since_last_look"]) >= trigger]
                    urgent = (r.get("news") or {}).get("urgent") or [f["symbol"] for f in new
                                                                      if (f.get("importance") or 0) >= 4]
                    due = time.time() - last_plan[tid] >= replan
                    if closed_for_day or not (due or (new and urgent) or moved or hot_posts):
                        continue
                    why = ("scheduled look" if due else f"big news on {urgent}" if new and urgent
                           else "market-moving post: " + hot_posts[0].get("post", "")[:80] if hot_posts
                           else f"price moves in {moved}")
                    log(f"{tid}: manager takes a look ({why})")
                    holdings = team_holdings(positions, ctx.owner_of, tid)
                    orders = orders_for_team(team, ctx.strategy, ctx.budgets[tid], ctx.limits)
                    p = manager_plan(ctx.llm, team, orders, r, holdings, "intraday")
                    last_plan[tid] = time.time()
                    last_plan_prices[tid] = {s: snaps[s]["price"] for s in team["tickers"] if s in snaps}
                    write_json(STATE / "plans" / f"{tid}.json",
                               {"date": today(), "time": now_et().strftime("%H:%M"), "trigger": why, **p})
                    results = execute(b, desk, team, p, prices)
                    log_plan(tid, why, p, results)
                    log(f"{tid}: {results or 'no trades'}")
                except Exception as e:
                    log(f"{tid}: skipped this check: {e}")
            if refresh_social:
                last_social = time.time()
            save_seen(seen)
        except Exception as e:
            log(f"Check failed, will retry: {e}")

        if time.time() - last_save >= 1800:
            write_dashboard(snapshot(b, ctx.org, ctx.owner_of, ctx.budgets, ctx.mode))
            save_progress(f"Bot team: intraday update {now_et():%Y-%m-%d %H:%M}")
            last_save = time.time()
        if fake and ticks >= int(os.environ.get("BOT_FAKE_TICKS", "3")):
            break
        time.sleep(0 if fake else max(5, every - (time.time() - started)))

    write_dashboard(snapshot(b, ctx.org, ctx.owner_of, ctx.budgets, ctx.mode))
    save_progress(f"Bot team: session end {now_et():%Y-%m-%d %H:%M}")
    log(f"Session done. AI calls today: {__import__('bots.llm', fromlist=['calls_today']).calls_today()}")


def watch(ctx: Ctx, minutes: int):
    """After hours: keep reading news, filings and political/social posts. No trading."""
    llm_mod.BUCKET["name"] = "after_hours"
    b = ctx.broker
    every = max(5, ctx.cfg.get("after_hours", {}).get("check_every_minutes", 15)) * 60
    stop_at = time.time() + minutes * 60
    seen, checks, last_save = load_seen(), 0, time.time()
    fake = os.environ.get("BOT_FAKE") == "1"
    log(f"After-hours watch for {minutes} minutes")
    while time.time() < stop_at:
        started = time.time()
        checks += 1
        try:
            if b.clock().get("is_open") and not fake:
                log("The market is open; the trading session takes over.")
                break
        except Exception:
            pass
        try:
            market_watch.check(ctx.llm, ctx.org)
        except Exception as e:
            log(f"Market watch failed: {e}")
        for tid, team in ctx.org.items():
            try:
                after_hours_news(ctx.llm, b, team, seen, with_filings=(checks % 4 == 1))
            except Exception as e:
                log(f"{tid}: after-hours research failed: {e}")
        save_seen(seen)
        if time.time() - last_save >= 3600:
            write_dashboard(snapshot(b, ctx.org, ctx.owner_of, ctx.budgets, ctx.mode))
            save_progress(f"Bot team: after-hours research {now_et():%Y-%m-%d %H:%M}")
            last_save = time.time()
        if fake and checks >= int(os.environ.get("BOT_FAKE_TICKS", "2")):
            break
        time.sleep(0 if fake else max(5, min(every - (time.time() - started), stop_at - time.time())))
    write_dashboard(snapshot(b, ctx.org, ctx.owner_of, ctx.budgets, ctx.mode))
    save_progress(f"Bot team: after-hours research {now_et():%Y-%m-%d %H:%M}")
    log(f"Watch done. After-hours AI calls today: {llm_mod.calls_today()}")


def auto(ctx: Ctx):
    """Decide what to do right now. Used by the self-restarting GitHub chain:
    trade while the market is open, write the evening report after the close,
    and keep researching the rest of the time."""
    b, now = ctx.broker, now_et()
    clock = b.clock()
    next_open = _parse_time(clock["next_open"])
    next_close = _parse_time(clock["next_close"])
    max_job = dt.timedelta(minutes=320)  # GitHub jobs can run at most 6 hours
    if clock.get("is_open") or (next_open - now) <= dt.timedelta(minutes=20):
        end = min(next_close, now + max_job)
        log(f"Auto: trading session until {end:%H:%M} ET")
        session(ctx, end.strftime("%H:%M"), False)
        if now_et().hour >= 16 and not b.clock().get("is_open") and b.is_trading_day(today()):
            evening(ctx, False)
        return
    if b.is_trading_day(today()) and now.hour >= 16:
        evening(ctx, False)
    minutes = int(min(max_job, next_open - now_et() - dt.timedelta(minutes=8)).total_seconds() // 60)
    log(f"Auto: after-hours research for {max(10, minutes)} minutes")
    watch(ctx, max(10, minutes))


def check(ctx: Ctx):
    a = ctx.account
    log(f"Alpaca OK: {ctx.mode} account, equity ${float(a['equity']):,.2f}, cash ${float(a['cash']):,.2f}")
    log(f"Market open now: {ctx.broker.clock().get('is_open')}")
    for t in ctx.org.values():
        log(f"Team {t['id']}: {len(t['tickers'])} companies, budget ${ctx.budgets[t['id']]:,.2f}")
    for o in ctx.overlaps:
        log(f"Overlap: {o['symbol']} handled by {o['kept_by']}, skipped by {o['skipped_by']}")
    if hasattr(ctx.llm, "test"):
        for name, status in ctx.llm.test().items():
            log(f"AI provider {name}: {status}")
    else:
        log(f"AI OK: {ctx.llm.ask_json('fast', 'check', 'You are a test.', 'Reply with {\"ok\": true}')}")
    snap = snapshot(ctx.broker, ctx.org, ctx.owner_of, ctx.budgets, ctx.mode)
    write_dashboard(snap)
    log("Everything works. Dashboard data written.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("phase", choices=["auto", "check", "session", "watch", "morning", "midday", "evening"])
    ap.add_argument("--minutes", type=int, default=330, help="how long an after-hours watch runs")
    ap.add_argument("--until", default="16:00", help="session end time, ET (HH:MM)")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    ctx = Ctx()
    if args.phase == "check":
        check(ctx)
    elif args.phase == "auto":
        auto(ctx)
    elif args.phase == "watch":
        watch(ctx, args.minutes)
    elif args.phase == "session":
        session(ctx, args.until, args.force)
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
