"""Market Watch researchers: political and social media posts that can move stocks.

- Truth Social: Donald Trump's posts, read from the public trumpstruth.org
  archive feed (free; it checks Truth Social every few minutes).
- X (Twitter): the accounts listed in config/settings.yaml, read through the
  official X API. X charges per post read, so this only runs if you add an
  X_BEARER_TOKEN secret, and it stops at a daily post limit you set.

New posts are rated by the AI: which of our stocks they affect, whether they
move the whole market, and how much it matters.
"""
import datetime as dt
import email.utils
import html
import os
import re
import xml.etree.ElementTree as XML

import requests

from .common import STATE, log, now_et, read_json, settings, today, write_json

FILE = STATE / "research" / "_market_watch.json"
SEEN = STATE / "cache" / "market_watch_seen.json"
UA = {"User-Agent": "trading-bot-team/1.0 (research project)"}


def _clean(text):
    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


# ---------------------------------------------------------------- sources
def fetch_truth_social(limit=25):
    try:
        r = requests.get("https://www.trumpstruth.org/feed", headers=UA, timeout=20)
        r.raise_for_status()
        root = XML.fromstring(r.content)
    except Exception as e:
        log(f"Truth Social feed unavailable: {e}")
        return []
    posts = []
    for item in root.iter("item"):
        guid = (item.findtext("guid") or item.findtext("link") or "").strip()
        text = _clean(item.findtext("description"))
        if not text or text.lower() in ("view video", "view image"):
            text = _clean(item.findtext("title"))
        try:
            when = email.utils.parsedate_to_datetime(item.findtext("pubDate")).isoformat()
        except Exception:
            when = None
        posts.append({"id": f"truth:{guid}", "source": "Truth Social @realDonaldTrump",
                      "time": when, "text": text[:700]})
        if len(posts) >= limit:
            break
    return posts


def fetch_x(cfg):
    token = os.environ.get("X_BEARER_TOKEN")
    accounts = cfg.get("x_accounts") or []
    if not token or not accounts:
        return []
    cap = cfg.get("x_max_posts_per_day", 150)
    usage = read_json(STATE / "cache" / "x_usage.json", {})
    used = usage.get(today(), 0)
    if used >= cap:
        return []
    h = {"Authorization": f"Bearer {token}"}
    ids = read_json(STATE / "cache" / "x_users.json", {})
    since = read_json(STATE / "cache" / "x_since.json", {})
    posts = []
    for name in accounts:
        if used >= cap:
            break
        try:
            if name not in ids:
                r = requests.get(f"https://api.x.com/2/users/by/username/{name}", headers=h, timeout=20)
                r.raise_for_status()
                ids[name] = r.json()["data"]["id"]
            params = {"max_results": 5, "tweet.fields": "created_at", "exclude": "retweets,replies"}
            if since.get(name):
                params["since_id"] = since[name]
            r = requests.get(f"https://api.x.com/2/users/{ids[name]}/tweets", headers=h, params=params, timeout=20)
            if r.status_code == 429:
                log("X API rate limit reached; trying again next check.")
                break
            r.raise_for_status()
            data = r.json().get("data") or []
        except Exception as e:
            log(f"X: could not read @{name}: {e}")
            continue
        used += len(data)
        if data:
            since[name] = data[0]["id"]
        for t in data:
            posts.append({"id": f"x:{t['id']}", "source": f"X @{name}", "time": t.get("created_at"),
                          "text": t.get("text", "")[:700]})
    usage[today()] = used
    write_json(STATE / "cache" / "x_usage.json", {k: v for k, v in usage.items() if k >= today()[:8]})
    write_json(STATE / "cache" / "x_users.json", ids)
    write_json(STATE / "cache" / "x_since.json", since)
    return posts


# ---------------------------------------------------------------- the researcher
FORMAT = """Answer in JSON:
{
  "findings": [
    {"source": "where the post came from", "post": "one-sentence summary of the post",
     "market_wide": true/false, "affected": ["TICKER", ...], "sentiment": -1.0 to 1.0,
     "importance": 1-5, "why": "how this could move the stocks"}
  ],
  "overall": "one or two sentences on the political/social mood for markets right now"
}
Only include posts that could plausibly move stock prices (tariffs, trade deals, sanctions, defense
or space spending, regulation, interest rates, comments about specific companies or industries,
government contracts). Skip everything else. "affected" may only use tickers from the list below.
importance: 5 = likely to move prices a lot today."""


def check(llm, org):
    """Read new posts and rate them. Returns the new findings (may be empty)."""
    cfg = settings().get("market_watch", {})
    if not cfg.get("enabled", True):
        return []
    seen = set(read_json(SEEN, []))
    if os.environ.get("BOT_FAKE") == "1":
        posts = [{"id": f"fake:{now_et():%Y%m%d%H}", "source": "Truth Social @realDonaldTrump",
                  "time": now_et().isoformat(), "text": "We will be ordering many more fighter jets!"}]
    else:
        posts = fetch_truth_social() + fetch_x(cfg)
    new = [p for p in posts if p["id"] not in seen]
    if not new:
        return []
    universe = {t["name"]: t["tickers"] for t in org.values()}
    system = ("You are the Political & Social Media Researcher on an AI stock trading team. You watch posts by "
              "the President and other influential accounts and judge, quickly and soberly, whether they could "
              "move the team's stocks or the whole market. Most posts don't; say so by leaving them out.")
    prompt = f"Our teams and their stocks:\n{universe}\n\nNew posts:\n{new}\n\n{FORMAT}"
    result = llm.ask_json("fast", "political/social researcher", system, prompt)
    seen.update(p["id"] for p in new)
    write_json(SEEN, sorted(seen)[-3000:])
    stamp = now_et().isoformat(timespec="minutes")
    found = result.get("findings") or []
    for f in found:
        f["seen_at"] = stamp
    data = read_json(FILE, {})
    cutoff = (now_et() - dt.timedelta(hours=36)).isoformat(timespec="minutes")
    old = [f for f in data.get("findings", []) if f.get("seen_at", "") >= cutoff]
    write_json(FILE, {"updated": stamp, "overall": result.get("overall", data.get("overall", "")),
                      "posts_read": data.get("posts_read", 0) + len(new), "findings": (found + old)[:40]})
    log(f"Market watch: {len(new)} new post(s), {len(found)} market-relevant")
    return found


def for_team(team, limit=10):
    """Findings that matter for this team: market-wide ones plus ones naming its stocks."""
    data = read_json(FILE, {})
    out = [f for f in data.get("findings", [])
           if f.get("market_wide") or set(f.get("affected") or []) & set(team["tickers"])]
    return {"overall": data.get("overall", ""), "findings": out[:limit]}
