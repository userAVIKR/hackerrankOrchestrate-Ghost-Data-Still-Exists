"""Evaluation workflow for the deletion-audit agent.

Runs three checks and prints a metrics summary:
  1. public samples   - dataset/sample_deletion_requests.csv vs dataset/sample_output.csv
                        (case status, step precision/recall, field and sequence accuracy);
  2. custom cases     - hand-built variants in custom_cases.py (intent, identity, cutoff);
  3. output contract  - validates the root output.csv (if present) for the 518 requests.

Usage (repository root):  python code/evaluation/main.py [--verbose] [--json PATH]
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from ghost_audit.common import DATASET_DIR, REPO_ROOT  # noqa: E402
from ghost_audit.pipeline import AuditAgent  # noqa: E402
from ghost_audit.planner import serialize  # noqa: E402
from custom_cases import CUSTOM_CASES, expected_for  # noqa: E402
from scoring import score  # noqa: E402
from validate_output import validate  # noqa: E402


def load(path):
    with open(path, encoding="utf-8", newline="") as fh:
        return {r["case_id"]: r for r in csv.DictReader(fh)}


def show_diff(cid, pred, gold):
    print(f"--- {cid}: predicted {pred['deletion_status']} / expected {gold['deletion_status']}")
    ps = json.loads(pred["steps_json"])
    gs = json.loads(gold["steps_json"])
    fmt = lambda s: f"{s['phase'][:5]} {s['operation']:<26} {s['system_id']:<20} {s['resource_id']:<18} {s['evidence_id']}"
    for i in range(max(len(ps), len(gs))):
        p = fmt(ps[i]) if i < len(ps) else ""
        g = fmt(gs[i]) if i < len(gs) else ""
        print(f"  {'  ' if p == g else '!!'} {p:<100} | {g}")


def run_custom(agent, gold):
    results = []
    by_id = {r["case_id"]: r for r in agent.all_requests}
    for case in CUSTOM_CASES:
        base = by_id[case["base_case"]]
        req = {k: v for k, v in base.items() if not k.startswith("_") and k != "user_id"}
        req.update(case["override"])
        req["case_id"] = case["case_id"]
        status, steps, _ = agent.audit(req, workflow_of=case["base_case"])
        exp_status, exp_steps = expected_for(case, gold)
        ok_status = status == exp_status
        ok_steps = exp_steps is None or json.loads(serialize(steps)) == exp_steps
        if case["case_id"] == "custom_05_earlier_cutoff":
            # Status-only case: additionally no step may cite capture made after the cutoff.
            ok_steps = all(s["evidence_id"] not in ("evidence_92", "evidence_02") for s in steps)
        results.append({"case_id": case["case_id"], "status": status, "expected_status": exp_status,
                        "status_ok": ok_status, "steps_ok": ok_steps, "n_steps": len(steps), "why": case["why"]})
    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--json", help="write the metrics summary to this path")
    args = ap.parse_args()
    agent = AuditAgent()

    gold = load(os.path.join(DATASET_DIR, "sample_output.csv"))
    sample_ids = set(load(os.path.join(DATASET_DIR, "sample_deletion_requests.csv")))
    pred = {r["case_id"]: r for r in agent.run([r for r in agent.all_requests if r["case_id"] in sample_ids])}
    result = score(pred, gold)
    print("== public samples ==")
    print(json.dumps({k: v for k, v in result.items() if k != "failures"}, indent=2))
    print("failures:", result["failures"])
    if args.verbose:
        for cid in result["failures"]:
            show_diff(cid, pred[cid], gold[cid])

    print("\n== custom cases ==")
    custom = run_custom(agent, gold)
    for c in custom:
        flag = "PASS" if c["status_ok"] and c["steps_ok"] else "FAIL"
        print(f"{flag} {c['case_id']:<28} status={c['status']:<15} expected={c['expected_status']:<15} steps={c['n_steps']}")

    print("\n== output contract ==")
    out_path = os.path.join(REPO_ROOT, "output.csv")
    contract = None
    if os.path.exists(out_path):
        contract = validate(out_path, os.path.join(DATASET_DIR, "deletion_requests.csv"),
                            set(agent.evidence.manifest) | {a["activity_id"] for a in agent.ds.activities}
                            | {a["audit_id"] for a in agent.ds.audit_log})
        print("\n".join(contract[:20]) if contract else "output.csv satisfies the submission contract")
    else:
        print("output.csv not found - run python code/main.py first")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump({"samples": result, "custom": custom, "contract_errors": contract,
                       "unresolved_attribution_ties": agent.reasoner.unresolved_ties}, fh, indent=2)


if __name__ == "__main__":
    main()
