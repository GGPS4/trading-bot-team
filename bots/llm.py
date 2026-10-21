"""Talks to Google's Gemini API (plain REST, no SDK needed).

Every bot asks for a JSON answer so the code can read it reliably.
"""
import json
import os
import re
import time

import requests

from .common import log, settings

API = "https://generativelanguage.googleapis.com/v1beta"
# Model name parts we never want for text reasoning.
_SKIP = ("lite", "image", "tts", "live", "audio", "embedding", "exp", "learnlm",
         "robotics", "computer-use", "native", "veo", "imagen")


class LLMError(Exception):
    pass


def parse_json(text: str):
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            return json.loads(m.group(0))
        raise


class Gemini:
    def __init__(self, api_key: str | None = None):
        self.key = api_key or os.environ.get("GEMINI_API_KEY", "")
        if not self.key:
            raise LLMError("GEMINI_API_KEY is not set (add it as a GitHub secret).")
        self._models: list[str] | None = None
        self._resolved: dict[str, str] = {}
        self._dead: set[str] = set()  # models that failed with "not allowed" this run
        self.calls = 0

    # ---------- model picking ----------
    def _list_models(self) -> list[str]:
        if self._models is None:
            names, token = [], None
            while True:
                params = {"pageSize": 1000}
                if token:
                    params["pageToken"] = token
                r = requests.get(f"{API}/models", params=params,
                                 headers={"x-goog-api-key": self.key}, timeout=30)
                if r.status_code >= 400:
                    raise LLMError(f"Could not list Gemini models: {r.status_code} {r.text[:200]}")
                data = r.json()
                for m in data.get("models", []):
                    if "generateContent" in m.get("supportedGenerationMethods", []):
                        names.append(m["name"].removeprefix("models/"))
                token = data.get("nextPageToken")
                if not token:
                    break
            self._models = names
        return self._models

    def resolve(self, name: str) -> str:
        """'auto-flash' / 'auto-pro' -> newest stable model of that family."""
        if not name.startswith("auto-"):
            return name
        if name in self._resolved:
            return self._resolved[name]
        family = name.split("-", 1)[1]
        cands = [n for n in self._list_models()
                 if re.match(rf"^gemini-[\d.]+-{family}(-|$)", n)
                 and not any(s in n for s in _SKIP)]
        if not cands:
            raise LLMError(f"No Gemini '{family}' model is available on this API key.")

        def rank(n):
            ver = tuple(int(x) for x in re.match(r"gemini-([\d.]+)", n).group(1).split(".") if x)
            stable = "preview" not in n
            return (stable, ver, -len(n))

        chosen = max(cands, key=rank)
        self._resolved[name] = chosen
        log(f"LLM: {name} -> {chosen}")
        return chosen

    # ---------- asking ----------
    def ask_json(self, tier: str, role: str, system: str, prompt: str, retries: int = 3) -> dict:
        """tier = 'fast' or 'smart'. role is just a label for logs."""
        cfg = settings().get("models", {})
        chain = [cfg.get(tier, "auto-flash")]
        if tier != "fast":
            chain.append(cfg.get("fast", "auto-flash"))
        last_err = None
        for model_name in chain:
            try:
                model = self.resolve(model_name)
            except LLMError as e:
                last_err = e
                continue
            if model in self._dead:
                continue
            for attempt in range(retries):
                body = {
                    "systemInstruction": {"parts": [{"text": system}]},
                    "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                    "generationConfig": {"responseMimeType": "application/json", "temperature": 0.4},
                }
                try:
                    r = requests.post(f"{API}/models/{model}:generateContent",
                                      headers={"x-goog-api-key": self.key}, json=body, timeout=240)
                except requests.RequestException as e:
                    last_err = e
                    time.sleep(10)
                    continue
                if r.status_code == 429:
                    last_err = LLMError(f"{model} rate limited: {r.text[:200]}")
                    if '"limit": 0' in r.text or "limit: 0" in r.text:
                        self._dead.add(model)  # this key has no quota for the model
                        break
                    time.sleep(30 * (attempt + 1))
                    continue
                if r.status_code >= 500:
                    last_err = LLMError(f"{model} server error {r.status_code}")
                    time.sleep(15 * (attempt + 1))
                    continue
                if r.status_code >= 400:
                    last_err = LLMError(f"{model}: {r.status_code} {r.text[:300]}")
                    self._dead.add(model)
                    break
                try:
                    parts = r.json()["candidates"][0]["content"]["parts"]
                    text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
                    result = parse_json(text)
                except (KeyError, IndexError, ValueError) as e:
                    last_err = LLMError(f"{model} gave an unreadable answer: {e}")
                    continue
                self.calls += 1
                log(f"LLM: {role} answered ({model})")
                return result
            log(f"LLM: {role} could not use {model} ({last_err}); trying fallback")
        raise LLMError(f"{role}: all models failed. Last error: {last_err}")
