# Prompt eval net

Two layers guard the prompts touched by the 2026-06 prompt audit.

## 1. `test_prompts.py` (repo root) — model-free, runs in CI
Pins the *structural* invariants of every prompt: grounding language, the
untrusted-content guards, `{context_str}`/`{query_str}` placeholders, the
single-paper sentence-count consistency, the consistent RAG citation wording,
the agent preamble's stop condition + efficiency nudge, the deckgen LaTeX
contract, and the system-prompt length cap. When you intentionally change a
prompt, update the matching assertion in the same commit.

```bash
~/venvs/chatekld2026/bin/python -m pytest test_prompts.py -q
```

## 2. `tests/eval/` — answer-quality golden set
A self-contained fixture vault (`fixtures/`) plus golden pairs (`golden_qa.json`)
that assert what a *grounded* answer must cite/contain (and must not contain, to
catch ungrounded answers).

- `scoring.py` — pure load/score/report (no models).
- `test_scoring.py` — hermetic tests for the scoring layer (runs in CI).
- `run_eval.py` — **live** runner; opt-in via `RUN_LIVE_EVAL=1`.

### Running the live eval (needs Ollama + pulled models)
```bash
RUN_LIVE_EVAL=1 \
EVAL_PROVIDER=ollama EVAL_MODEL=llama3.2 EVAL_EMBED=nomic-embed-text \
~/venvs/chatekld2026/bin/python -m tests.eval.run_eval
```
It writes only to a throwaway `CHATEKLD_BASE_DIR`, never your real app data.

### Before/after comparison
Run it, record the pass rate, switch prompt revisions (`git stash` or check out
the pre-change commit), run again, compare. The **delta** is the signal — this
is a regression tripwire, not a semantic grader.
