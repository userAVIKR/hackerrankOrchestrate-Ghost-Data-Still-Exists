"""Optional LLM second opinion for request intent.

The deterministic rules in intent.py decide every request in the dataset with high
confidence, so the default run makes no model calls. With `--use-llm`, requests the
rules are unsure about (confidence < 0.9) are sent to Claude for a JSON verdict. The
API key is read from the environment (ANTHROPIC_API_KEY or an `ant auth login`
profile) and never logged. Responses are cached on disk so reruns are free.
"""
from __future__ import annotations

import hashlib
import json
import os

from .common import CACHE_DIR

MODEL = os.environ.get("GHOST_AUDIT_MODEL", "claude-opus-5-5")

PROMPT = """You review privacy requests for an e-commerce platform.
Decide whether the customer's message authorises deleting their account / personal data.
Requests for access, a data copy, export, correction, password help, or marketing opt-out
are NOT deletion requests, even when they mention the account. Messages that ask to remove,
erase, close or delete the account, or to verify / finish a deletion, ARE deletion requests.
Reply with JSON only: {"authorizes_deletion": true|false, "reason": "<short>"}

Customer message:
"""


class IntentLLM:
    def __init__(self, usage=None):
        import anthropic  # imported lazily: the default run needs no SDK

        self.client = anthropic.Anthropic()
        self.usage = usage

    def classify(self, text: str) -> dict | None:
        key = hashlib.sha256((MODEL + text).encode("utf-8")).hexdigest()[:24]
        os.makedirs(CACHE_DIR, exist_ok=True)
        path = os.path.join(CACHE_DIR, f"llm_{key}.json")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)
        response = self.client.beta.messages.create(
            model=MODEL,
            max_tokens=256,
            output_config={"effort": "low"},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            messages=[{"role": "user", "content": PROMPT + text}],
        )
        if self.usage is not None:
            self.usage.record(response.model, response.usage.input_tokens, response.usage.output_tokens)
        if response.stop_reason == "refusal":
            return None
        raw = "".join(b.text for b in response.content if b.type == "text").strip()
        start, end = raw.find("{"), raw.rfind("}")
        try:
            verdict = json.loads(raw[start:end + 1])
        except ValueError:
            return None
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(verdict, fh)
        return verdict
