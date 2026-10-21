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

## Daily schedule (GitHub Actions, weekdays)

- **Morning (~10:45 AM ET):** research, then managers plan, then investors trade.
- **Midday (~1:30 PM ET):** a news check. Managers act only on important news.
- **Evening (~5:40 PM ET):** manager reviews and lessons, then executives, the Reporter, the President, and the dashboard.

GitHub sometimes starts scheduled runs 5–30 minutes late. That's normal.

## Setup

1. **Secrets:** go to Settings, then Secrets and variables, then Actions, then New repository secret, and add:
   - `GEMINI_API_KEY`
   - `ALPACA_PAPER_KEY`
   - `ALPACA_PAPER_SECRET`
2. **Dashboard:** go to Settings, then Pages. Under Source, pick "Deploy from a branch", choose branch `main` and folder `/docs`, then Save.
3. **Test:** go to Actions, then Bot Team, then Run workflow, and pick `check`. It should finish green.
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
