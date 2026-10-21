# AI Trading Bot Team

A team of AI bots that paper trades stocks on Alpaca, learns from its mistakes, and reports to a dashboard.
It's built from the plan in the "AI Trading Bot Team" outline.

## How the team works

| Bot | What it does | Runs on |
|---|---|---|
| President | Reads the daily report. Sets tomorrow's strategy, each team's focus, money per team and stricter risk rules. | Gemini Pro (falls back to Flash) |
| Reporter | Writes the end-of-day report, keeps report history, feeds the dashboard. | Gemini Flash |
| Executive of Sector Teams / Special Teams | Passes orders and budgets down, summarizes their teams, flags problems. | Gemini Flash |
| Team Manager (1 per team) | Picks buys and sells with stop-loss and take-profit levels. After the close, it reviews losing trades and writes lessons. | Gemini Pro (falls back to Flash) |
| Team Investor (1 per team) | Checks every order against the risk rules, places it at Alpaca and records why. | Plain code, no AI |
| News Researcher (1 per team) | Reads Alpaca news and SEC filings, then rates each item. | Gemini Flash |
| Social Researcher (1 per team) | Reads Reddit and rates the crowd's mood. | Gemini Flash |

The risk rules in `config/settings.yaml` are enforced by code. The AI can only make them stricter.

## Trading all day (GitHub Actions, weekdays)

From the 9:30 AM open to the 4:00 PM close, the bots run in a loop:

- **Every 5 minutes:** check live prices, check stop-loss and take-profit levels, and read any new news articles. A news researcher rates new articles right away.
- **Every 30 minutes**, or sooner if there's big news or one of the team's stocks moves 2%: the manager takes a fresh look and can buy or sell.
- **Every 30 minutes:** the dashboard updates.
- **After the close:** manager reviews and lessons, then executives, the Reporter, and the President.

GitHub jobs can last at most 6 hours, so the day runs as two back-to-back sessions: open to 1 PM, and 1 PM to close.
You can change the timing in the `intraday:` part of `config/settings.yaml`. Set `close_positions_before_close: true` for pure day trading, so everything is sold before 4 PM.
The bots stop making new AI decisions after `max_ai_calls_per_day`, to stay in Gemini's free limit. Stop-losses keep working after that.

## Setup

1. **Secrets:** go to Settings, then Secrets and variables, then Actions, then New repository secret, and add:
   - `GEMINI_API_KEY`
   - `ALPACA_PAPER_KEY`
   - `ALPACA_PAPER_SECRET`
2. **Dashboard:** go to Settings, then Pages. Under Source, pick "Deploy from a branch", choose branch `main` and folder `/docs`, then Save.
3. **Test:** go to Actions, then Bot Team, then Run workflow, and pick `check`. It should finish green.
   Choosing `session` starts trading right away, if the market is open.
4. **Go-live protection (do this now):** go to Settings, then Environments, then New environment, and name it `live-trading`. Tick "Required reviewers" and add yourself.

## Everyday controls

- **Pause everything:** set `paused: true` in `config/settings.yaml`.
- **Turn teams on or off:** edit `config/teams.yaml`.
- **Change risk rules:** edit `config/settings.yaml`.
- **Real money:** go to Actions, then "Switch trading mode", choose `live`, type `GO LIVE`, then approve. First add the `ALPACA_LIVE_KEY` and `ALPACA_LIVE_SECRET` secrets. In live mode the bots only use up to `live.max_capital_usd`.

## Test without any keys

```
pip install -r requirements.txt
BOT_FAKE=1 BOT_FAKE_NOW=2026-10-21T10:45 python run.py morning
BOT_FAKE=1 BOT_FAKE_NOW=2026-10-21T17:30 python run.py evening
```

This isn't financial advice. Most trading bots don't beat simply holding an index fund, so treat the paper-trading phase as the real test.
