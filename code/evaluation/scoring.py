"""Scoring helpers that compare predicted rows with reference rows."""
from __future__ import annotations

import json
from collections import Counter

FIELDS = ["phase", "system_id", "resource_id", "operation", "state", "evidence_id"]


def _key(step):
    return tuple(step.get(f, "") for f in FIELDS)


def score(pred_rows: dict, gold_rows: dict) -> dict:
    n = len(gold_rows)
    status_ok = exact = seq_ok = 0
    tp = fp = fn = 0
    field_hits = Counter()
    field_total = 0
    failures = []
    for cid, gold in gold_rows.items():
        pred = pred_rows.get(cid, {"deletion_status": "", "steps_json": "[]"})
        gs = json.loads(gold["steps_json"])
        ps = json.loads(pred["steps_json"])
        s_ok = pred["deletion_status"] == gold["deletion_status"]
        status_ok += s_ok
        gk = Counter(_key(s) for s in gs)
        pk = Counter(_key(s) for s in ps)
        inter = sum((gk & pk).values())
        tp += inter
        fp += sum(pk.values()) - inter
        fn += sum(gk.values()) - inter
        same_seq = [_key(s) for s in gs] == [_key(s) for s in ps]
        seq_ok += same_seq
        exact += same_seq and s_ok
        for i, g in enumerate(gs):
            field_total += 1
            p = ps[i] if i < len(ps) else {}
            for f in FIELDS:
                field_hits[f] += g.get(f) == p.get(f)
        if not (same_seq and s_ok):
            failures.append(cid)
    precision = tp / (tp + fp) if tp + fp else 1.0
    recall = tp / (tp + fn) if tp + fn else 1.0
    return {
        "cases": n,
        "status_accuracy": status_ok / n if n else 0,
        "exact_case_accuracy": exact / n if n else 0,
        "sequence_accuracy": seq_ok / n if n else 0,
        "step_precision": precision,
        "step_recall": recall,
        "step_f1": 2 * precision * recall / (precision + recall) if precision + recall else 0,
        "field_accuracy": {f: field_hits[f] / field_total for f in FIELDS} if field_total else {},
        "failures": failures,
    }
