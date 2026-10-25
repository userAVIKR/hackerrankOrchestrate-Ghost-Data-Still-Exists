"""Local LLM intent classifier: Qwen2.5-1.5B-Instruct from Hugging Face.

Every request's `request_text` is shown to the model with a short instruction, and the
model is asked to answer YES (the message authorises deletion) or NO. Instead of
sampling free text we read the next-token distribution and compare the probabilities
of the "YES" and "NO" answer tokens. That gives one deterministic forward pass per
request and a calibrated confidence P(YES).

The model runs locally (GPU in float16 when available, otherwise CPU), so no API key
is needed and inference cost is $0. Results are cached on disk, keyed by model revision
and text.
"""
from __future__ import annotations

import hashlib
import json
import os

from .common import CACHE_DIR

MODEL_ID = os.environ.get("GHOST_AUDIT_HF_MODEL", "Qwen/Qwen2.5-1.5B-Instruct")
MODEL_REVISION = os.environ.get("GHOST_AUDIT_HF_REVISION", "989aa7980e4cf806f80c7fef2b1adb7bc71aa306")

SYSTEM_PROMPT = (
    "You are a privacy-operations analyst at an online shop. You decide whether a customer's "
    "message authorises the deletion of their account and personal data."
)
USER_TEMPLATE = """Customer message (it may be written in any language):
\"\"\"{text}\"\"\"

Answer YES if the customer asks to delete, erase, remove or close their account or personal data,
or asks you to verify, complete or finish such a deletion.
Answer NO if the customer only wants access to their data, a copy or export, a correction or update,
help logging in, to stop marketing messages, or explicitly says not to delete anything.

Does this message authorise deletion? Answer with exactly one word: YES or NO."""


class HFIntentModel:
    def __init__(self, usage=None, batch_size: int = 8):
        self.usage = usage
        self.batch_size = batch_size
        self.model = None  # weights load lazily, only when a text is not in the cache
        self.device = None

    def _load(self):
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer

        self.torch = torch
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        dtype = torch.float16 if self.device == "cuda" else torch.float32
        self.tokenizer = AutoTokenizer.from_pretrained(MODEL_ID, revision=MODEL_REVISION)
        self.tokenizer.padding_side = "left"
        self.model = AutoModelForCausalLM.from_pretrained(MODEL_ID, revision=MODEL_REVISION, dtype=dtype)
        self.model.to(self.device).eval()
        self.yes_ids = self._answer_ids(["YES", "Yes", " YES", " Yes"])
        self.no_ids = self._answer_ids(["NO", "No", " NO", " No"])

    def _answer_ids(self, words):
        ids = set()
        for w in words:
            toks = self.tokenizer.encode(w, add_special_tokens=False)
            if toks:
                ids.add(toks[0])
        return sorted(ids)

    def _prompt(self, text: str) -> str:
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": USER_TEMPLATE.format(text=text.strip())},
        ]
        return self.tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)

    @staticmethod
    def _key(text: str) -> str:
        return hashlib.sha256(f"{MODEL_ID}@{MODEL_REVISION}\n{text}".encode("utf-8")).hexdigest()[:24]

    def classify_many(self, texts: list[str]) -> list[dict]:
        """Return [{p_yes, authorizes_deletion, input_tokens}] for each text (cached)."""
        os.makedirs(CACHE_DIR, exist_ok=True)
        cache_path = os.path.join(CACHE_DIR, "hf_intent_cache.json")
        cache = {}
        if os.path.exists(cache_path):
            with open(cache_path, encoding="utf-8") as fh:
                cache = json.load(fh)
        todo = sorted({t for t in texts if self._key(t) not in cache})
        if todo and self.model is None:
            self._load()
        for start in range(0, len(todo), self.batch_size):
            batch = todo[start:start + self.batch_size]
            enc = self.tokenizer([self._prompt(t) for t in batch], return_tensors="pt", padding=True).to(self.device)
            with self.torch.no_grad():
                logits = self.model(**enc).logits[:, -1, :].float()
            probs = self.torch.softmax(logits, dim=-1)
            for i, text in enumerate(batch):
                p_yes = float(probs[i, self.yes_ids].sum())
                p_no = float(probs[i, self.no_ids].sum())
                norm = p_yes / (p_yes + p_no) if p_yes + p_no > 0 else 0.5
                cache[self._key(text)] = {
                    "p_yes": round(norm, 6),
                    "answer_mass": round(p_yes + p_no, 6),
                    "input_tokens": int(enc["attention_mask"][i].sum()),
                }
        if todo:
            with open(cache_path, "w", encoding="utf-8") as fh:
                json.dump(cache, fh, indent=0, sort_keys=True)
        out = []
        for t in texts:
            r = dict(cache[self._key(t)])
            r["authorizes_deletion"] = r["p_yes"] >= 0.5
            out.append(r)
        if self.usage is not None:
            # Identical request texts share one forward pass; the scored answer is one output token.
            for t in sorted(set(texts)):
                self.usage.record(MODEL_ID, cache[self._key(t)]["input_tokens"], 1)
        return out
