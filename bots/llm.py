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

    def _family(self, family, lite=False):
        """All usable models of a family, newest first (non-lite before lite)."""
        skip = tuple(x for x in _SKIP if x != "lite") if lite else _SKIP
        cands = [n for n in self._list_models()
                 if re.match(rf"^gemini-[\d.]+-{family}(-|$)", n) and not any(x in n for x in skip)]
        rank = lambda n: ("lite" not in n,  # noqa: E731
                          tuple(int(x) for x in re.match(r"gemini-([\d.]+)", n).group(1).split(".") if x),
                          "preview" not in n, -len(n))
        return sorted(cands, key=rank, reverse=True)

    def chain(self, tier):
        cfg = settings().get("models", {})
        names = []
        for want in ([cfg.get("smart", "auto-pro")] if tier == "smart" else []) + [cfg.get("fast", "auto-flash")]:
            if want.startswith("auto-"):
                try:
                    names += self._family(want.split("-", 1)[1], lite=(want == "auto-flash"))[:4]
                except LLMError:
                    continue
            else:
                names.append(want)
        seen = set()
        return [n for n in names if not (n in seen or seen.add(n))]

    def ask(self, tier, system, prompt):
        """Each Gemini model has its own per-minute limit, so when one is busy try the next."""
        cool = self.__dict__.setdefault("_cool", {})
        last, busy = None, False
        for model in self.chain(tier):
            if model in self._dead:
                continue
            if cool.get(model, 0) > time.time():
                busy = True
                continue
            body = {"systemInstruction": {"parts": [{"text": system}]},
                    "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                    "generationConfig": {"responseMimeType": "application/json", "temperature": 0.4}}
            try:
                r = requests.post(f"{GEMINI_API}/models/{model}:generateContent",
                                  headers={"x-goog-api-key": self.key}, json=body, timeout=180)
            except requests.RequestException as e:
                last = LLMError(f"{model}: {e}")
                continue
            if r.status_code == 429:
                if "limit: 0" in r.text or '"limit": 0' in r.text:
                    self._dead.add(model)
                    last = LLMError(f"{model} has no quota on this key")
                else:
                    cool[model] = time.time() + 60
                    busy = True
                continue
            if r.status_code in (403, 404):
                self._dead.add(model)
                last = LLMError(f"{model}: {r.status_code} {r.text[:200]}")
                continue
            if r.status_code >= 400:
                last = LLMError(f"{model}: {r.status_code} {r.text[:200]}")
                continue
            try:
                parts = r.json()["candidates"][0]["content"]["parts"]
                return parse_json("".join(p.get("text", "") for p in parts if not p.get("thought"))), model
            except (KeyError, IndexError, ValueError) as e:
                last = LLMError(f"{model}: unusable answer ({e})")
                continue
        if busy:
            raise RateLimited("gemini: every model is at its per-minute limit")
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
        self._cands = None
        self._tried = set()
        self._cool = {}

    def candidates(self):
        """Preferred models that exist on this account, then (OpenRouter) any other free model."""
        if self._cands is not None:
            return self._cands
        override = (settings().get("ai_models") or {}).get(self.name)
        if override:
            self._cands = [override]
            return self._cands
        ids = []
        if self.models_url:
            try:
                r = requests.get(self.models_url, headers=self.h, timeout=30)
                data = r.json() if r.ok else {}
                items = data.get("data", data) if isinstance(data, dict) else data
                ids = [m.get("id") or m.get("name") for m in items if isinstance(m, dict)]
            except Exception:
                ids = []
        ids = [i for i in ids if i]
        if self.name == "openrouter" and ids:
            # only free models, never safety/guard classifiers or vision-only models
            ids = [i for i in ids if i.endswith(":free")
                   and not any(b in i for b in ("guard", "safety", "vision", "embed", "audio"))]
        cands = []
        for p in self.prefer:
            if not ids:
                cands.append(p)
            elif p in ids:
                cands.append(p)
            else:
                cands += [i for i in ids if p in i and i not in cands][:2]
        if self.name == "openrouter":
            cands += [i for i in ids if i not in cands][:6]
        self._cands = list(dict.fromkeys(cands))
        log(f"LLM: {self.name} models -> {self._cands[:6]}")
        return self._cands

    def ask(self, tier, system, prompt):
        """Rate limits are per model, so when one model is busy, try the next one."""
        busy, last = False, None
        for model in self.candidates():
            if model in self._tried:
                continue
            if self._cool.get(model, 0) > time.time():
                busy = True
                continue
            body = {"model": model, "temperature": 0.4,
                    "messages": [{"role": "system", "content": system + " Reply with valid JSON only."},
                                 {"role": "user", "content": _trim(prompt, self.max_chars)}]}
            if self.json_mode:
                body["response_format"] = {"type": "json_object"}
            try:
                r = requests.post(self.url, headers=self.h, json=body, timeout=180)
            except requests.RequestException as e:
                last = LLMError(f"{self.name} {model}: {e}")
                continue
            if r.status_code in (413, 429):  # per-minute limit or request too big for this model's limit
                self._cool[model] = time.time() + 60
                busy = True
                continue
            if r.status_code in (400, 404) and ("model" in r.text.lower()):
                self._tried.add(model)  # model retired or unknown
                continue
            if r.status_code >= 400:
                last = LLMError(f"{self.name} {model}: {r.status_code} {r.text[:200]}")
                continue
            try:
                text = r.json()["choices"][0]["message"].get("content") or ""
                return parse_json(text), model
            except (ValueError, KeyError, IndexError):
                if self.name == "openrouter":
                    self._tried.add(model)  # this free model can't do JSON well
                last = LLMError(f"{self.name} {model}: answer was not JSON")
                continue
        if busy:
            raise RateLimited(f"{self.name}: every model is at its per-minute limit")
        if last is None and self._tried:
            self._tried.clear()  # everything was ruled out: give them all another chance next time
        raise last or LLMError(f"{self.name}: no working model")


def build_providers():
    return {
        "gemini": Gemini(),
        "groq": OpenAIStyle(
            "groq", "https://api.groq.com/openai/v1/chat/completions", "GROQ_API_KEY",
            "https://api.groq.com/openai/v1/models",
            ["openai/gpt-oss-120b", "llama-3.3-70b-versatile", "meta-llama/llama-4-maverick",
             "meta-llama/llama-4-scout", "qwen/qwen3.6-27b", "qwen/qwen3-32b", "openai/gpt-oss-20b",
             "moonshotai/kimi-k2"],
            max_chars=16000),
        "openrouter": OpenAIStyle(
            "openrouter", "https://openrouter.ai/api/v1/chat/completions", "OPENROUTER_API_KEY",
            "https://openrouter.ai/api/v1/models",
            ["deepseek-chat", "deepseek-v3", "llama-3.3-70b", "gpt-oss-120b", "qwen3", "gemma-3-27b",
             "mistral-small", "openrouter/free"], max_chars=60000,
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
        for attempt in range(3):
            result, waiting, errors = self._try_all(tier, role, system, prompt)
            if result is not None:
                return result
            if not waiting:
                break
            # Every provider is just resting after a per-minute limit: wait for the first one back.
            pause = max(5, min(70, min(waiting) - time.time()))
            log(f"LLM: all providers busy, {role} waits {pause:.0f}s")
            time.sleep(pause)
        raise LLMError(f"{role}: no AI provider could answer ({'; '.join(errors)[:400]})")

    def _try_all(self, tier, role, system, prompt):
        cfg = settings()
        order = (cfg.get("ai_routing") or {}).get(tier) or ["gemini", "groq", "openrouter"]
        caps = cfg.get("ai_daily_caps") or {}
        errors, waiting = [], []
        for name in order:
            p = self.providers.get(name)
            if not p or not p.available:
                continue
            if caps.get(name) and provider_calls_today(name) >= caps[name]:
                continue
            if self.cooldown.get(name, 0) > time.time():
                waiting.append(self.cooldown[name])
                continue
            try:
                result, model = p.ask(tier, system, prompt)
            except RateLimited as e:
                self.cooldown[name] = time.time() + 60  # per-minute limit: rest a minute
                waiting.append(self.cooldown[name])
                errors.append(str(e))
                continue
            except (LLMError, requests.RequestException, KeyError, IndexError, ValueError) as e:
                self.cooldown[name] = time.time() + 90
                errors.append(f"{name}: {e}")
                continue
            self.calls += 1
            count_call(name)
            log(f"LLM: {role} answered ({name} {model})")
            return result, [], errors
        return None, waiting, errors

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
