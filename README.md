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
| Political & Social Media Researcher (shared) | Watches Trump's Truth Social posts, and X accounts if an X token is added. Flags posts that could move our stocks or the whole market. | Gemini Flash |

The risk rules in `config/settings.yaml` are enforced by code. The AI can only make them stricter.

## AI providers (all free)

The bots spread their thinking across three AI providers. If one runs out of free quota or goes down, the next one takes over automatically:

| Provider | Used first by | Free limit (approx.) | Setup |
|---|---|---|---|
| Gemini (Google) | managers, President | your key's free tier, plus your $10/month credit | `GEMINI_API_KEY` |
| Groq | researchers, executives, Reporter | ~1,000 requests/day | `GROQ_API_KEY` |
| OpenRouter | last backup | 50/day (1,000/day after a one-time $10) | `OPENROUTER_API_KEY` |

The order and daily caps are in `ai_routing` and `ai_daily_caps` in `config/settings.yaml`.

## Trading all day (GitHub Actions, weekdays)

From the 9:30 AM open to the 4:00 PM close, the bots run in a loop:

- **Every 5 minutes:** check live prices, check stop-loss and take-profit levels, and read any new news articles. A news researcher rates new articles right away.
- **Every 20 minutes**, or sooner if there's big news or one of the team's stocks moves 2%: the manager takes a fresh look and can buy or sell.
- **Every 30 minutes:** the dashboard updates.
- **After the close:** manager reviews and lessons, then executives, the Reporter, and the President.

GitHub jobs can last at most 6 hours, so the day runs as two back-to-back sessions: open to 1 PM, and 1 PM to close.
You can change the timing in the `intraday:` part of `config/settings.yaml`. Set `close_positions_before_close: true` for pure day trading, so everything is sold before 4 PM.
The bots stop making new AI decisions after `max_ai_calls_per_day`, to stay in Gemini's free limit. Stop-losses keep working after that.

## Strategy: day trading with money always in play

- **Minimum invested:** while the market is open, every team must keep at least 75% of its budget in stocks (`risk.min_invested_pct`). If a manager leaves too much cash, the investor buys the manager's next-best ranked ideas. A team that's under the line gets a fresh manager look within about 10 minutes.
- **Day trading:** tight stops (about 1.5%), quick profit targets (about 3%), and up to 25 buys per team per day. There are no new positions after 3:30 PM, and everything is sold about 10 minutes before the close. Each day starts fresh in cash.
- **Safety still comes first:** the 3% daily loss limit and the 10% cash reserve override the 75% rule.
- **Watch 15-20, hold 5:** each team watches 15-20 stocks (`watchlist` in settings) and holds at most 5 at a time. The starter lists are in `config/teams.yaml`. During the day, managers add names that are in the news, moving unusually or trading heavy volume (using Alpaca's market-wide top movers, most-active list and headlines), and drop names that have gone quiet. The evening review sets up the next day's list from after-hours news. Code enforces the limits: at most 3 changes per look, real and tradable stocks over $5 only, one team per stock, and held stocks can't be dropped. The live lists and every change are on each team's **Watchlist** tab.

## How trades are entered and exited

- **Long and short:** managers can BUY (profit if the price rises) or SHORT (profit if it falls; whole shares of easy-to-borrow stocks, at most half a team's budget). SELL closes a long, COVER closes a short.
- **Size by volatility:** each trade risks about 0.4% of the team budget if its stop is hit. Stops are at least 35% of the stock's typical daily move, so jumpy stocks get wider stops and smaller positions (`sizing`).
- **Protective stops at the broker:** whole-share positions keep a real stop order at Alpaca. Fractional positions are watched by code about once a minute.
- **Scale out:** at the first target, half the position is sold and the stop on the rest moves to the entry price (`exits`).
- **Trailing stop:** once a trade is up 1%, the stop follows the best price 0.8% behind and only moves in the trade's favor.
- **Market mood** (`bots/mood.py`): SPY, QQQ, the VIXY fear gauge and breadth set the regime. *Caution* makes longs 25% smaller. *Risk-off* makes longs half size, requires conviction +1, and favors shorts.
- **Pre-market scan** (`bots/premarket.py`): from 8:00 AM ET, stocks gapping 2%+ on the watchlists (and 3%+ ones in the overnight news) go to the managers' first look and to the Research page.

## What the managers see

- **Day-trading signals** (`bots/intraday.py`), from today's 5-minute bars: VWAP and distance from it, the opening range (first 30 min) with breakout/breakdown flags, 15- and 60-minute momentum, the short-term trend, and relative volume (today vs a normal day at this hour).
- **Event calendar** (`bots/events.py`): earnings dates for watched stocks (Nasdaq's public calendar) and medium/high-impact US economic releases (CPI, jobs, Fed). New buys pause from 10 minutes before to 5 minutes after high-impact releases during market hours (`events` in settings).
- **Scorecard** (`bots/scorecard.py`): every buy is tagged with its setup (opening range breakout, VWAP bounce, momentum, news catalyst, earnings gap, ...) and the experiment being tested. Closed trades over 30 days are scored by setup, team, experiment and exit type (win rate, average return, profit factor). Managers see their own scorecard; the President uses the full one to move money toward what works.
- **Health banner:** the dashboard warns when data is stale (over 25-45 minutes during market hours) or when the loss limit or a pause is in force.

## Research never stops

Outside market hours (evenings, overnight, pre-market and weekends), "watch" jobs keep the researchers reading every 15 minutes:

- company news
- SEC filings (if `sec_contact` is set)
- Truth Social
- X

They never trade. Everything they find is handed to the managers at the next open, so earnings released at 4:05 PM or a post at midnight is already known at 9:30 AM.

- **Truth Social:** read for free from the public trumpstruth.org archive.
- **X (Twitter):** needs an `X_BEARER_TOKEN` secret, from developer.x.com (pay-per-use, about $0.005 per post). The daily post limit is in `market_watch:` in `config/settings.yaml`.

## How realistic is practice trading?

- The bots are never told it's practice money.
- Every trade is charged realistic slippage (`realism.slippage_pct`), so results aren't better than real fills would be.
- **Make the paper balance match the money you'll really use** (Alpaca → Paper account → reset). A $100,000 practice account teaches different habits than $1,000.
- Limits of paper trading: Alpaca's practice fills don't affect the market, and the free price feed (IEX) is a slice of all trading. Real fills can be a bit slower and worse, especially at the open and close.

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
