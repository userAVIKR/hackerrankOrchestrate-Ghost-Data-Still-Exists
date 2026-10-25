# Usage and Cost Report: final full-dataset run

The run that produced `output.csv` was `python code/main.py`, on 2026-10-25, over 518
evaluation requests. The machine-readable counters are in `code/.cache/usage_last_run.json`,
written by `ghost_audit/usage.py`.

## Models used

| Provider | Model | Role | Where it runs |
|---|---|---|---|
| Hugging Face | `Qwen/Qwen2.5-1.5B-Instruct`, revision `989aa79` | Intent classification: does `request_text` authorise deletion? | Locally, NVIDIA RTX 2050 (4 GB) in float16 |
| RapidOCR (PP-OCR ONNX models) | det/rec v4 | Text extraction from 11 evidence screenshots | Locally, CPU |
| pypdf | (no model) | PDF text layer of 4 documents | Locally |
| Anthropic | `claude-opus-5-5` | Optional `--use-llm` second opinion. **Not used in the final run.** | API |

## Token usage and cost: final run

Intent is scored for every public request: the 518 evaluation requests, plus the 32
samples, which share the deletion-workflow activity stream and take part in workflow
attribution. That is 550 requests in total. Requests with identical text share one
forward pass, so the model ran **398 times**.

| Model | Calls | Input tokens | Output tokens | Total tokens | Cost (USD) |
|---|---|---|---|---|---|
| Qwen/Qwen2.5-1.5B-Instruct (local) | 398 | 71,285 | 398 | 71,683 | $0.00 |
| RapidOCR (local) | 11 images | n/a | n/a | n/a | $0.00 |
| claude-opus-5-5 (optional, off) | 0 | 0 | 0 | 0 | $0.00 |
| **Overall** | **398 LLM calls** | **71,285** | **398** | **71,683** | **$0.00** |

| Per request (÷ 518 evaluation requests) | Value |
|---|---|
| Average tokens per request | 138.4 |
| Average input tokens per LLM call | 179.1 |
| Estimated cost per request | $0.00 (local inference; electricity not counted) |

The output tokens are one per call because the model is scored, not sampled: the agent
reads the probability of the "YES" versus "NO" answer token from the next-token logits.
One forward pass therefore yields both the verdict and its confidence, with no
free-text parsing.

## Runtime

| Stage | Time |
|---|---|
| First run: model download (~3 GB, once) | ~2.5 min |
| First run: 398 intent forward passes on a 4 GB GPU | ~8.5 min |
| First run: OCR of 11 screenshots | ~40 s |
| Any later run (model and OCR results cached by content hash) | ~1 s |

## Efficiency choices

- **Scoring instead of generating.** Each call produces 1 output token, not ~40 tokens of JSON.
- **Deduplication.** 550 requests need only 398 calls, because identical texts share a pass.
- **Content-hash caching.** Reruns and the evaluation script cost nothing.
- **Small local model.** It runs on the participant's GPU, so there's no API key, no
  network dependency and no per-token charge.

## Cost if the optional Claude path were enabled

| Scenario | Calls | Input tokens | Output tokens | Cost at $4 / $20 per MTok |
|---|---|---|---|---|
| Final run (flag off) | 0 | 0 | 0 | $0.00 |
| All 518 requests sent to `claude-opus-5-5` | 518 | ~119,000 | ~21,000 | ~$0.89 total, ~$0.0017 per request |
