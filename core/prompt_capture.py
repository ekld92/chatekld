"""In-memory capture of the effective system prompt last sent per workflow.

Powers the read-only **Prompt Hub** (``GET /api/prompts`` + the "Prompt Hub"
tab): a transparency panel that shows the EXACT system prompt transmitted to
the LLM for each app workflow, local or online.

WHY a live capture sink instead of a static prompt catalog:
system prompts are assembled at ~12 sites across 9 modules in every possible
style — bare constants (deck review, compile-fix, the refactor French prompts),
``.format()`` slot templates (deck section/augment), a mode-template + user
prefix (vault RAG), plain config passthrough (plain chat), and *fully dynamic*
composition (the thesaurus primer built per query, the retrieved-context block,
the agent preamble that wraps the deckgen prompt). Worse, the two flagship
LOCAL paths never touch ``LLMRequest.system_prompt`` at all: single-paper uses
``provider.stream_chat(system_prompt=…)`` and vault RAG bakes the prompt into a
LlamaIndex ``text_qa_template``. So a catalog that printed the templates would
be *lying* about what was actually sent. The only faithful answer to "what did
we send the model?" is to record the final string from the real send paths —
which is what this module does, from a handful of choke points (the two
``core.llm.factory`` streaming seams, the agent loop, and the two local
bypasses), plus the vision/OCR instruction prompts.

Design constraints:
  * Thread-safe — several workflows (a deck generate + a vault chat) can run
    concurrently, each on its own Waitress worker thread.
  * Bounded — only the LATEST capture per workflow is kept, and every stored
    string is size-capped, so a multi-thousand-token context block can never
    grow memory without bound.
  * Redacted — every stored string passes through ``core.llm.redact.redact``
    because captures can embed retrieved vault text and (defensively) must
    never surface an API key.
  * Never fatal — ``record`` swallows every exception. A transparency feature
    must not be able to break a generation it is only observing.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Optional

from core.llm.redact import redact

logger = logging.getLogger(__name__)

# Per-entry size ceilings. System prompts are legitimately long (the deck macro
# cheatsheet, the composed vault QA template, the French refactor prompts), so
# the cap is generous — display, not storage of record. 12 workflows x 40 KB is
# a ~0.5 MB worst-case footprint, trivially bounded.
_SYSTEM_PROMPT_MAX_CHARS = 40_000
_QUERY_MAX_CHARS = 1_000
_TRUNCATION_MARKER = "\n…[truncated for display]"

# Role of the captured string. Everything routed through core.llm is a genuine
# provider "system" prompt; the vision/OCR prompts are sent as a *user*-role
# instruction to a vision model (there is no system field on that path), so they
# are labelled distinctly rather than misrepresented as system prompts.
ROLE_SYSTEM = "system"
ROLE_USER_INSTRUCTION = "user-instruction"

# The full set of prompt-producing workflows, in display order. Listing them
# statically (rather than only surfacing ones that have fired) lets the Hub show
# a "not captured yet — run this workflow" placeholder for paths the user has
# not exercised this session, so the panel is never mysteriously empty.
WORKFLOWS: tuple[dict, ...] = (
    {
        "id": "paper_summary",
        "label": "Single Paper · summary",
        "description": "Persona + report-type override + audience/language suffixes. "
        "Local passes it as the system param; online sends it in the native system field.",
        "role": ROLE_SYSTEM,
    },
    {
        "id": "vault_rag",
        "label": "Vault RAG · single-shot chat",
        "description": "Answer-mode template + app safety preamble + thesaurus primer + your "
        "override prefix. Local captures the composed QA template (slots filled at query time); "
        "online captures the native system field (primer + prefix).",
        "role": ROLE_SYSTEM,
    },
    {
        "id": "vault_agent",
        "label": "Vault RAG · agent mode",
        "description": "Fixed tool-steering preamble prepended to your override prompt "
        "(+ a forced-final suffix on the last iteration).",
        "role": ROLE_SYSTEM,
    },
    {
        "id": "plain_chat",
        "label": "Plain Chat",
        "description": "The full system prompt you set in LLM Settings — no grounding, no tools.",
        "role": ROLE_SYSTEM,
    },
    {
        "id": "deck_generate",
        "label": "Deck Generator · sections",
        "description": "Section template (.format audience) + image/bib/macro rules, "
        "wrapped under the agent preamble.",
        "role": ROLE_SYSTEM,
    },
    {
        "id": "deck_augment",
        "label": "Deck Generator · augment",
        "description": "Per-operation augment prompt (deepen/table/new-section), "
        "wrapped under the agent preamble.",
        "role": ROLE_SYSTEM,
    },
    {
        "id": "deck_review",
        "label": "Deck Generator · integrity review",
        "description": "Static RAG-free reviewer prompt run over the whole assembled deck.",
        "role": ROLE_SYSTEM,
    },
    {
        "id": "deck_compile_fix",
        "label": "Deck Generator · compile & auto-fix",
        "description": "Static LaTeX-log-repair prompt used by the bounded compile loop.",
        "role": ROLE_SYSTEM,
    },
    {
        "id": "refactor_review",
        "label": "Note Refactor · prose review",
        "description": "Static, shorthand-aware advisory review prompt (writes nothing).",
        "role": ROLE_SYSTEM,
    },
    {
        "id": "refactor_edit",
        "label": "Note Refactor · LLM edits",
        "description": "The rewrite / custom-edit / PDF-summary / chart prompt for the "
        "requested action (the note rides as untrusted source).",
        "role": ROLE_SYSTEM,
    },
    {
        "id": "vision_describe",
        "label": "Vision · image description",
        "description": "Instruction sent to the vision model to describe + transcribe an image. "
        "Sent as a USER-role message (the vision path has no system field), not a system prompt.",
        "role": ROLE_USER_INSTRUCTION,
    },
    {
        "id": "ocr_extract",
        "label": "Vision · scanned-PDF OCR",
        "description": "Pure text-extraction instruction sent to the OCR model. "
        "USER-role message, not a system prompt.",
        "role": ROLE_USER_INSTRUCTION,
    },
)

# Fast id -> descriptor lookup; also the allowlist that keeps a stray/misspelled
# workflow id from silently creating a phantom Hub row.
_WORKFLOW_BY_ID: dict[str, dict] = {w["id"]: w for w in WORKFLOWS}

_LOCK = threading.Lock()
# workflow id -> latest capture dict. Only the most recent send per workflow is
# retained (the panel answers "what was last sent", not a history).
_CAPTURES: dict[str, dict] = {}

# Test/override hook: None => read the persisted ``prompt_capture_enabled`` knob
# (default on); True/False => force it without touching disk. Used by the test
# suite to exercise the disabled path without a config write.
_enabled_override: Optional[bool] = None


def configure(enabled: Optional[bool]) -> None:
    """Force capture on/off (``True``/``False``) or defer to config (``None``).

    Test seam — production leaves this ``None`` so the ``prompt_capture_enabled``
    config knob governs.
    """
    global _enabled_override
    _enabled_override = enabled


def is_enabled() -> bool:
    """Whether capture is active: explicit override wins, else the config knob."""
    if _enabled_override is not None:
        return _enabled_override
    # Read lazily and defensively: a config read must never be able to turn a
    # record() call — which runs inside a live generation — into an exception.
    try:
        from core.config import load_config_readonly

        return bool(load_config_readonly().get("prompt_capture_enabled", True))
    except Exception:
        # Fail OPEN (capture on) — the knob is a convenience, and losing a
        # capture is preferable to a config hiccup silently disabling the panel.
        return True


def _clip(text: str, limit: int) -> str:
    """Redact then hard-cap *text* for display, appending a marker when cut."""
    red = redact(text or "")
    if len(red) <= limit:
        return red
    return red[:limit] + _TRUNCATION_MARKER


def record(
    workflow: str,
    system_prompt: str,
    *,
    provider: str = "",
    model: str = "",
    context_chunks: int = 0,
    query: str = "",
    note: str = "",
) -> None:
    """Record the effective system prompt last sent for *workflow*.

    Called once per LLM request (not per token) from the real send paths, so
    the stored string is exactly what the provider received. Unknown workflow
    ids are dropped (they cannot map to a Hub row). NEVER raises: any failure is
    logged at debug and swallowed so a capture bug cannot break generation.
    """
    try:
        if workflow not in _WORKFLOW_BY_ID:
            # Defensive: a typo in a caller's label would otherwise create an
            # unlabelled ghost row. Log it so the mismatch is diagnosable.
            logger.debug("prompt_capture: unknown workflow id %r ignored", workflow)
            return
        if not is_enabled():
            return
        entry = {
            "workflow": workflow,
            "system_prompt": _clip(system_prompt, _SYSTEM_PROMPT_MAX_CHARS),
            "provider": provider or "",
            "model": model or "",
            "context_chunks": int(context_chunks or 0),
            "query": _clip(query, _QUERY_MAX_CHARS),
            "note": note or "",
            # Epoch seconds; the frontend formats it in the viewer's locale.
            "captured_at": time.time(),
        }
        with _LOCK:
            _CAPTURES[workflow] = entry
    except Exception:
        logger.debug("prompt_capture.record failed", exc_info=True)


def snapshot() -> dict:
    """Return the full Hub payload: every known workflow + its latest capture.

    Shape: ``{"enabled": bool, "workflows": [ {id, label, description, role,
    captured: bool, ...entry fields when captured...}, ... ]}``. Un-run
    workflows appear with ``captured: False`` so the panel lists the complete
    set rather than only what has fired this session.
    """
    with _LOCK:
        # Copy under the lock so a concurrent record() cannot mutate an entry we
        # are serialising.
        captures = {k: dict(v) for k, v in _CAPTURES.items()}
    workflows = []
    for descriptor in WORKFLOWS:
        wid = descriptor["id"]
        row = {
            "id": wid,
            "label": descriptor["label"],
            "description": descriptor["description"],
            "role": descriptor["role"],
        }
        cap = captures.get(wid)
        if cap:
            row["captured"] = True
            row.update(cap)
        else:
            row["captured"] = False
        workflows.append(row)
    return {"enabled": is_enabled(), "workflows": workflows}


def reset() -> None:
    """Drop all captures (test seam / used by ``/api/reset`` cleanup)."""
    with _LOCK:
        _CAPTURES.clear()
