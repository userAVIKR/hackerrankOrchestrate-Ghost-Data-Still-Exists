"""Step planning: resource assessments -> ordered FINDING / REMEDIATION / VERIFICATION steps."""
from __future__ import annotations

import json

from .reasoner import CaseAssessment, ResourceAssessment

PHASES = ["FINDING", "REMEDIATION", "VERIFICATION"]
OP_ORDER = {
    "FINDING": ["REPORT_REMAINING_RECORD", "REPORT_EXPOSED_PII", "REPORT_ACTIVE_DATA_FLOW", "REPORT_RESTORE_RISK"],
    # Stop recreation paths first (flows, restores), then change records.
    "REMEDIATION": ["DISABLE_DATA_FLOW", "BLOCK_RESTORE", "DELETE_RECORD", "REDACT_RECORD", "ANONYMIZE_RECORD"],
    "VERIFICATION": ["VERIFY_RECORD_ABSENT", "VERIFY_RECORD_PRESENT", "VERIFY_RECORD_REDACTED",
                     "VERIFY_RECORD_ANONYMIZED", "VERIFY_DATA_FLOW_DISABLED", "VERIFY_RESTORE_BLOCKED"],
}
OP_STATE = {
    "REPORT_REMAINING_RECORD": "PRESENT", "REPORT_EXPOSED_PII": "PRESENT", "REPORT_ACTIVE_DATA_FLOW": "ACTIVE",
    "REPORT_RESTORE_RISK": "ACTIVE", "DISABLE_DATA_FLOW": "DISABLED", "BLOCK_RESTORE": "BLOCKED",
    "DELETE_RECORD": "ABSENT", "REDACT_RECORD": "REDACTED", "ANONYMIZE_RECORD": "ANONYMIZED",
    "VERIFY_RECORD_ABSENT": "ABSENT", "VERIFY_RECORD_PRESENT": "PRESENT", "VERIFY_RECORD_REDACTED": "REDACTED",
    "VERIFY_RECORD_ANONYMIZED": "ANONYMIZED", "VERIFY_DATA_FLOW_DISABLED": "DISABLED",
    "VERIFY_RESTORE_BLOCKED": "BLOCKED",
}

# action -> (finding op, remediation op, verification op)
ACTION_OPS = {
    "DELETE": ("REPORT_REMAINING_RECORD", "DELETE_RECORD", "VERIFY_RECORD_ABSENT"),
    "REDACT": ("REPORT_EXPOSED_PII", "REDACT_RECORD", "VERIFY_RECORD_REDACTED"),
    "ANONYMIZE": ("REPORT_EXPOSED_PII", "ANONYMIZE_RECORD", "VERIFY_RECORD_ANONYMIZED"),
    "RETAIN": (None, None, "VERIFY_RECORD_PRESENT"),
    "DISABLE": ("REPORT_ACTIVE_DATA_FLOW", "DISABLE_DATA_FLOW", "VERIFY_DATA_FLOW_DISABLED"),
    "BLOCK": ("REPORT_RESTORE_RISK", "BLOCK_RESTORE", "VERIFY_RESTORE_BLOCKED"),
}


def _verification_op(r: ResourceAssessment) -> str:
    if r.action in ("REDACT", "ANONYMIZE") and r.compliant and r.state == "ABSENT":
        return "VERIFY_RECORD_ABSENT"
    return ACTION_OPS[r.action][2]


def plan(assessment: CaseAssessment) -> tuple[str, list[dict]]:
    if not assessment.authorized:
        return "NOT_APPLICABLE", []
    violations = [r for r in assessment.resources if not r.compliant]
    status = "INCOMPLETE" if violations else "COMPLETE"
    raw: list[tuple[str, str, ResourceAssessment, str]] = []
    if status == "COMPLETE":
        for r in assessment.resources:
            raw.append(("VERIFICATION", _verification_op(r), r, r.evidence_id))
    else:
        for r in violations:
            finding, remediation, _ = ACTION_OPS[r.action]
            raw.append(("FINDING", finding, r, r.evidence_id))
            raw.append(("REMEDIATION", remediation, r, ""))
        for r in assessment.resources:
            raw.append(("VERIFICATION", ACTION_OPS[r.action][2] if not r.compliant else _verification_op(r), r, ""))

    def key(item):
        phase, op, r, _ = item
        return (PHASES.index(phase), OP_ORDER[phase].index(op), r.system, r.resource_id)

    steps = []
    for n, (phase, op, r, ev) in enumerate(sorted(raw, key=key), start=1):
        steps.append({
            "step_no": n,
            "phase": phase,
            "system_id": r.system,
            "resource_id": r.resource_id,
            "operation": op,
            "state": OP_STATE[op],
            "evidence_id": ev,
        })
    return status, steps


def serialize(steps: list[dict]) -> str:
    return json.dumps(steps, ensure_ascii=False, separators=(",", ":"))
