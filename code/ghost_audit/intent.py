"""Request-intent classification: does `request_text` authorise deletion?

A deterministic multilingual rule set runs first. It scores deletion cues against
explicit non-deletion cues and negations ("not asking for deletion", "do not close",
"leave the account active"). Optionally an LLM can be asked when the rules are not
confident (see llm.py); the rules alone classify every request in the dataset.
"""
from __future__ import annotations

import re

DELETION_CUES = [
    r"\bdelet", r"\berase", r"\berasure", r"\bremove (?:my|this|the) (?:account|profile)",
    r"\bclose (?:my|this|the) account", r"\bpermanent(?:ly)? (?:account[- ])?delet",
    r"\bforget me\b", r"right to be forgotten", r"\bwipe\b", r"\bremov(?:e|al|ed)\b",
    r"\b(?:profile|account) (?:is|was) (?:gone|removed|deleted)", r"finish the deletion",
    r"\bclosed? (?:the|my|this) account", r"\bdisconnected my account\b",
    # Portuguese / Spanish / French / Japanese / Korean / German / Italian
    r"exclus[ãa]o", r"eliminaci[óo]n", r"\beliminar\b", r"\bborrar\b", r"suppression", r"\bsupprimer\b",
    r"削除", r"삭제", r"l[öo]schung", r"\bl[öo]schen\b", r"cancellazione", r"\bcancellare\b",
]
NON_DELETION_CUES = [
    r"not (?:asking|requesting) (?:for )?(?:deletion|erasure|to (?:delete|erase))",
    r"not a request to (?:erase|delete)",
    r"\b(?:do not|don't|dont) (?:close|delete|erase|remove)",
    r"without removing", r"leave (?:the|my) (?:account|profile)[^.]* (?:active|in place)",
    r"keep the account", r"continue using", r"still (?:need|use|want)",
    r"marketing lists? only", r"stop the promotional", r"\bunsubscribe\b",
    r"\b(?:send me|need|want|get) a copy\b", r"\bdownload my\b", r"portable format", r"what personal information",
    r"\bcorrect (?:it|my)\b", r"\bmisspelled\b", r"\bupdate my\b", r"out of date",
    r"change the email", r"reset(?:ting)? the login",
]


def classify_request(text: str) -> dict:
    t = (text or "").lower()
    pos = [p for p in DELETION_CUES if re.search(p, t)]
    neg = [p for p in NON_DELETION_CUES if re.search(p, t)]
    # A negated deletion phrase is never a deletion cue.
    if neg:
        authorized = False
        confidence = 0.95 if not pos else 0.8
    elif pos:
        authorized = True
        confidence = 0.95
    else:
        authorized = False
        confidence = 0.5
    return {"authorizes_deletion": authorized, "confidence": confidence,
            "deletion_cues": pos, "non_deletion_cues": neg}
