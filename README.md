# 👻 Ghost Data Still Exists: an AI Deletion-Audit Agent

> Built for **HackerRank Orchestrate (October 2026)**, an AI-agent building challenge by HackerRank.
> Challenge: [Ghost Data Still Exists](https://www.hackerrank.com/contests/hackerrank-orchestrate-october26/challenges/ghost-data-still-exists)

A company's dashboard says a customer's account deletion is **"Complete"**. Weeks later the
same email is "already registered". The personal data is still sitting in replicas,
search indexes, support tickets, exports, logs and backups, and a sync job is even
recreating it.

This project is an agent that **audits deletion requests**. For each request it:

1. Decides whether the person actually asked for deletion. Requests can be in English,
   Portuguese, Spanish, French, Japanese or Korean.
2. Finds every current **and historical** identifier of the person: old emails, old phone
   numbers, email hashes.
3. Searches 94 evidence files to see what still exists *at the audit time*: JSON
   snapshots, logs in five formats, SQLite dumps, PDFs and screenshots.
4. Applies the deletion policy (delete / redact / anonymize / retain) and returns:
   - `COMPLETE` with proof, or
   - `INCOMPLETE` with findings, an ordered fix plan, and checks, or
   - `NOT_APPLICABLE` if the person never asked for deletion.

All data in this project is fictional and was supplied by the challenge.

---

## 📊 Results

| Metric (32 labelled sample cases) | Score |
|---|---|
| Deletion-status accuracy | **100 %** |
| Exact match (status plus every step in order) | **30 / 32** |
| Step precision / recall | **100 % / 99 %** |
| Custom test cases | **5 / 5 pass** |

- The two misses need record IDs that appear nowhere in the provided data.
- Details: [`code/evaluation/evaluation_report.md`](code/evaluation/evaluation_report.md).

---

## 🖥️ Run it on your PC: step by step

### What you need

| | Minimum | Notes |
|---|---|---|
| OS | Windows 10/11, macOS or Linux | |
| Python | **3.10 or newer** | Developed on Python 3.14 |
| Disk | ~6 GB free | Python packages plus a 3 GB AI model |
| RAM | 8 GB (16 GB recommended) | |
| GPU | Optional | An NVIDIA GPU makes the first run faster; CPU works fine |
| Internet | First run only | To download packages and the model |

### Step 1: Get the code

```bash
git clone https://github.com/<your-username>/<this-repo>.git
cd <this-repo>
```

Run **every command below from this folder**, the one that contains `README.md` and `dataset/`.

### Step 2: Check Python

```bash
python --version        # Windows
python3 --version       # macOS / Linux
```

You need 3.10 or newer. If Python isn't installed, get it from
[python.org/downloads](https://www.python.org/downloads/). On Windows, tick **"Add
python.exe to PATH"** during setup.

> **Windows tip:** if `python` opens the Microsoft Store, use `py` instead in Step 3
> (`py -m venv .venv`).

### Step 3: Create a virtual environment

This keeps the project's packages separate from the rest of your system.

> The repository does **not** include a `.venv` folder (it's git-ignored on purpose). A
> virtual environment only works on the machine and operating system that created it,
> so everyone makes their own with the commands below.

**Windows (PowerShell or the VS Code terminal)**
```powershell
python -m venv .venv
.venv\Scripts\activate
```

**macOS / Linux**
```bash
python3 -m venv .venv
source .venv/bin/activate
```

Your prompt should now start with `(.venv)`.

> **Windows tip:** if PowerShell says *"running scripts is disabled"*, run this once and
> then activate again:
> `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`

### Step 4 (optional): Use your NVIDIA GPU

Skip this step if you don't have an NVIDIA graphics card. Everything also runs on the CPU.

```bash
pip install torch --index-url https://download.pytorch.org/whl/cu126
```

### Step 5: Install the requirements

```bash
pip install -r code/requirements.txt
```

This installs PDF reading, the local OCR engine for screenshots, and PyTorch plus
Hugging Face `transformers` for the AI model.

### Step 6: Run the agent

```bash
python code/main.py
```

- **First run:** downloads the AI model, **Qwen2.5-1.5B-Instruct** (about 3 GB), from
  Hugging Face. It takes about 10 min on a GPU, or 15–30 min on a CPU.
- **Every later run:** about 1 second, because everything is cached.

When it finishes you'll see:

```
[agent] local LLM intent check: 546/550 agree with the rules; disagreements: [...]
[agent] wrote 518 rows to .../output.csv in 0.7s: {'INCOMPLETE': 357, 'COMPLETE': 151, 'NOT_APPLICABLE': 10}
```

The results are in **`output.csv`** in the project folder.

> **Low on disk space, slow PC, or no internet?** Skip the AI model:
> ```bash
> python code/main.py --rules-only
> ```
> The rule engine alone produces the same `output.csv` on this dataset.

### Step 7: Check the accuracy yourself

```bash
python code/evaluation/main.py --verbose
```

This prints:
- the score on the 32 sample cases;
- the 5 custom test cases;
- a check that `output.csv` follows the required format.

### Step 8: See *why* the agent decided something

```bash
python code/main.py --explain case_24
```

This shows every record the agent tied to that person, how it was linked, its state at
audit time, the policy applied, and the evidence file it cited. Try `case_34` (a record
linked through an *old* phone number) or `case_533` (not a deletion request).

---

## 🧠 How it works (short version)

```
request text ──► intent: multilingual rules + local LLM (Qwen2.5-1.5B) ───────┐
emails/phones ─► identity resolution (current + historical identifiers) ──────┤
94 evidence files ─► parsers (JSON, 5 log formats, SQLite, PDF, OCR) ─────────┼─► reasoner ─► planner ─► output.csv
activity log ─► workflow attribution (time window + timing signature) ────────┘
```

- **Evidence over claims.** A workflow that *says* "deletion succeeded" is only a claim.
  The agent trusts captured evidence, and only evidence captured **before the audit
  time**.
- **AI where it helps.** A local Hugging Face LLM independently judges every request's
  intent. It agrees with the rules on 546 of 550 requests; the rules win when they're
  confident.
- **Deterministic where it matters.** IDs, timestamps and policy rules are handled in
  code, so the output is reproducible and can't contain invented record IDs.
- **No API keys or cloud costs.** OCR and the LLM run on your own machine.

Full architecture and decision rules: [`code/README.md`](code/README.md).

---

## 📁 Project structure

```
├── code/
│   ├── main.py                  # entry point → writes output.csv
│   ├── requirements.txt
│   ├── README.md                # architecture & decision logic
│   ├── ghost_audit/             # the agent
│   │   ├── evidence.py          #   parsers for every evidence format
│   │   ├── media.py             #   PDF text + screenshot OCR
│   │   ├── identity.py          #   current + historical identifiers
│   │   ├── intent.py            #   multilingual intent rules
│   │   ├── hf_intent.py         #   local LLM intent check (Qwen2.5-1.5B)
│   │   ├── reasoner.py          #   state at audit time, policy, citations
│   │   └── planner.py           #   ordered findings / fixes / checks
│   └── evaluation/              # scoring, custom cases, reports
├── dataset/                     # challenge data (fictional)
├── problem_statement.md         # full challenge specification
└── output.csv                   # the agent's answers (518 cases)
```

---

## 🛠️ Troubleshooting

| Problem | Fix |
|---|---|
| `python` opens the Microsoft Store | Use `py` instead of `python`, or run `.venv\Scripts\python code\main.py` |
| `No module named 'pypdf'` (or similar) | Activate the venv (Step 3) and rerun Step 5 |
| Strange characters in the output (Windows) | Run `python -X utf8 code/main.py` |
| Model download is slow or fails | Rerun; it resumes. Or use `--rules-only` |
| `CUDA out of memory` | Use a CPU-only torch (skip Step 4) or `--rules-only` |
| Hugging Face "symlinks" warning on Windows | Harmless; to hide it, set `HF_HUB_DISABLE_SYMLINKS_WARNING=1` |

---

## 🏆 About HackerRank Orchestrate

I built this as a participant in **HackerRank Orchestrate, October 2026**. It's a
challenge where participants build an AI agent for a realistic problem, using any
language, model or tools, and then explain it in an AI-judged interview. This
edition's problem, *Ghost Data Still Exists*, is about proving whether "deleted"
personal data is truly gone.

- Challenge page: https://www.hackerrank.com/contests/hackerrank-orchestrate-october26/challenges/ghost-data-still-exists
- Full specification: [`problem_statement.md`](problem_statement.md)

**Built with:** Python · Hugging Face Transformers ·
[Qwen2.5-1.5B-Instruct](https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct) · PyTorch ·
RapidOCR · pypdf · SQLite. Developed with an AI coding assistant (Claude Code).
