# Ghost Data Still Exists — Deletion-Audit Agent

An evidence-driven agent that decides, for each deletion request, whether the person's
data is really gone at `audit_at`. When data remains, it plans an ordered remediation.

## Quick start

```bash
# from the repository root
python -m venv .venv
.venv/Scripts/python -m pip install -r code/requirements.txt      # Windows
# .venv/bin/python -m pip install -r code/requirements.txt        # macOS / Linux

python code/main.py                       # -> output.csv (518 rows) at the repo root
python code/evaluation/main.py --verbose  # samples + custom cases + output contract
python code/main.py --explain case_45     # reasoning trace for one case
```

- Python 3.10 or later. Tested with Python 3.14 on Windows.
- A run with a warm OCR cache takes under 1 second. A cold run takes about 45 s, because
  RapidOCR reads the 11 screenshots once and caches the text in `code/.cache/`.
- The default run uses a **local Hugging Face LLM**,
  [`Qwen/Qwen2.5-1.5B-Instruct`](https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct)
  pinned to revision `989aa79`, to classify request intent.
  - About 3 GB, downloaded once on first run.
  - Runs on the GPU in float16 when available, otherwise on the CPU.
  - No API key is needed, and scores are cached in `code/.cache/hf_intent_cache.json`.
  - `--rules-only` skips the model. If `torch` or `transformers` is missing, the agent
    falls back to the rules automatically.
- `--use-llm` is optional and separate: it adds Claude (the `anthropic` SDK, key from
  `ANTHROPIC_API_KEY`) as a further second opinion.

## Architecture

```
dataset/*.csv ──► loader ──► identity index ─────────┐
evidence/**   ──► evidence parsers ──► observations ──┼──► reasoner ──► planner ──► output.csv
system_activity ──► workflow attribution ────────────┘     (state @ audit_at,     (ordered steps)
request_text ──► intent classifier ──────────────────────►  policy, citations)
```

| Module | Responsibility |
|---|---|
| `ghost_audit/evidence.py` | Parses every evidence shape into `Observation(resource, state, pii, identifiers, time, evidence_id)`. Shapes: snapshot bundles, ledgers, service exports, 5 log dialects with trace-id joins, SQLite dumps (primary vs. replica tables), the handover text, PDFs and screenshots. |
| `ghost_audit/media.py` | PDF text layer (pypdf) and screenshot OCR (RapidOCR), cached by file hash. |
| `ghost_audit/identity.py` | Current and historical emails, phones, email hashes and `user_id` per person, from `user_identities.csv` plus `user_audit_log.csv`. |
| `ghost_audit/intent.py` | Multilingual rules (EN/PT/ES/FR/JA/KO) with negation handling. Decides whether `request_text` authorises deletion. |
| `ghost_audit/hf_intent.py` | Local LLM (Qwen2.5-1.5B-Instruct). Asked YES/NO "does this message authorise deletion?"; P(YES) is read from the next-token logits, one deterministic forward pass per distinct text. |
| `ghost_audit/reasoner.py` | Workflow attribution, relevance, state at `audit_at`, policy action, citations. |
| `ghost_audit/planner.py` | Builds FINDING → REMEDIATION → VERIFICATION steps and orders them. |
| `ghost_audit/llm.py` | Optional Claude second opinion on intent (off by default). |
| `evaluation/` | Scorer, custom cases, output-contract validator, and the reports. |

## How a case is decided

1. **Intent (rules plus LLM).** Access, export, correction, password and
   marketing-opt-out requests become `NOT_APPLICABLE` with `[]`. Explicit negations such
   as "do not close" or "not asking for deletion" override deletion words.
   - The local Qwen model scores every request independently.
   - When the rules are confident (≥ 0.9), they decide and the model acts as a logged
     cross-check.
   - When the rules are not confident, the model's verdict decides.
   - On the 550 public requests the two agree on 546. The 4 disagreements are labelled
     sample cases where the rules are right, such as "Can you verify that my account was
     deleted?". The model agrees with the rules on all 518 evaluation requests.
2. **Identity.** The request email is matched case-insensitively against current and
   historical addresses to find the `user_id`. All of that person's identifiers are then
   collected: old phones, email hashes, and the stable reference.
3. **Relevant records.**
   - **Evidence links.** A record whose evidence carries one of the person's identifiers.
     Log lines are joined through their trace ids, so a `lookup_key` on one line links the
     `resource_id` on the completion line.
   - **Data-catalog links.** Records tied to the person by `LINK_RESOURCE_IDENTITY` events.
   - **The deletion workflow's own activity.** Workflow activity carries no identifiers,
     so it is attributed by a small constraint solver:
     - **Time window:** the activity falls between 95 minutes before and 145 minutes after
       the case's `reported_at`.
     - **Status rule:** a primary-profile deletion result must match the workflow's claim
       (`SUCCESS↔COMPLETE`, `PARTIAL↔PARTIALLY_COMPLETE`, `FAILED↔NOT_STARTED`). This holds
       in 252 of 252 evidence-linked cases.
     - **Kinds:** a case gets at most one record of each kind.
     - **Clock signature:** each workflow run emits its events on a shared clock. Modulo
       30 s, "policy action completed" events fall about 10 s after the account deletion,
       and "final result" or check events about 21 s after it. This was learned from
       evidence-linked runs.
     - **Remaining ties:** solved jointly per connected group, by maximising clock
       agreement.
   - **Lifecycle filter.** A record counts only if it was observed between `requested_at`
     and `audit_at`. Older observations belong to earlier requests by the same person.
4. **State at `audit_at`.**
   - Only evidence whose file was captured at or before `audit_at` is admissible. The
     latest admissible observation wins.
   - Within one capture, a copy in any replica table counts as present, so replication
     drift is caught.
   - Workflow results are claims. They are used only when no evidence observed the
     record, and then only as a cited activity (for example, `CHECK_DATA_FLOW … remains
     enabled`).
5. **Policy.** The required treatment comes from `deletion_rules.csv` by data category:
   - **DELETE:** profile, search, export, attachment, marketing, cache;
   - **REDACT:** support message, tombstone, email log;
   - **ANONYMIZE:** analytics, telemetry;
   - **RETAIN:** security audit within retention, legal hold;
   - **DISABLE:** data flows; **BLOCK:** restorable backups.

   The workflow's own operation, when present, refines the category. For example, a
   contact record inside the analytics store gets `DELETE_RECORD`.
6. **Plan.**
   - **Ordering:** phases in order, then the operation order from the problem statement,
     then `system_id`, then `resource_id`.
   - **Remediation order:** flows and restores are stopped before records change.
   - **COMPLETE cases:** verification steps only, each citing the evidence that proves the
     state.
   - **INCOMPLETE cases:** findings cite evidence. Remediation and the post-remediation
     checks use `""`.
   - **Records no file mentions:** the citation falls back to the latest capture of that
     system's own data store that covers the case (for example, an empty
     `customer_profiles` snapshot).

## Guardrails

- Only participant-facing files under `dataset/` are read. There are no golden labels and
  no per-case rules.
- Nothing captured after `audit_at` is cited or used for state.
- No secrets are stored. The optional LLM key comes from the environment.
