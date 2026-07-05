"""Process-wide concurrency gate for user-triggered LOCAL model calls.

Why this exists
---------------
The refactor hub makes three kinds of on-demand *local* model call — vision/OCR
extraction (``extract.py``), advisory prose review (``review.py``), and applyable
LLM edits (``llm_edit.py``). Each module used to own an *independent*
``threading.Lock`` (``_VISION_LOCK`` / ``_REVIEW_LOCK`` / ``_LLM_LOCK``), so each
serialized only *within* its own module. Nothing stopped a vision extraction, a
prose review, and a custom edit from firing **simultaneously** at the same local
backend (LM Studio / Ollama) — up to three concurrent inferences, on top of
whatever the indexer is doing. On a memory-constrained machine that concurrent
load is exactly what triggers the OOM / JIT-model-reload hiccups the deck
per-section retry layer exists to paper over.

``LOCAL_MODEL_LOCK`` replaces all three: at most **one** user-triggered local
model call runs at a time across the whole refactor hub. This only *tightens*
concurrency (strictly safer for memory pressure), never loosens it.

Scope + safety
--------------
* Scoped to the refactor hub's user-triggered calls. It deliberately does **not**
  wrap the indexer's own vision path: the indexer already issues image
  descriptions strictly one-at-a-time, and coupling the refactor hub to the
  indexer's multi-hour run would risk long waits / lock-ordering hazards. So the
  worst-case concurrency is 1 (refactor) + 1 (indexer), down from 3 + 1.
* No refactor path acquires this gate while already holding it, and none nests a
  second gated call inside a first (extract → vision only; review/llm_edit →
  chat only; none calls another gated module while holding the lock). A plain,
  non-reentrant ``Lock`` is therefore correct — reentrancy would defeat the point
  of a concurrency gate. If a future path ever needs to nest gated calls, split
  the inner call out from the lock rather than switching to an ``RLock``.
* These calls run on daemon worker threads (the routes bound them off the request
  thread — see ``api/routes/refactor.py::_run_llm_action_bounded`` and
  ``extract-image``), so a call waiting on this gate blocks only a daemon thread,
  never a Flask request thread; the request thread is bounded by its own join
  deadline. A wedged holder therefore degrades later calls to a timeout, it never
  pins the server.
"""
from __future__ import annotations

import threading

# The single gate. All three refactor local-model callers alias their former
# per-module lock to this object, so acquiring any of them acquires the one gate.
LOCAL_MODEL_LOCK = threading.Lock()
