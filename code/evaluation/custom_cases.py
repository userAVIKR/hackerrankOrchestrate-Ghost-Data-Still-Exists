"""Custom evaluation cases created for this solution.

Each case is a variant of a public sample request (same person, same deletion-workflow
run) with one property changed, so the expected answer can be derived by hand from the
rules in problem_statement.md and the participant-visible evidence. They target the
failure modes that matter most in this challenge: request intent, historical
identifiers, case-insensitive matching, and the audit_at evidence cutoff.
"""

from __future__ import annotations

import json

CUSTOM_CASES = [
    {
        "case_id": "custom_01_negated_intent",
        "base_case": "case_21",
        "override": {
            "request_text": "Please do not delete my account. I only want the promotional emails to stop; "
                            "I still use the account for orders.",
        },
        "why": "Mentions deletion but explicitly refuses it (marketing opt-out). Must be NOT_APPLICABLE "
               "with an empty step list even though a completed deletion workflow exists for this person.",
        "expected_status": "NOT_APPLICABLE",
        "expected_steps": [],
    },
    {
        "case_id": "custom_02_historical_email",
        "base_case": "case_12",
        "override": {"email": "minjun.kim90@gmail.com"},
        "why": "Request submitted from the subject's previous address (user_audit_log audit_282). The agent "
               "must resolve it to user_12 and find the analytics contact record that is only linked through "
               "the old address's email hash (evidence_01).",
        "expected_status": "INCOMPLETE",
        "expected_steps_from": "case_12",
    },
    {
        "case_id": "custom_03_uppercase_email",
        "base_case": "case_30",
        "override": {"email": "CAMILLE.BERNARD86@OUTLOOK.COM"},
        "why": "Same request with the email upper-cased. Identity matching must be case-insensitive.",
        "expected_status": "INCOMPLETE",
        "expected_steps_from": "case_30",
    },
    {
        "case_id": "custom_04_later_cutoff",
        "base_case": "case_24",
        "override": {"audit_at": "2026-09-17T13:00:00Z"},
        "why": "Same request audited two days later. The 2026-09-17 records bundle (evidence_02) becomes "
               "admissible and holds a newer capture (09-15T12:00) of the profile and search entry, so both "
               "findings must cite evidence_02 instead of evidence_92. Statuses do not change.",
        "expected_status": "INCOMPLETE",
        "expected_steps_from": "case_24",
        "evidence_rewrite": {"evidence_92": "evidence_02"},
    },
    {
        "case_id": "custom_05_earlier_cutoff",
        "base_case": "case_27",
        "override": {"audit_at": "2026-09-15T11:00:00Z"},
        "why": "case_27 audited before the 11:44 service export (evidence_92) was captured. Without that "
               "capture there is no admissible proof that the retained, redacted, disabled and blocked records "
               "are in their final state, but no admissible evidence of a violation either; the 09-17 bundle "
               "(which shows the profile again) must stay out of scope. The agent must not report a remaining "
               "profile, so the case stays COMPLETE.",
        "expected_status": "COMPLETE",
    },
]


def expected_for(case: dict, gold_rows: dict) -> tuple[str, list | None]:
    if "expected_steps" in case:
        return case["expected_status"], case["expected_steps"]
    if "expected_steps_from" in case:
        steps = json.loads(gold_rows[case["expected_steps_from"]]["steps_json"])
        rewrite = case.get("evidence_rewrite", {})
        for s in steps:
            s["evidence_id"] = rewrite.get(s["evidence_id"], s["evidence_id"])
        return case["expected_status"], steps
    return case["expected_status"], None  # status-only expectation
