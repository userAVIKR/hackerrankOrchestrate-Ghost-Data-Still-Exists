# Evaluation Report: Ghost Data Still Exists deletion-audit agent

Reproduce with `python code/evaluation/main.py --verbose` from the repository root.

The scorer is in `code/evaluation/scoring.py`. Steps are compared on all six graded fields:
`phase, system_id, resource_id, operation, state, evidence_id`.

## 1. Public samples (32 labelled cases)

| Metric | Value |
|---|---|
| Case-level deletion-status accuracy | **100.0 %** (32 / 32) |
| Exact case accuracy (status + full ordered step list) | **93.75 %** (30 / 32) |
| Sequence accuracy (ordered step list identical) | **93.75 %** |
| Step precision (multiset match on all six fields) | **100.0 %** (191 / 191 predicted steps are correct) |
| Step recall | **98.96 %** (191 / 193 gold steps found) |
| Step F1 | **99.48 %** |

Field-level accuracy, position by position against the gold sequence:

| phase | system_id | resource_id | operation | state | evidence_id |
|---|---|---|---|---|---|
| 98.96 % | 96.89 % | 96.89 % | 96.89 % | 96.89 % | 98.96 % |

**The two remaining misses (case_18, case_20).** Each gold answer contains one
`VERIFY_RECORD_PRESENT` step for a security audit record: `audit_record_6645` and
`audit_record_5569`. Neither ID occurs anywhere in the participant data. A full-text
search across every CSV, log, JSON, SQL, PDF and OCR'd screenshot returns nothing. No
evidence-based agent can produce them.

The missing step also shifts the positions of the steps after it, which explains why
the field-level figures sit below step recall. Every other step in those two cases is
correct.

## 2. Intent classification: rules vs. local LLM (Qwen2.5-1.5B-Instruct)

| Classifier | 32 labelled samples (all deletions) | 518 evaluation requests vs. final decision | Notes |
|---|---|---|---|
| Multilingual rules alone | 32 / 32 | 518 / 518 (10 NOT_APPLICABLE) | Confidence ≥ 0.8 on every request |
| Qwen2.5-1.5B alone (P(YES) ≥ 0.5) | 28 / 32 (87.5 %) | 518 / 518 agree | Misses 4 indirect phrasings |
| **Combined (shipped)**: rules decide when confidence ≥ 0.9, otherwise the LLM decides | 32 / 32 | 518 / 518 | Disagreements are logged on every run |

The four LLM misses were case_06, case_19, case_25 and case_28, with P(YES) between 0.28
and 0.47. All four are follow-ups about an existing deletion, for example "Can you verify
that my account was deleted?" or "tell me whether the account … [has] already been
removed". The model reads them as information requests, while the labelled answers treat
them as deletion audits.

That result is why the rules keep authority when they are confident. The LLM is the
tie-breaker for low-confidence wording and an independent check against rule blind spots.

## 3. Custom evaluation cases (5, created for this solution)

Each case is defined in `code/evaluation/custom_cases.py` as a variant of a public case,
with one property changed. The expected answer is derived by hand from the rules.

| Case | What it tests | Expected | Result |
|---|---|---|---|
| `custom_01_negated_intent` | case_21's subject writes: "Please do not delete my account. I only want the promotional emails to stop…" A completed deletion workflow exists for this person, so a keyword matcher would audit it. | `NOT_APPLICABLE`, `[]` | **PASS** |
| `custom_02_historical_email` | case_12 is filed from the subject's *previous* address, `minjun.kim90@gmail.com`. The agent must resolve user_12 and find the analytics contact record linked only through the old address's email hash in a log. | case_12's 5 steps | **PASS** |
| `custom_03_uppercase_email` | case_30 with the email upper-cased. | case_30's 14 steps | **PASS** |
| `custom_04_later_cutoff` | case_24 audited 2026-09-17T13:00 instead of 09-15T12:00. The 09-17 records bundle (`evidence_02`) becomes admissible and holds a newer capture. | Same steps, both findings now cite `evidence_02` | **PASS** |
| `custom_05_earlier_cutoff` | case_27 audited 2026-09-15T11:00, before the 11:44 service export. The 09-17 bundle shows the profile again, but it must stay inadmissible. | `COMPLETE`, no citation of `evidence_92` or `evidence_02` | **PASS** |

## 4. Full evaluation set (518 unlabelled cases)

