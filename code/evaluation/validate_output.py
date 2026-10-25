"""Validate an output.csv against the submission contract (problem_statement.md)."""
from __future__ import annotations

import csv
import json
import sys

PHASES = ["FINDING", "REMEDIATION", "VERIFICATION"]
OPS = {
    "REPORT_REMAINING_RECORD": ("FINDING", "PRESENT"), "REPORT_EXPOSED_PII": ("FINDING", "PRESENT"),
    "REPORT_ACTIVE_DATA_FLOW": ("FINDING", "ACTIVE"), "REPORT_RESTORE_RISK": ("FINDING", "ACTIVE"),
    "DISABLE_DATA_FLOW": ("REMEDIATION", "DISABLED"), "BLOCK_RESTORE": ("REMEDIATION", "BLOCKED"),
    "DELETE_RECORD": ("REMEDIATION", "ABSENT"), "REDACT_RECORD": ("REMEDIATION", "REDACTED"),
    "ANONYMIZE_RECORD": ("REMEDIATION", "ANONYMIZED"), "VERIFY_RECORD_ABSENT": ("VERIFICATION", "ABSENT"),
    "VERIFY_RECORD_PRESENT": ("VERIFICATION", "PRESENT"), "VERIFY_RECORD_REDACTED": ("VERIFICATION", "REDACTED"),
    "VERIFY_RECORD_ANONYMIZED": ("VERIFICATION", "ANONYMIZED"),
    "VERIFY_DATA_FLOW_DISABLED": ("VERIFICATION", "DISABLED"), "VERIFY_RESTORE_BLOCKED": ("VERIFICATION", "BLOCKED"),
}
KEYS = {"step_no", "phase", "system_id", "resource_id", "operation", "state", "evidence_id"}


def validate(output_path: str, requests_path: str, evidence_ids: set[str] | None = None) -> list[str]:
    errors = []
    with open(requests_path, encoding="utf-8", newline="") as fh:
        expected_ids = [r["case_id"] for r in csv.DictReader(fh)]
    with open(output_path, encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames != ["case_id", "deletion_status", "steps_json"]:
            errors.append(f"bad header {reader.fieldnames}")
        rows = list(reader)
    ids = [r["case_id"] for r in rows]
    if sorted(ids) != sorted(expected_ids):
        errors.append(f"case ids differ: {len(ids)} rows vs {len(expected_ids)} requests")
    if ids != sorted(ids):
        errors.append("rows are not sorted by case_id")
    for r in rows:
        cid, status = r["case_id"], r["deletion_status"]
        if status not in ("COMPLETE", "INCOMPLETE", "NOT_APPLICABLE"):
            errors.append(f"{cid}: bad status {status}")
        try:
            steps = json.loads(r["steps_json"])
        except ValueError:
            errors.append(f"{cid}: steps_json is not JSON")
            continue
        if not isinstance(steps, list):
            errors.append(f"{cid}: steps_json is not an array")
            continue
        if status == "NOT_APPLICABLE" and steps:
            errors.append(f"{cid}: NOT_APPLICABLE must have []")
        if status != "NOT_APPLICABLE" and not steps:
            errors.append(f"{cid}: {status} with no steps")
        last_phase = 0
        for i, s in enumerate(steps, 1):
            if set(s) != KEYS:
                errors.append(f"{cid}: step {i} keys {sorted(s)}")
                continue
            if s["step_no"] != i:
                errors.append(f"{cid}: step numbering")
            op = OPS.get(s["operation"])
            if not op or op[0] != s["phase"] or op[1] != s["state"]:
                errors.append(f"{cid}: step {i} inconsistent phase/operation/state")
            ph = PHASES.index(s["phase"]) if s["phase"] in PHASES else -1
            if ph < last_phase:
                errors.append(f"{cid}: phases out of order")
            last_phase = max(last_phase, ph)
            if status == "COMPLETE" and s["phase"] != "VERIFICATION":
                errors.append(f"{cid}: COMPLETE with non-verification step")
            if status == "COMPLETE" and not s["evidence_id"]:
                errors.append(f"{cid}: COMPLETE verification without evidence")
            if status == "INCOMPLETE" and s["phase"] != "FINDING" and s["evidence_id"]:
                errors.append(f"{cid}: future step carries evidence")
            if s["phase"] == "FINDING" and not s["evidence_id"]:
                errors.append(f"{cid}: finding without evidence")
            if evidence_ids is not None and s["evidence_id"] and s["evidence_id"] not in evidence_ids:
                errors.append(f"{cid}: unknown evidence id {s['evidence_id']}")
    return errors


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "output.csv"
    req = sys.argv[2] if len(sys.argv) > 2 else "dataset/deletion_requests.csv"
    errs = validate(out, req)
    print("\n".join(errs) if errs else "output.csv satisfies the submission contract")
    sys.exit(1 if errs else 0)
