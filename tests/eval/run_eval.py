"""Live vault-chat golden-set eval (needs a real provider; gated).

This is the answer-quality half of the eval. It indexes the bundled fixture
vault, runs each golden query through the real ``ObsidianVaultManager`` chat
path, and scores the produced answers with ``scoring.py``. Because it calls a
live embedding + chat model it is **opt-in**: nothing runs unless
``RUN_LIVE_EVAL=1`` is set, mirroring ``test_llm.py``'s ``RUN_LIVE_PROVIDER_TESTS``.

How to use it for a before/after comparison
-------------------------------------------
1. With your Ollama (or LM Studio) running and the embed + chat models pulled:

       RUN_LIVE_EVAL=1 \
       EVAL_PROVIDER=ollama EVAL_MODEL=llama3.2 EVAL_EMBED=nomic-embed-text \
       ~/venvs/papermind2026/bin/python -m tests.eval.run_eval

2. Note the pass rate, `git stash` (or checkout the pre-change commit), run it
   again, and compare. The signal is the *delta*, not the absolute number —
   this is a tripwire for grounding/citation regressions, not a semantic grader.

It writes only to a throwaway ``CHATEKLD_BASE_DIR`` (a temp dir when unset), so
it never touches your real ChatEKLD app data, index, or config.

NOTE: this live path has not been executed in the change that introduced it
(no provider was available in that environment). If the manager wiring needs a
tweak on first run, the fix is local to ``main()`` below — the scored data and
the scoring logic are covered by ``test_scoring.py``.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import List

# scoring.py is model-free and does not import core.constants, so importing it
# at module scope is safe even though the heavy core/rag imports below must wait
# until _ensure_base_dir() has fixed CHATEKLD_BASE_DIR.
from tests.eval.scoring import GoldenPair, PairResult, score_answer

_HERE = Path(__file__).resolve().parent
_FIXTURES = _HERE / "fixtures"
_GOLDEN = _HERE / "golden_qa.json"


def _collect_answer(resp) -> str:
    """Normalise the two ``stream_chat`` return shapes to a single answer string.

    The common case is a streaming response exposing ``.response_gen`` (a lazy
    token iterator — LlamaIndex ``StreamingResponse`` on the local path,
    ``_OnlineStreamingResponse`` online). A degenerate query (e.g. the LM Studio
    "no relevant content" sentinel) can return a plain ``Response`` that only
    stringifies. This mirrors how ``api/routes/vault.py`` handles both.
    """
    if hasattr(resp, "response_gen"):
        return "".join(tok for tok in resp.response_gen if tok)
    return str(resp)


def run_pairs(
    manager,
    pairs: List[GoldenPair],
    *,
    model: str,
    embed: str,
    provider: str,
    top_k: int = 4,
) -> List[PairResult]:
    """Run each golden *pair* through ``manager.stream_chat`` and score the answer.

    Pulled out of ``main()`` so the scoring pipeline can be driven hermetically
    (``test_eval_pipeline.py`` passes a fake manager whose ``stream_chat``
    returns canned answers) — that de-risks this wiring without needing a live
    provider, and verifies a grounded answer passes while a hallucinated one
    fails. *manager* only needs the ``stream_chat(query, llm_name=, embed_name=,
    provider_name=, prompt_mode=, top_k=)`` contract the real
    ``ObsidianVaultManager`` exposes.
    """
    results: List[PairResult] = []
    for pair in pairs:
        resp = manager.stream_chat(
            pair.query,
            llm_name=model,
            embed_name=embed,
            provider_name=provider,
            prompt_mode=pair.prompt_mode,
            top_k=top_k,
        )
        results.append(score_answer(_collect_answer(resp), pair))
    return results


def _ensure_base_dir() -> str:
    """Point CHATEKLD_BASE_DIR at a throwaway dir if the caller didn't set one.

    Must run BEFORE any ``core.constants`` import: ``_get_base_dir`` reads the
    env var exactly once at import time, so setting it afterwards has no effect.

    Cleanup is intentionally omitted. When we mint the temp dir we leave it (and
    the indexed fixture vault inside it) on disk after the run so a failure can
    be inspected; a manual eval runs infrequently. Set your own
    CHATEKLD_BASE_DIR, or periodically clear ``$TMPDIR/chatekld-eval-*``, if the
    leftover bytes matter.
    """
    base = os.environ.get("CHATEKLD_BASE_DIR", "").strip()
    if not base:
        base = tempfile.mkdtemp(prefix="chatekld-eval-")
        os.environ["CHATEKLD_BASE_DIR"] = base
    return base


def main() -> int:
    if os.environ.get("RUN_LIVE_EVAL") != "1":
        print("RUN_LIVE_EVAL != 1 — skipping. Set RUN_LIVE_EVAL=1 to run the live eval.")
        return 0

    _ensure_base_dir()
    provider = os.environ.get("EVAL_PROVIDER", "ollama")
    model = os.environ.get("EVAL_MODEL", "llama3.2")
    embed = os.environ.get("EVAL_EMBED", "nomic-embed-text")

    # Import only after the base dir is fixed.
    from core import constants
    from rag.vault import ObsidianVaultManager
    from tests.eval.scoring import format_report, load_pairs

    cfg = {
        "obsidian_vault_path": str(_FIXTURES),
        "provider": provider,
        "llm": model,
        "embed_provider": "ollama",
        # keep retrieval simple + deterministic for a tripwire eval
        "vault_hybrid_enabled": False,
        "vault_reranker_enabled": False,
        "vault_prewarm_enabled": False,
    }
    Path(constants.CONFIG_FILE).write_text(json.dumps(cfg), encoding="utf-8")

    # Use a FRESH manager instance, not rag.vault.obsidian_manager (the app
    # singleton). This script runs standalone against a throwaway base dir, so a
    # private instance shares no index/lock state with any running app. The
    # script is single-threaded, so it adds no concurrency against the manager's
    # internal locks either.
    manager = ObsidianVaultManager()
    print(f"Indexing fixture vault at {_FIXTURES} (provider={provider}, embed={embed}) ...")
    manager.index_vault(model, embed, provider_name=provider)

    # The two stream_chat return shapes are normalised by run_pairs/_collect_answer
    # exactly as api/routes/vault.py handles them (streaming .response_gen vs a
    # plain stringifiable Response for the empty-answer sentinel).
    results = run_pairs(
        manager, load_pairs(_GOLDEN),
        model=model, embed=embed, provider=provider, top_k=4,
    )

    print()
    print(format_report(results))
    return 0


def test_live_golden_eval():
    """Pytest entry point — skipped unless RUN_LIVE_EVAL=1."""
    import pytest

    if os.environ.get("RUN_LIVE_EVAL") != "1":
        pytest.skip("RUN_LIVE_EVAL != 1 (live provider eval is opt-in)")
    assert main() == 0


if __name__ == "__main__":
    raise SystemExit(main())
