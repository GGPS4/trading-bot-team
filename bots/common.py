"""Shared helpers: paths, config loading, JSON state files, time."""
import collections
import time
import datetime as dt
import json
import os
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

ROOT = Path(__file__).resolve().parent.parent
STATE = Path(os.environ.get("BOT_STATE_DIR", ROOT / "state"))
DOCS_DATA = Path(os.environ.get("BOT_DOCS_DATA_DIR", ROOT / "docs" / "data"))
CONFIG = Path(os.environ.get("BOT_CONFIG_DIR", ROOT / "config"))
ET = ZoneInfo("America/New_York")


def now_et() -> dt.datetime:
    fake = os.environ.get("BOT_FAKE_NOW")  # used by tests, e.g. 2026-10-21T10:45
    if fake:
        return dt.datetime.fromisoformat(fake).replace(tzinfo=ET)
    return dt.datetime.now(ET)


def today() -> str:
    return now_et().date().isoformat()


def load_yaml(name: str) -> dict:
    with open(CONFIG / name) as f:
        return yaml.safe_load(f) or {}


def settings() -> dict:
    return load_yaml("settings.yaml")


def teams_config() -> dict:
    return load_yaml("teams.yaml")


def read_json(path, default=None):
    p = Path(path)
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text())
    except json.JSONDecodeError:
        return default


def write_json(path, data) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2, default=str))


LOG_LINES = collections.deque(maxlen=500)   # recent log lines, saved with each dashboard update
PROBLEMS = collections.deque(maxlen=200)    # (time, line) for failures, shown on the dashboard
_PROBLEM_WORDS = ("failed", "skipped", "error", "traceback", "could not", "no ai provider")


def log(msg: str) -> None:
    line = f"[{now_et():%Y-%m-%d %H:%M:%S} ET] {msg}"
    print(line, flush=True)
    LOG_LINES.append(line)
    if any(w in msg.lower() for w in _PROBLEM_WORDS):
        PROBLEMS.append((time.time(), msg))


def recent_problems(minutes=60):
    cutoff = time.time() - minutes * 60
    return [m for t, m in PROBLEMS if t >= cutoff]


def clamp(x, lo, hi):
    return max(lo, min(hi, x))


def pct(x) -> str:
    return f"{x * 100:+.2f}%"