- **Contract.** `code/evaluation/validate_output.py` passes:
  - 518 rows, sorted by `case_id`, with the exact header;
  - every `steps_json` is a valid array, numbered from 1, with phases in order;
  - operation, phase and state are consistent on every step;
  - `NOT_APPLICABLE` rows have `[]`;
  - every COMPLETE step and every finding cites a real `file_id` or `activity_id`;
  - no remediation or post-remediation step carries evidence.
- **Status mix.** 357 INCOMPLETE, 151 COMPLETE, 10 NOT_APPLICABLE. The 10 match the 10
  requests the privacy-intake log classified as non-deletion.
- **Steps.** 3,251 in total, about 6.3 per case. Each case has exactly one primary
  profile; every other record kind appears at most once, except 5 cases that hold both a
  support attachment (DELETE) and a support message (REDACT).
- **Determinism.** Two consecutive runs, and a cold run with the OCR cache deleted, give a
  byte-identical `output.csv` (md5 `2db95379…`).
- **Attribution.** 61 of about 1,500 deletion-worker activities could not be attributed by
  identifiers, the time window, the status rule or kind exclusivity. They were resolved
  by the joint clock-signature solver (section 5, iteration 7). Because those decisions
  are probabilistic, they are the most likely source of residual error on the hidden set.

## 5. Failures found and improvements made

| # | Exact on samples | Failure observed | Change |
|---|---|---|---|
| 1 | 0 % | No end-to-end run yet. | Built the parsers, identity index, policy table and planner. Linking used evidence identifiers only. |
| 2 | 71.9 % | Accounts, anonymised analytics, and some audit records had no identifiers in any evidence (for example, case_25's `analytics_1895`). | Attributed deletion-worker activities to cases with a time window around `reported_at`, the status rule, and kind exclusivity. Allowed retained audit records seen in later captures to appear in INCOMPLETE verification lists. |
| 3 | 78 % | Order-based tie-breaking gave case_25's analytics record to case_10 and case_26's to case_24. | Learned the workflow clock signature from evidence-linked runs (residues of about 10 s and 21 s, modulo 30 s) and used it as a constraint. |
| 4 | 87.5 % | Hand-built cases broke the status rule: worker "reported the requested operation" events with a different result. A loose tolerance (±2.5 s) produced false phase matches (case_19). | Status rule applied only to "submitted primary profile" events. Direct worker reports bound to the case whose `reported_at` is within 15 minutes. Offsets refit to 9.9 s / 21.4 s with ±1.4 s tolerance. |
| 5 | 90.6 % | Cases whose profile ID appears nowhere (case_04 `account_004`). Evidence shows profile IDs mirror `user_id`s (`account_005`↔`user_05`, `account_8741`↔`user_8741`). | Derived the profile ID from the stable `user_id` when no evidence or workflow names one. |
| 6 | 93.75 % | Absence citations picked a cross-service lookup ledger (`evidence_08`) instead of the account store's own capture (`evidence_54`). | A fallback citation for an unmentioned record must come from a capture of that system's own data store (SQL, snapshot, profile lookups) that covers the case. |
| 7 | 93.75 % (eval-set review) | (a) Records observed after `audit_at` were being pulled in (case_33). (b) Screenshot rows linked through page-level identifiers. (c) case_34's support attachment, linked only through Haruka's *previous* phone in a log captured before her request, was missed. | Relevance became "identity-linked by any evidence up to `audit_at`, and observed during this request's lifecycle". Screenshot rows link only through their own row, except the record the data catalog names. A per-group exhaustive solver replaced greedy tie-breaking. |
| 8 | 93.75 % (unchanged) | The agent made no model calls, which is weak for an "AI-powered" brief. Rules alone can also miss unusual intent wording. | Added a local Hugging Face LLM (Qwen2.5-1.5B-Instruct) that scores every request's intent from the YES/NO next-token probability. On first use it disagreed with the rules on 4 indirect sample phrasings where the rules were right, so the combination rule became: rules decide when confidence ≥ 0.9, otherwise the LLM decides. `output.csv` is byte-identical to the rules-only run. |

## 6. Known limitations

- Workflow events with no identifiers are attributed probabilistically: about 4 % of
  them needed the final joint solver. An error there moves a profile or analytics ID to a
  neighbouring case in the same batch.
- Screenshot state is read with keyword heuristics, for example "Disabled" or "Paused"
  marks a connector inactive, and a listed backup with no block marker counts as
  restorable.
- Audit records whose IDs never appear in participant data (as in case_18 and case_20)
  cannot be produced.
