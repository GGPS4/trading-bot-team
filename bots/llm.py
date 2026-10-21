"""The bots' AI brains, spread across several free providers.

- Gemini (Google): your API key plus your $10/month credit
- Groq:            free tier, very fast open-source models
- OpenRouter:      free models (50 requests/day, or 1,000/day after a one-time $10 top-up)

config/settings.yaml decides which bots use which provider first (ai_routing).
If a provider is out of quota, rate-limited or down, the request moves to the
next one automatically. Every bot asks for a JSON answer so the code can read it.
"""
import json
import os
import re
import time

import requests

from .common import STATE, log, read_json, settings, today, write_json


class LLMError(Exception):
    pass


def parse_json(text: str):
    text = (text or "").strip()
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()  # some open models "think" out loud
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            return json.loads(m.group(0))
        raise


# ------------------------------------------------------------------ budgets
USAGE = STATE / "ai_usage.json"
BUCKET = {"name": "trading"}  # run.py switches this to "after_hours" for overnight research


def _key(name=None):
    name = name or BUCKET["name"]
    return today() if name == "trading" else f"{today()}-{name}"


def calls_today(name=None) -> int:
    return read_json(USAGE, {}).get(_key(name), 0)


def provider_calls_today(provider) -> int:
    return read_json(USAGE, {}).get(f"{today()}-provider-{provider}", 0)


def check_budget():
    if BUCKET["name"] == "trading":
        cap = settings().get("intraday", {}).get("max_ai_calls_per_day", 1000)
    else:
        cap = settings().get("after_hours", {}).get("max_ai_calls_per_day", 300)
    if calls_today() >= cap:
        raise LLMError(f"Daily AI budget for {BUCKET['name']} used up ({cap} calls). Stop-losses still work.")


def count_call(provider):
    u = read_json(USAGE, {})
    u = {k: v for k, v in u.items() if k >= today()[:8]}  # keep this month only
    u[_key()] = u.get(_key(), 0) + 1
    pk = f"{today()}-provider-{provider}"
    u[pk] = u.get(pk, 0) + 1
    write_json(USAGE, u)


def _trim(prompt, max_chars):
    """Smaller models accept less text. Keep the start and the answer format at the end."""
    if not max_chars or len(prompt) <= max_chars:
        return prompt
    tail = prompt[-1800:]
    return prompt[:max_chars - 1900] + "\n...[some details trimmed to fit]...\n" + tail


class RateLimited(LLMError):
    pass


# ------------------------------------------------------------------ Gemini
GEMINI_API = "https://generativelanguage.googleapis.com/v1beta"
_SKIP = ("lite", "image", "tts", "live", "audio", "embedding", "exp", "learnlm",
         "robotics", "computer-use", "native", "veo", "imagen")


