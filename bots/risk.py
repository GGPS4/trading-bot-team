"""Risk rules. Plain code, no AI.

Every order a bot wants to place passes through here first. If a rule
says no, the order is blocked and logged for the Reporter.
"""
import math

from .common import clamp, now_et

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
                 no_buys_after=None):
        self.l = limits
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
    def check_buy(self, team, symbol, price, conviction, stop_pct, tp_pct, deploying=False):
        """Returns (ok, reason, plan). plan has qty or notional, stop and take-profit prices.
        deploying=True: a buy made to meet the minimum-invested rule (accepts conviction 2+)."""
        l = self.l
        if self.paused:
            return False, "trading is paused in settings", None
        if not self.market_open:
            return False, "market is closed", None
        if self.past_buy_cutoff():
            return False, f"no new day trades after {self.no_buys_after}", None
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
        if (conviction or 0) < (min(2, l["min_conviction_to_buy"]) if deploying else l["min_conviction_to_buy"]):
            return False, f"conviction {conviction} is below the minimum {l['min_conviction_to_buy']}", None
        if not price or price <= 0:
            return False, "no current price", None

        stop_pct = clamp(stop_pct or l["default_stop_loss_pct"], l["min_stop_loss_pct"], l["max_stop_loss_pct"])
        tp_pct = clamp(tp_pct or l["default_take_profit_pct"], stop_pct, 0.5)  # aim to win at least what we risk

        budget = self.budgets.get(team, 0)
        room_team = budget - self.team_invested(team)
        room_capital = self.capital * (1 - l["cash_reserve_pct"]) - self.total_invested()
        room_cash = self.cash - self.capital * l["cash_reserve_pct"]
        dollars = min(budget * l["max_position_pct"], room_team, room_capital, room_cash)
        if dollars < 1:
            return False, "no budget left for this team", None

        plan = {"stop_pct": stop_pct, "tp_pct": tp_pct,
                "stop_price": round(price * (1 - stop_pct), 2),
                "take_profit_price": round(price * (1 + tp_pct), 2)}
        qty = math.floor(dollars / price)
        if qty >= 1:
            plan.update(kind="bracket", qty=qty, dollars=round(qty * price, 2))
        elif l.get("allow_fractional_shares"):
            plan.update(kind="fractional", qty=round(dollars / price, 6), dollars=round(dollars, 2))
        else:
            return False, f"one share costs ${price:.2f}, more than the ${dollars:.2f} allowed", None

        self.trades_today[team] = self.trades_today.get(team, 0) + 1
        self.cash -= plan["dollars"]
        self.positions[symbol] = {"symbol": symbol, "market_value": plan["dollars"]}
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
