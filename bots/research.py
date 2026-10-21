"""Team Researchers.

Researcher 1 (news): reads company news and SEC filings.
Researcher 2 (social): watches Reddit for posts about the team's stocks.

Raw data is collected with plain code (free). Only the short list of
articles/posts is sent to the AI, which rates each one.
"""
import datetime as dt
import statistics
import time

import requests

from .common import STATE, log, now_et, read_json, settings, write_json

UA = "trading-bot-team/1.0 (research project)"

RATING_FORMAT = """Answer in JSON exactly like this:
{
  "findings": [
    {"symbol": "TICKER", "sentiment": -1.0 to 1.0, "importance": 1-5,
     "unconfirmed_rumor": true/false, "summary": "one sentence", "source": "where it came from"}
  ],
  "urgent": ["TICKER", ...],
  "overall": "two sentences on the mood around this group of stocks"
}
sentiment: -1 very bad for the stock, 0 neutral, +1 very good.
importance: 1 = noise, 3 = worth knowing, 5 = likely to move the price a lot today.
Put a symbol in "urgent" only if something with importance 5 just happened.
Only include symbols from the team's list. Skip items that say nothing new."""


# ---------------------------------------------------------------- technicals
def _rsi(closes, n=14):
    if len(closes) <= n:
        return None
    gains, losses = [], []
    for a, b in zip(closes[-n - 1:-1], closes[-n:]):
        d = b - a
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    avg_loss = sum(losses) / n
    if avg_loss == 0:
        return 100.0
    rs = (sum(gains) / n) / avg_loss
    return round(100 - 100 / (1 + rs), 1)


def technicals(bars: dict) -> dict:
    out = {}
    for sym, rows in bars.items():
        closes = [b["c"] for b in rows]
        if len(closes) < 6:
            continue
        last = closes[-1]
        rets = [b / a - 1 for a, b in zip(closes[:-1], closes[1:])]
        sma = lambda n: sum(closes[-n:]) / n if len(closes) >= n else None  # noqa: E731
        out[sym] = {
            "price": round(last, 2),
            "change_1d": round(rets[-1], 4),
            "change_5d": round(last / closes[-6] - 1, 4),
            "change_20d": round(last / closes[-21] - 1, 4) if len(closes) > 20 else None,
            "above_20d_avg": (last > sma(20)) if sma(20) else None,
            "above_50d_avg": (last > sma(50)) if sma(50) else None,
            "rsi_14": _rsi(closes),
            "daily_volatility": round(statistics.pstdev(rets[-20:]), 4) if len(rets) >= 20 else None,
        }
    return out


# ---------------------------------------------------------------- raw sources
def fetch_reddit(tickers, per_ticker=8):
    posts, blocked = [], False
    for t in tickers:
        try:
            r = requests.get(
                "https://www.reddit.com/r/stocks+wallstreetbets+investing+StockMarket/search.json",
                params={"q": f"{t}", "restrict_sr": 1, "sort": "new", "t": "day", "limit": per_ticker},
                headers={"User-Agent": UA}, timeout=15)
        except requests.RequestException:
            blocked = True
            break
        if r.status_code in (403, 429):
            blocked = True
            break
        if r.status_code != 200:
            continue
        for c in r.json().get("data", {}).get("children", []):
            d = c.get("data", {})
            title = d.get("title", "")
            if t not in title.upper().replace("$", " ").split() and f"${t}" not in title.upper():
                continue  # ticker must actually appear in the title
            posts.append({"symbol": t, "title": title[:200], "subreddit": d.get("subreddit"),
                          "score": d.get("score"), "comments": d.get("num_comments"),
                          "text": (d.get("selftext") or "")[:300]})
        time.sleep(1.2)
    return posts, blocked


