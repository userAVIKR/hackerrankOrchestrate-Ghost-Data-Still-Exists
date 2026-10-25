"""Ghost Data Still Exists - deletion-audit agent entry point.

Usage (from the repository root):
    python code/main.py                      # audit dataset/deletion_requests.csv -> output.csv
    python code/main.py --input dataset/sample_deletion_requests.csv --output sample_pred.csv
    python code/main.py --explain case_45    # print the reasoning trace for one case
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ghost_audit.common import REPO_ROOT  # noqa: E402
from ghost_audit.pipeline import AuditAgent  # noqa: E402
from ghost_audit.usage import UsageTracker  # noqa: E402


def write_output(rows, path):
    with open(path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=["case_id", "deletion_status", "steps_json"])
        writer.writeheader()
        writer.writerows(rows)


def explain(agent: AuditAgent, case_id: str):
    req = next((r for r in agent.all_requests if r["case_id"] == case_id), None)
    if req is None:
        print(f"unknown case {case_id}")
        return
    status, steps, assessment = agent.audit(req)
    print(f"{case_id}: subject={req['user_id']} intent={req['_intent']}")
    print(f"  requested={req['requested_at']} reported={req['reported_status']}@{req['reported_at']} audit={req['audit_at']}")
    for r in assessment.resources:
        print(f"  - {r.resource_id:<18} {r.category:<20} action={r.action:<9} state={r.state:<8} pii={r.pii!s:<5} "
              f"compliant={r.compliant!s:<5} evidence={r.evidence_id or '-':<13} basis={r.basis}  [{'; '.join(r.reasons)}]")
    print(f"  => {status}")
    print(json.dumps(steps, indent=1))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", default=os.path.join(REPO_ROOT, "dataset", "deletion_requests.csv"))
    ap.add_argument("--output", default=os.path.join(REPO_ROOT, "output.csv"))
    ap.add_argument("--explain", help="print the reasoning trace for one case_id and exit")
    ap.add_argument("--use-llm", action="store_true",
                    help="ask an LLM to double-check request intent when the rules are unsure "
                         "(needs ANTHROPIC_API_KEY; the deterministic rules are used otherwise)")
    ap.add_argument("--rules-only", action="store_true",
                    help="skip the local Qwen2.5-1.5B-Instruct intent model (deterministic rules only)")
    args = ap.parse_args()

    t0 = time.time()
    usage = UsageTracker()
    agent = AuditAgent(llm=args.use_llm, usage=usage, intent_model=not args.rules_only)
    if args.explain:
        explain(agent, args.explain)
        return
    with open(args.input, encoding="utf-8", newline="") as fh:
        ids = {r["case_id"] for r in csv.DictReader(fh)}
    requests = [r for r in agent.all_requests if r["case_id"] in ids]
    missing = ids - {r["case_id"] for r in requests}
    if missing:  # input file outside the dataset folder: audit it directly
        with open(args.input, encoding="utf-8", newline="") as fh:
            extra = [r for r in csv.DictReader(fh) if r["case_id"] in missing]
        requests += extra
    rows = agent.run(requests)
    write_output(rows, args.output)
    counts = {}
    for r in rows:
        counts[r["deletion_status"]] = counts.get(r["deletion_status"], 0) + 1
    usage.write_summary(len(rows), requests_scored=len(agent.all_requests))
    intents = [r["_intent"] for r in agent.all_requests]
    if any("model_p_yes" in i for i in intents):
        disagree = [r["case_id"] for r in agent.all_requests if not r["_intent"].get("model_agrees", True)]
        print(f"[agent] local LLM intent check: {len(intents) - len(disagree)}/{len(intents)} agree with the rules"
              f"; disagreements: {disagree or 'none'}")
    print(f"[agent] wrote {len(rows)} rows to {args.output} in {time.time() - t0:.1f}s: {counts}")


if __name__ == "__main__":
    main()