class Gemini:
    name = "gemini"

    def __init__(self):
        self.key = os.environ.get("GEMINI_API_KEY", "")
        self.available = bool(self.key)
        self._models = None
        self._resolved = {}
        self._dead = set()

    def _list_models(self):
        if self._models is None:
            names, token = [], None
            while True:
                params = {"pageSize": 1000}
                if token:
                    params["pageToken"] = token
                r = requests.get(f"{GEMINI_API}/models", params=params,
                                 headers={"x-goog-api-key": self.key}, timeout=30)
                if r.status_code >= 400:
                    raise LLMError(f"Could not list Gemini models: {r.status_code} {r.text[:200]}")
                data = r.json()
                names += [m["name"].removeprefix("models/") for m in data.get("models", [])
                          if "generateContent" in m.get("supportedGenerationMethods", [])]
                token = data.get("nextPageToken")
                if not token:
                    break
            self._models = names
        return self._models

    def resolve(self, name):
        """'auto-flash' / 'auto-pro' -> newest model of that family."""
        if not name.startswith("auto-"):
            return name
        if name not in self._resolved:
            family = name.split("-", 1)[1]
            cands = [n for n in self._list_models()
                     if re.match(rf"^gemini-[\d.]+-{family}(-|$)", n) and not any(s in n for s in _SKIP)]
            if not cands:
                raise LLMError(f"No Gemini '{family}' model on this key.")
            rank = lambda n: (tuple(int(x) for x in re.match(r"gemini-([\d.]+)", n).group(1).split(".") if x),  # noqa: E731
                              "preview" not in n, -len(n))
            self._resolved[name] = max(cands, key=rank)
            log(f"LLM: gemini {name} -> {self._resolved[name]}")
        return self._resolved[name]

    def ask(self, tier, system, prompt):
        cfg = settings().get("models", {})
        chain = [cfg.get("smart", "auto-pro"), cfg.get("fast", "auto-flash")] if tier == "smart" \
            else [cfg.get("fast", "auto-flash")]
        last = None
        for mname in chain:
            try:
                model = self.resolve(mname)
            except LLMError as e:
                last = e
                continue
            if model in self._dead:
                continue
            body = {"systemInstruction": {"parts": [{"text": system}]},
                    "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                    "generationConfig": {"responseMimeType": "application/json", "temperature": 0.4}}
            for attempt in range(2):
                r = requests.post(f"{GEMINI_API}/models/{model}:generateContent",
                                  headers={"x-goog-api-key": self.key}, json=body, timeout=240)
                if r.status_code == 429:
                    if "limit: 0" in r.text or '"limit": 0' in r.text:
                        self._dead.add(model)
                        last = LLMError(f"{model} has no quota on this key")
                        break
                    if attempt == 0:
                        time.sleep(8)
                        continue
                    raise RateLimited(f"gemini {model} rate limited")
                if r.status_code >= 500:
                    last = LLMError(f"gemini server error {r.status_code}")
                    time.sleep(5)
                    continue
                if r.status_code >= 400:
                    self._dead.add(model)
                    last = LLMError(f"{model}: {r.status_code} {r.text[:200]}")
                    break
                parts = r.json()["candidates"][0]["content"]["parts"]
                return parse_json("".join(p.get("text", "") for p in parts if not p.get("thought"))), model
        raise last or LLMError("gemini failed")


# ------------------------------------------------------------------ OpenAI-style providers
class OpenAIStyle:
    """Groq, GitHub Models and OpenRouter all speak the same 'chat completions' language."""

    def __init__(self, name, url, key_env, models_url, prefer, max_chars, extra_headers=None, json_mode=True):
        self.name, self.url, self.models_url = name, url, models_url
        self.key = os.environ.get(key_env, "")
        self.available = bool(self.key)
        self.prefer, self.max_chars, self.json_mode = prefer, max_chars, json_mode
        self.h = {"Authorization": f"Bearer {self.key}", "Content-Type": "application/json", **(extra_headers or {})}
        self._model = None
        self._tried = set()

    def pick_model(self):
        if self._model:
            return self._model
        override = (settings().get("ai_models") or {}).get(self.name)
        if override:
            self._model = override
            return override
        ids = []
        if self.models_url:
            try:
                r = requests.get(self.models_url, headers=self.h, timeout=30)
                data = r.json() if r.ok else {}
                items = data.get("data", data) if isinstance(data, dict) else data
                ids = [m.get("id") or m.get("name") for m in items if isinstance(m, dict)]
            except Exception:
                ids = []
        for p in self.prefer:
            if p not in self._tried and (not ids or p in ids):
                self._model = p
                break
        if not self._model:
            raise LLMError(f"{self.name}: none of the preferred models are available")
        log(f"LLM: {self.name} -> {self._model}")
        return self._model

    def ask(self, tier, system, prompt):
        for _ in range(len(self.prefer)):
            model = self.pick_model()
            body = {"model": model, "temperature": 0.4,
                    "messages": [{"role": "system", "content": system + " Reply with valid JSON only."},
                                 {"role": "user", "content": _trim(prompt, self.max_chars)}]}
            if self.json_mode:
                body["response_format"] = {"type": "json_object"}
            r = requests.post(self.url, headers=self.h, json=body, timeout=180)
            if r.status_code == 429:
                raise RateLimited(f"{self.name} rate limited")
            if r.status_code in (400, 404) and ("model" in r.text.lower()):
                self._tried.add(model)  # model retired or unknown: try the next preferred one
                self._model = None
                continue
            if r.status_code >= 400:
                raise LLMError(f"{self.name}: {r.status_code} {r.text[:200]}")
            try:
                text = r.json()["choices"][0]["message"].get("content") or ""
            except (ValueError, KeyError, IndexError):
                raise LLMError(f"{self.name}: unexpected reply {r.status_code} {r.text[:160]!r}")
            try:
                return parse_json(text), model
            except ValueError:
                raise LLMError(f"{self.name}: answer was not JSON: {text[:120]!r}")
        raise LLMError(f"{self.name}: no working model")


def build_providers():
    return {
        "gemini": Gemini(),
        "groq": OpenAIStyle(
            "groq", "https://api.groq.com/openai/v1/chat/completions", "GROQ_API_KEY",
            "https://api.groq.com/openai/v1/models",
            ["openai/gpt-oss-120b", "qwen/qwen3.6-27b", "openai/gpt-oss-20b", "llama-3.3-70b-versatile"],
            max_chars=24000),
        "openrouter": OpenAIStyle(
            "openrouter", "https://openrouter.ai/api/v1/chat/completions", "OPENROUTER_API_KEY",
            None, ["openrouter/free"], max_chars=60000,
            extra_headers={"HTTP-Referer": "https://github.com/trading-bot-team", "X-Title": "Trading Bot Team"},
            json_mode=False),
    }


# ------------------------------------------------------------------ the router
class Router:
    def __init__(self):
        self.providers = build_providers()
        self.cooldown = {}  # provider -> time it can be used again
        self.calls = 0
        ready = [p for p, v in self.providers.items() if v.available]
        if not ready:
            raise LLMError("No AI provider keys are set (GEMINI_API_KEY, GROQ_API_KEY, ...).")
        log(f"LLM: providers ready: {', '.join(ready)}")

    def resolve(self, name):  # kept for `run.py check`
        return self.providers["gemini"].resolve(name)

    def ask_json(self, tier, role, system, prompt):
        check_budget()
        cfg = settings()
        order = (cfg.get("ai_routing") or {}).get(tier) or ["gemini", "groq", "openrouter"]
        caps = cfg.get("ai_daily_caps") or {}
        errors = []
        for name in order:
            p = self.providers.get(name)
            if not p or not p.available:
                continue
            if self.cooldown.get(name, 0) > time.time():
                continue
            if caps.get(name) and provider_calls_today(name) >= caps[name]:
                continue
            try:
                result, model = p.ask(tier, system, prompt)
            except RateLimited as e:
                self.cooldown[name] = time.time() + 300  # rest this provider for 5 minutes
                errors.append(str(e))
                continue
            except (LLMError, requests.RequestException, KeyError, IndexError, ValueError) as e:
                self.cooldown[name] = time.time() + 120
                errors.append(f"{name}: {e}")
                continue
            self.calls += 1
            count_call(name)
            log(f"LLM: {role} answered ({name} {model})")
            return result
        raise LLMError(f"{role}: no AI provider could answer ({'; '.join(errors)[:400]})")


    def test(self):
        """Ask every provider a tiny question; used by `run.py check`."""
        out = {}
        for name, p in self.providers.items():
            if not p.available:
                out[name] = "not set up (no key)"
                continue
            try:
                res, model = p.ask("fast", "You are a test.", 'Return the JSON {"ok": true}.')
                out[name] = f"OK ({model})" if res.get("ok") else f"answered oddly ({model})"
            except Exception as e:
                out[name] = f"FAILED: {str(e)[:150]}"
        return out
