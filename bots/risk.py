"""Risk rules. Plain code, no AI.

Every order a bot wants to place passes through here first. If a rule
says no, the order is blocked and logged for the Reporter.
"""
import math

from .common import clamp, now_et, settings

TIGHTEN_ONLY_MAX = ("max_position_pct", "max_daily_loss_pct", "max_open_positions_per_team",
                    "max_trades_per_team_per_day", "max_stop_loss_pct")
TIGHTEN_ONLY_MIN = ("min_conviction_to_buy", "cash_reserve_pct")


def effective_limits(base: dict, overrides: dict | None) -> dict:
    """Apply the President's overrides, but only if they make rules stricter."""
    limits = dict(base)
    for k, v in (overrides or {}).items():
        if not isinstance(v, (int, float)):
            continue
        if k in TIGHTEN_ONLY_MAX:
            limits[k] = min(base[k], v)
        elif k in TIGHTEN_ONLY_MIN:
            limits[k] = max(base[k], v)
    limits["cash_reserve_pct"] = clamp(limits["cash_reserve_pct"], 0, 0.9)
    return limits


class RiskDesk:
    def __init__(self, limits, account, capital, positions, owner_of, budgets, trades_today, market_open, paused,
                 no_buys_after=None, blackouts=None):
        self.l = limits
        self.blackouts = blackouts or []            # [(start, end, label)] around big economic releases
        self.no_buys_after = no_buys_after          # "HH:MM" ET, or None
        self.capital = capital                      # dollars the bots are allowed to manage
        self.cash = float(account["cash"])
        self.equity = float(account["equity"])
        self.last_equity = float(account.get("last_equity") or account["equity"])
        self.positions = {p["symbol"]: p for p in positions}
        self.owner_of = owner_of                    # symbol -> team id
        self.budgets = budgets                      # team id -> dollars
        self.trades_today = dict(trades_today)      # team id -> count
        self.market_open = market_open
        self.paused = paused
        self.mood = None          # market mood (bots/mood.py), set by the runner
        self.sizing = settings().get("sizing", {})
        self.short_cfg = settings().get("shorting", {})

    # ---------- helpers ----------
    def day_change(self):
        return self.equity / self.last_equity - 1 if self.last_equity else 0.0

    def team_invested(self, team):
        return sum(abs(float(p["market_value"])) for s, p in self.positions.items() if self.owner_of.get(s) == team)

    def deploy_gap(self, team):
        """Dollars a team still has to put to work to meet the minimum-invested rule."""
        need = self.budgets.get(team, 0) * self.l.get("min_invested_pct", 0)
        return max(0.0, need - self.team_invested(team))

    def past_buy_cutoff(self):
        if not self.no_buys_after:
            return False
        hh, mm = (int(x) for x in self.no_buys_after.split(":"))
        n = now_et()
        return (n.hour, n.minute) >= (hh, mm)

    def team_open_count(self, team):
        return sum(1 for s in self.positions if self.owner_of.get(s) == team)

    def total_invested(self):
        return sum(abs(float(p["market_value"])) for p in self.positions.values())

    # ---------- checks ----------
    def team_short_exposure(self, team):
        return sum(abs(float(p["market_value"])) for s, p in self.positions.items()
                   if self.owner_of.get(s) == team and float(p.get("qty", 1)) < 0)

    def check_buy(self, team, symbol, price, conviction, stop_pct, tp_pct, deploying=False):
        return self.check_entry(team, symbol, price, conviction, stop_pct, tp_pct, "long", deploying)

    def check_entry(self, team, symbol, price, conviction, stop_pct, tp_pct, side="long", deploying=False,
                    volatility=None, shortable=True):
        """Returns (ok, reason, plan). plan has qty or notional, stop and first-target prices.
        side: "long" (buy) or "short" (sell borrowed shares, profit if the price falls).
        deploying=True: an entry made to meet the minimum-invested rule (accepts conviction 2+).
        volatility: the stock's typical daily move (fraction); jumpier stocks get wider stops and
        smaller positions so every trade risks about the same number of dollars."""
        l, mood = self.l, self.mood or {}
        regime = mood.get("regime", "normal")
        if self.paused:
            return False, "trading is paused in settings", None
        if not self.market_open:
            return False, "market is closed", None
        if self.past_buy_cutoff():
            return False, f"no new day trades after {self.no_buys_after}", None
        n = now_et()
        for start, end, label in self.blackouts:
            if start <= n <= end:
                return False, f"new trades paused around {label} ({start:%I:%M}-{end:%I:%M %p})", None
        if self.owner_of.get(symbol) != team:
            return False, f"{symbol} is not on this team's list", None
        if self.day_change() <= -l["max_daily_loss_pct"]:
            return False, f"daily loss limit hit ({self.day_change():.1%})", None
        if self.trades_today.get(team, 0) >= l["max_trades_per_team_per_day"]:
            return False, "team used all of today's trades", None
        if symbol in self.positions:
            return False, "already holding this stock", None
        if self.team_open_count(team) >= l["max_open_positions_per_team"]:
            return False, "team already has the maximum number of positions", None
        min_conv = min(2, l["min_conviction_to_buy"]) if deploying else l["min_conviction_to_buy"]
        if side == "long" and regime == "risk_off":
            min_conv += 1  # weak market: longs need a stronger reason
        if (conviction or 0) < min_conv:
            return False, f"conviction {conviction} is below the minimum {min_conv}" + \
                (" (risk-off market)" if side == "long" and regime == "risk_off" else ""), None
        if not price or price <= 0:
            return False, "no current price", None

        short_cfg = self.short_cfg or {}
        if side == "short":
            if not short_cfg.get("enabled"):
                return False, "short selling is turned off", None
            if not shortable:
                return False, "broker can't borrow this stock for shorting", None

        # Stop distance: the manager's choice, but at least a slice of the stock's normal daily move.
        sz = self.sizing or {}
        stop_pct = stop_pct or l["default_stop_loss_pct"]
        if volatility:
            stop_pct = max(stop_pct, sz.get("stop_vs_volatility", 0.35) * volatility)
        stop_pct = clamp(stop_pct, l["min_stop_loss_pct"], l["max_stop_loss_pct"])
        tp_pct = clamp(tp_pct or l["default_take_profit_pct"], stop_pct, 0.5)  # aim to win at least what we risk

        budget = self.budgets.get(team, 0)
        mult = 1.0
        if side == "long":
            mult = {"caution": 0.75, "risk_off": 0.5}.get(regime, 1.0)
        risk_dollars = budget * sz.get("risk_per_trade_pct", 0.004) * mult
        room_team = budget - self.team_invested(team)
        room_capital = self.capital * (1 - l["cash_reserve_pct"]) - self.total_invested()
        room_cash = self.cash - self.capital * l["cash_reserve_pct"]
        caps = [risk_dollars / stop_pct, budget * l["max_position_pct"] * mult, room_team, room_capital, room_cash]
        if side == "short":
            caps.append(budget * short_cfg.get("max_short_pct_of_budget", 0.5) - self.team_short_exposure(team))
        dollars = min(caps)
        if dollars < 1:
            return False, "no budget left for this team", None

        if side == "long":
            plan = {"side": "long", "stop_pct": stop_pct, "tp_pct": tp_pct,
                    "stop_price": round(price * (1 - stop_pct), 2), "take_profit_price": round(price * (1 + tp_pct), 2)}
        else:
            plan = {"side": "short", "stop_pct": stop_pct, "tp_pct": tp_pct,
                    "stop_price": round(price * (1 + stop_pct), 2), "take_profit_price": round(price * (1 - tp_pct), 2)}
        qty = math.floor(dollars / price)
        if qty >= 1:
            plan.update(kind="shares", qty=qty, dollars=round(qty * price, 2))
        elif side == "long" and l.get("allow_fractional_shares"):
            plan.update(kind="fractional", qty=round(dollars / price, 6), dollars=round(dollars, 2))
        else:
            return False, f"one share costs ${price:.2f}, more than the ${dollars:.2f} allowed", None
        plan["risk_dollars"] = round(plan["dollars"] * stop_pct, 2)

        self.trades_today[team] = self.trades_today.get(team, 0) + 1
        self.cash -= plan["dollars"]
        self.positions[symbol] = {"symbol": symbol, "market_value": plan["dollars"],
                                  "qty": plan["qty"] if side == "long" else -plan["qty"]}
        return True, "ok", plan

    def check_sell(self, team, symbol):
        if self.paused:
            return False, "trading is paused in settings"
        if not self.market_open:
            return False, "market is closed"
        if symbol not in self.positions:
            return False, "not holding this stock"
        if self.owner_of.get(symbol) != team:
            return False, "another team owns this position"
        return True, "ok"
