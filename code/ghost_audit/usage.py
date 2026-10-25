"""Token / cost accounting for model calls made during a run."""
from __future__ import annotations

import json
import os
from collections import defaultdict
from datetime import datetime, timezone

from .common import CACHE_DIR

# USD per 1M tokens (input, output), Anthropic first-party list prices.
PRICES = {
    "claude-opus-5-5": (4.00, 20.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-haiku-5-5": (0.10, 0.50),
    # Local inference on the participant's own GPU: no per-token charge.
    "Qwen/Qwen2.5-1.5B-Instruct": (0.0, 0.0),
}


class UsageTracker:
    def __init__(self):
        self.calls = defaultdict(lambda: {"calls": 0, "input_tokens": 0, "output_tokens": 0})
        self.local_ocr_images = 0
        self.local_pdf_documents = 0

    def record(self, model, input_tokens, output_tokens):
        c = self.calls[model]
        c["calls"] += 1
        c["input_tokens"] += input_tokens
        c["output_tokens"] += output_tokens

    def summary(self, n_requests: int, requests_scored: int | None = None) -> dict:
        per_model = {}
        tot_in = tot_out = tot_calls = 0
        tot_cost = 0.0
        for model, c in self.calls.items():
            pin, pout = PRICES.get(model, (0.0, 0.0))
            cost = c["input_tokens"] / 1e6 * pin + c["output_tokens"] / 1e6 * pout
            per_model[model] = dict(c, cost_usd=round(cost, 6))
            tot_in += c["input_tokens"]
            tot_out += c["output_tokens"]
            tot_calls += c["calls"]
            tot_cost += cost
        total = tot_in + tot_out
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "requests": n_requests,
            "requests_scored_by_models": requests_scored,
            "per_model": per_model,
            "model_calls": tot_calls,
            "input_tokens": tot_in,
            "output_tokens": tot_out,
            "total_tokens": total,
            "avg_tokens_per_request": total / n_requests if n_requests else 0,
            "cost_usd": round(tot_cost, 6),
            "cost_per_request_usd": round(tot_cost / n_requests, 8) if n_requests else 0,
            "local_ocr_images": self.local_ocr_images,
            "local_pdf_documents": self.local_pdf_documents,
        }

    def write_summary(self, n_requests: int, requests_scored: int | None = None):
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(os.path.join(CACHE_DIR, "usage_last_run.json"), "w", encoding="utf-8") as fh:
            json.dump(self.summary(n_requests, requests_scored), fh, indent=2)