def fetch_filings(tickers, contact, days=3):
    """Recent SEC filings (8-K news, 10-Q/10-K reports, Form 4 insider trades)."""
    if not contact:
        return []
    headers = {"User-Agent": contact}
    cache = STATE / "cache" / "sec_tickers.json"
    mapping = read_json(cache)
    if not mapping:
        try:
            r = requests.get("https://www.sec.gov/files/company_tickers.json", headers=headers, timeout=30)
            r.raise_for_status()
            mapping = {v["ticker"]: str(v["cik_str"]).zfill(10) for v in r.json().values()}
            write_json(cache, mapping)
        except requests.RequestException as e:
            log(f"SEC: could not load ticker list ({e})")
            return []
    cutoff = (now_et().date().toordinal() - days)
    out = []
    for t in tickers:
        cik = mapping.get(t)
        if not cik:
            continue
        try:
            r = requests.get(f"https://data.sec.gov/submissions/CIK{cik}.json", headers=headers, timeout=20)
            r.raise_for_status()
        except requests.RequestException:
            continue
        rec = r.json().get("filings", {}).get("recent", {})
        for form, date, acc, desc in zip(rec.get("form", []), rec.get("filingDate", []),
                                         rec.get("accessionNumber", []), rec.get("primaryDocDescription", [])):
            if form not in ("8-K", "10-Q", "10-K", "4"):
                continue
            if dt.date.fromisoformat(date).toordinal() < cutoff:
                break
            out.append({"symbol": t, "form": form, "date": date, "description": desc})
        time.sleep(0.2)
    return out


# ---------------------------------------------------------------- the two researchers
def _team_header(team):
    return (f"Team: {team['name']}\nCompanies (only these): {', '.join(team['tickers'])}\n"
            f"Team focus: {team.get('focus') or 'the ' + team['name'] + ' sector'}\n")


def news_researcher(llm, broker, team, hours):
    news = broker.news(team["tickers"], hours=hours)
    filings = fetch_filings(team["tickers"], settings().get("sec_contact", ""))
    if not news and not filings:
        return {"findings": [], "urgent": [], "overall": "No new news.", "items_read": 0}
    items = [{"symbols": n.get("symbols"), "headline": n.get("headline"), "summary": (n.get("summary") or "")[:300],
              "source": n.get("source"), "time": n.get("created_at")} for n in news]
    system = ("You are the News Researcher on an AI stock trading team. You read company news and SEC filings "
              "and tell your manager what matters for the team's stocks. Be skeptical: separate confirmed facts "
              "from rumors, and ignore recycled or promotional articles.")
    prompt = (_team_header(team) + f"\nNews from the last {hours} hours:\n{items}\n\nSEC filings:\n{filings}\n\n"
              + RATING_FORMAT)
    result = llm.ask_json("fast", f"{team['id']} news researcher", system, prompt)
    result["items_read"] = len(items) + len(filings)
    return result


def social_researcher(llm, team):
    posts, blocked = fetch_reddit(team["tickers"])
    if not posts:
        note = "Reddit blocked this run." if blocked else "No relevant posts today."
        return {"findings": [], "urgent": [], "overall": note, "items_read": 0}
    system = ("You are the Social Media Researcher on an AI stock trading team. You read Reddit posts about the "
              "team's stocks and judge the crowd's mood. Most posts are noise, hype or jokes; only flag posts "
              "that point to real information or unusually strong crowd attention.")
    prompt = _team_header(team) + f"\nReddit posts from the last day:\n{posts}\n\n" + RATING_FORMAT
    result = llm.ask_json("fast", f"{team['id']} social researcher", system, prompt)
    result["items_read"] = len(posts)
    return result


def run_research(llm, broker, team, phase):
    hours = 20 if phase == "morning" else 4
    log(f"{team['id']}: researching ({phase})")
    out = {"time": now_et().isoformat(timespec="minutes"), "phase": phase}
    try:
        out["news"] = news_researcher(llm, broker, team, hours)
    except Exception as e:  # research failing should never crash trading
        log(f"{team['id']}: news researcher failed: {e}")
        out["news"] = {"findings": [], "urgent": [], "overall": f"News researcher failed: {e}", "items_read": 0}
    if phase == "morning":
        try:
            out["social"] = social_researcher(llm, team)
        except Exception as e:
            log(f"{team['id']}: social researcher failed: {e}")
            out["social"] = {"findings": [], "urgent": [], "overall": f"Social researcher failed: {e}", "items_read": 0}
    else:
        prev = read_json(STATE / "research" / f"{team['id']}.json", {})
        out["social"] = prev.get("social", {"findings": [], "urgent": [], "overall": "", "items_read": 0})
    try:
        out["technicals"] = technicals(broker.daily_bars(team["tickers"]))
    except Exception as e:
        log(f"{team['id']}: price history failed: {e}")
        out["technicals"] = {}
    write_json(STATE / "research" / f"{team['id']}.json", out)
    return out
