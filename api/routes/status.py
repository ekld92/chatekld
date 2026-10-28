"""Provider status / model-listing endpoints.

Read-only routes that the UI polls to populate provider/model selectors and
the connection/health badges (/api/status, /api/models, /api/vision-models,
/api/health). Each dispatches on the *active* provider and crosses
the local-vs-online boundary: local providers (``ollama`` / ``lm_studio``) go
through ``core.providers.get_provider`` (the embedding/legacy-chat adapters),
while online providers go through ``core.llm.factory.get_llm_provider`` (the
unified chat interface). For an online provider "ok" only means an API key is
present — there is no live ping. Vision/OCR model listing is provider-scoped to
local backends only (online providers expose no vision interface here).

All routes are gated by the app-level before_request origin guard
(api.security.register_origin_guard).
"""
import logging
import threading
import time

from flask import Blueprint, jsonify, request
from api.security import sanitise_error_msg
from core.providers import get_provider
from api.validators import coerce_enum
from core.providers.server import get_provider_warnings
from core.llm.factory import get_llm_provider, is_online
from core.server_info import ensure_server_info

logger = logging.getLogger(__name__)

_LOCAL_PROVIDERS = frozenset({"ollama", "lm_studio"})
_VISION_KINDS = frozenset({"ocr", "vision"})

status_bp = Blueprint('status', __name__)

# PERF (2026-09-02): three polled probe routes with no collapse of concurrent
# callers.
#   Before:    /api/status probed the local backend on EVERY request (app.js
#              polls it every 15 s) with the generation timeout, which defaults
#              to 0 = unbounded; /api/models did the same on each provider
#              switch; and /api/health had a TTL but computed OUTSIDE its lock,
#              so N concurrent misses all probed. Two open windows doubled all
#              of it.
#   Change:    one small TTL + SINGLE-FLIGHT helper behind all three. A miss
#              elects one caller to compute; every other caller of the same key
#              waits on its Event and shares the answer. Probes are bounded at
#              `PROBE_TIMEOUT_S` (see core/providers/ollama.py).
#   Safe because:
#              the payloads are already stale-tolerant by construction — the
#              health route has advertised a TTL since it was written and the
#              badge is a 15 s poll — and the elected caller computes exactly
#              what the uncached path computed. A computation that RAISES is
#              propagated to every waiter and caches nothing, so an error is
#              never memoised.
#   Preserves: "Never hold `_health_cache_lock` across a probe" — the lock is
#              held only around dict/Event bookkeeping, never across `fn()`.
#              `/api/health`'s server-discovery self-heal still runs on the
#              cache-MISS path only, so a cached hit still costs nothing.
#   Lock/thread contract:
#              one module lock per cache, released before `fn()` runs. Waiters
#              block on a threading.Event, not on the lock.
#   Pinned by: test_status_routes.py::TestProbeCaching
class _TtlSingleFlight:
    """TTL cache that collapses concurrent misses onto ONE computation."""

    def __init__(self, ttl_s: float):
        self.ttl_s = ttl_s
        self._lock = threading.Lock()
        self._entries: dict = {}      # key -> {"at": float, "value": Any}
        self._inflight: dict = {}     # key -> threading.Event

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def get(self, key, fn):
        """Return ``(value, was_cached)``; ``fn()`` runs at most once per miss."""
        while True:
            with self._lock:
                ent = self._entries.get(key)
                if ent is not None and time.monotonic() - ent["at"] < self.ttl_s:
                    return ent["value"], True
                ev = self._inflight.get(key)
                if ev is None:
                    ev = threading.Event()
                    self._inflight[key] = ev
                    leader = True
                else:
                    leader = False
            if leader:
                try:
                    value = fn()
                except BaseException:
                    # Never memoise a failure, and never leave waiters parked.
                    with self._lock:
                        self._inflight.pop(key, None)
                    ev.set()
                    raise
                with self._lock:
                    self._entries[key] = {"at": time.monotonic(), "value": value}
                    self._inflight.pop(key, None)
                ev.set()
                return value, False
            # A follower: wait for the leader, then re-read the cache. Bounded
            # by the probe ceiling plus slack so a leader that dies without
            # setting the Event cannot park a worker forever.
            ev.wait(timeout=_PROBE_WAIT_S)
            with self._lock:
                ent = self._entries.get(key)
                if ent is not None:
                    return ent["value"], True
            # The leader failed (its exception belongs to IT, not to us) — try
            # again, which will elect a new leader.


#: Ceiling on how long a follower waits for the elected caller. Comfortably
#: above `PROBE_TIMEOUT_S` (3 s) so a healthy probe is never abandoned.
_PROBE_WAIT_S = 10.0

#: Short TTLs: /api/status backs a 15 s badge poll, /api/models a selector the
#: user refreshes by switching provider. Both are keyed on the PROVIDER NAME,
#: so switching provider is never served a stale other-provider answer.
_STATUS_CACHE = _TtlSingleFlight(5.0)
_MODELS_CACHE = _TtlSingleFlight(5.0)



@status_bp.route("/api/status")
def api_status():
    """Report the active provider's reachability for the connection badge.

    For an online provider, ``ok`` is the result of ``health_check()`` — which
    only confirms an API key is configured (no network round-trip). For a local
    provider it is ``check_running()`` (a real liveness probe). ``ollama_ok`` is
    kept as a duplicate of ``ok`` for backward compatibility with older UI code.
    ``warnings`` surfaces any accumulated provider-config warnings.
    """

    from core.config import load_config
    cfg = load_config()
    provider_name = cfg.get("provider", "ollama")

    def _probe():
        if is_online(provider_name):
            return get_llm_provider(provider_name).health_check()
        # Bounded at PROBE_TIMEOUT_S, never the generation knob: this route is
        # polled every 15 s and `local_request_timeout_s` defaults to 0.
        return get_provider(provider_name).check_running()

    (ok, err), _cached = _STATUS_CACHE.get(provider_name, _probe)

    return jsonify({
        "provider": provider_name,
        "ollama_ok": ok,  # Keeping key for backward compat
        "ok": ok,
        "online": is_online(provider_name),
        "error": err,
        # Deliberately OUTSIDE the cache: warnings accumulate in-process and
        # cost nothing to read, so a fresh one must not wait out the TTL.
        "warnings": get_provider_warnings(),
    })

@status_bp.route("/api/models")
def api_models():
    """List chat models for the active provider — or for ``?provider=``.

    Online providers return ``CURATED_MODELS`` merged with a best-effort,
    key-gated, short-TTL-cached live fetch (degrading to curated-only without a
    key / offline); local providers return the installed model list. The
    provider name is echoed back so the UI can confirm what it queried, with
    ``configured`` — the chat model persisted for THAT provider
    (``resolve_chat_model``), the prefill of a per-run model field.

    ``?provider=`` (follow-ups lot 5, 2026-09-22) lists another provider
    without touching the configuration — the "Update my library" run picks a
    model for ONE run. A PRESENT-but-unknown name is a 400, never a silent
    fallback to the configured provider (the ``--targets []`` rule): the
    caller would be handed the wrong list and read it as the right one. The
    cache stays keyed on the provider name, so the two never share an entry.
    """
    from core.config import load_config, resolve_chat_model
    from core.llm.factory import ALL_PROVIDER_NAMES
    from core.providers.ollama import PROBE_TIMEOUT_S
    cfg = load_config()
    requested = request.args.get("provider")
    if requested is not None:
        provider_name = str(requested).strip().lower()
        if provider_name not in ALL_PROVIDER_NAMES:
            return jsonify({"error": "Unknown provider. Known: "
                            + ", ".join(sorted(ALL_PROVIDER_NAMES))}), 400
    else:
        provider_name = cfg.get("provider", "ollama")

    def _list():
        if is_online(provider_name):
            return get_llm_provider(provider_name).list_models()
        provider = get_provider(provider_name)
        try:
            # A listing nobody can render in three seconds is a dead backend.
            # Keyword-only and optional, so a provider that has not adopted it
            # (or a test double) still works.
            return provider.get_models(timeout_s=PROBE_TIMEOUT_S)
        except TypeError:
            return provider.get_models()

    (models, err), _cached = _MODELS_CACHE.get(provider_name, _list)
    # `configured` (audit remediation 2026-09-23, N10, maintainer decision):
    # the persisted model only when it is really THIS provider's. The local
    # providers share one field (`llm`), which names the ACTIVE local
    # provider's model — so asking for LM Studio while Ollama is active used
    # to prefill an Ollama model id LM Studio does not have. A non-active
    # local provider now gets "" (type or pick one); an online provider keeps
    # its own persisted field (`openai_model`, …), which is its own.
    active = str(cfg.get("provider", "ollama") or "ollama").strip().lower()
    configured = ("" if not is_online(provider_name) and provider_name != active
                  else resolve_chat_model(cfg, provider_name))
    return jsonify({"models": models, "error": err, "provider": provider_name,
                    "configured": configured})

@status_bp.route("/api/vision-models")
def api_vision_models():
    """List vision/OCR models for a LOCAL backend, plus the current selections.

    Vision/OCR is local-only (Ollama or LM Studio): the ``?provider=`` query arg
    is enum-clamped to ``_LOCAL_PROVIDERS`` and falls back to the configured
    ``vision_provider`` / ``ocr_provider`` for the requested ``?kind=`` (``vision``
    vs ``ocr``, default ``ocr``). The response also echoes the persisted
    provider/model selections (and the live manager singletons' current values)
    so the Settings UI can show what is actually wired up for each kind.
    """
    from core.config import load_config
    from services.vision import glm_ocr_manager, vision_manager

    cfg = load_config()
    requested_provider = coerce_enum(
        request.args.get("provider", "").strip().lower(), _LOCAL_PROVIDERS,
    )
    kind = coerce_enum(
        request.args.get("kind", "ocr").strip().lower(), _VISION_KINDS,
    ) or "ocr"
    default_provider = cfg.get("vision_provider", "ollama") if kind == "vision" else cfg.get("ocr_provider", "ollama")
    model_provider = requested_provider or default_provider
    provider = get_provider(model_provider)
    models, err = provider.get_models()
    selected_provider = cfg.get("vision_provider") if kind == "vision" else cfg.get("ocr_provider")
    selected_model = cfg.get("vision_model") if kind == "vision" else cfg.get("ocr_model")
    return jsonify({
        "models": models,
        "error": err,
        "provider": model_provider or cfg.get("provider", "ollama"),
        "kind": kind,
        "selected_provider": selected_provider or model_provider,
        "selected_model": selected_model or "",
        "ocr_provider": cfg.get("ocr_provider") or glm_ocr_manager.provider,
        "ocr_model": cfg.get("ocr_model") or glm_ocr_manager.model,
        "vision_provider": cfg.get("vision_provider") or vision_manager.provider,
        "vision_model": cfg.get("vision_model") or vision_manager.model,
    })


# Server-side result cache for /api/health. The UI polls every 15 s and each
# uncached probe does live TCP checks against Ollama AND LM Studio plus disk
# reads — without a TTL the badge poll alone hammers both backends 4x/minute
# forever (and doubles up when two windows are open). 10 s keeps the badge at
# most one poll behind reality while collapsing concurrent/burst callers onto
# one probe.
_HEALTH_TTL_S = 10.0
#: Since 2026-09-02 this is a ``_TtlSingleFlight`` rather than a plain
#: ``{at, payload}`` dict + lock. The old shape computed the payload OUTSIDE
#: its lock — correctly, since a lock must never span a probe — which meant N
#: concurrent misses each probed both backends. The helper keeps that property
#: (the lock spans bookkeeping only) and adds single-flight on top.
_HEALTH_CACHE = _TtlSingleFlight(_HEALTH_TTL_S)


def _compute_health() -> dict:
    """One uncached health probe: vector store + local model backends."""
    import os

    from core.config import load_config
    from core.constants import OBSIDIAN_INDEX_DIR
    from rag.lancedb_store import lancedb_table_count
    from rag.vault import obsidian_manager

    cfg = load_config()
    details = {
        "vector_store": {"status": "ok", "error": None},
        "local_model": {"status": "ok", "error": None},
    }

    # 1) Vector store. Which backend an EXISTING index uses is decided by the
    #    on-disk index, not the config knob (the knob only governs fresh
    #    builds) — lancedb_table_count returns -1 when no lancedb table
    #    exists, which routes us to the simple/docstore checks.
    try:
        vault_path = obsidian_manager.get_vault_path()
        if not vault_path:
            details["vector_store"] = {
                "status": "degraded",
                "error": "No vault folder configured.",
            }
        elif not os.path.isdir(vault_path):
            # No raw path in the payload — parity with the app-wide
            # sanitise-before-UI discipline (2026-07-22 audit: /api/health was
            # the one surface emitting raw paths/exception text).
            details["vector_store"] = {
                "status": "error",
                "error": "Configured vault path is not a directory — re-pick "
                         "the vault folder in Settings.",
            }
        else:
            lancedb_rows = lancedb_table_count(OBSIDIAN_INDEX_DIR)
            if lancedb_rows >= 0:
                if lancedb_rows == 0:
                    details["vector_store"] = {
                        "status": "degraded",
                        "error": "LanceDB table exists but holds no vectors.",
                    }
            else:
                # Simple backend (or nothing yet). docstore_doc_count snapshots
                # the LOADED index under the manager's own mutation lock —
                # reading obsidian_manager._index.docstore directly from this
                # poll raced the streaming indexer's inserts.
                count = obsidian_manager.docstore_doc_count()
                if count == 0:
                    details["vector_store"] = {
                        "status": "degraded",
                        "error": "Loaded index docstore is empty.",
                    }
                elif count is None and not os.path.isfile(
                    os.path.join(OBSIDIAN_INDEX_DIR, "docstore.json")
                ):
                    # None = not loaded (lazy-load pending) or briefly busy —
                    # only "and nothing persisted either" is a real finding.
                    details["vector_store"] = {
                        "status": "degraded",
                        "error": "Vault index has not been built yet.",
                    }
    except Exception as exc:
        details["vector_store"] = {
            "status": "error",
            "error": f"Unexpected health check failure: {sanitise_error_msg(exc)}",
        }

    # 2) Local model backends: every distinct LOCAL provider any role is
    #    configured to use (chat/embed/vision/OCR), deduplicated so Ollama is
    #    probed once even when it serves all four roles.
    local_errors = []
    providers_to_check = {
        cfg.get(key, "ollama")
        for key in ("provider", "embed_provider", "vision_provider", "ocr_provider")
    } & _LOCAL_PROVIDERS

    for prov_name in sorted(providers_to_check):
        try:
            prov = get_provider(prov_name)
            ok, err = prov.check_running()
            if not ok:
                local_errors.append(
                    f"{prov_name} is not running: "
                    f"{sanitise_error_msg(err) if err else 'connection refused'}")
                continue
            # A4 first-run UX: a runner that is UP but has ZERO models installed
            # cannot index or chat — the first index/chat would otherwise fail at
            # call time with a model-not-found error and no guidance. Surface an
            # ACTIONABLE hint here (distinct from "not running") so the first-run
            # banner tells the user to pull a model. Only a definitively-empty list
            # with NO list error counts: a transient get_models failure must not be
            # mistaken for "no models" (that would nag a healthy install on a blip).
            try:
                models, models_err = prov.get_models()
            except Exception:
                models, models_err = None, "unavailable"
            if models is not None and not models_err and len(models) == 0:
                local_errors.append(
                    f"{prov_name} is running but has no models installed — pull an "
                    "embedding model (e.g. nomic-embed-text) and a chat model to index and chat."
                )
        except Exception as exc:
            local_errors.append(f"Failed to check {prov_name}: {sanitise_error_msg(exc)}")

    if local_errors:
        details["local_model"] = {
            "status": "degraded",
            "error": "; ".join(local_errors),
        }

    # Overall severity is the worst sub-check: "error" must not be flattened
    # into "degraded" (the pre-fix behaviour) or the UI can't distinguish a
    # broken vault path from a merely-unbuilt index.
    statuses = {details["vector_store"]["status"], details["local_model"]["status"]}
    if "error" in statuses:
        overall_status = "error"
    elif "degraded" in statuses:
        overall_status = "degraded"
    else:
        overall_status = "ok"

    return {"status": overall_status, "details": details}


@status_bp.route("/api/health")
def api_health():
    """Grounded liveness/readiness probe (vector store + local LLM backends).

    TTL-cached and SINGLE-FLIGHTED server-side (see ``_HEALTH_TTL_S``) so the
    UI's 15 s badge poll cannot turn into a per-request probe storm against the
    local backends, and so concurrent misses share one probe rather than each
    running their own.

    This is also where the server-discovery record self-heals: a missing
    ``server.json`` silently disables every CLI/MCP write while the app is
    demonstrably up (observed 2026-08-12, cause unknown — CLAUDE.md § Known
    Issues). Re-asserting it here piggybacks on a poll the UI already makes, so
    the window shrinks from "the rest of the process's life" to one poll. It runs
    on the cache-MISS path only: the UI's 15 s poll against a ~10 s TTL misses
    almost every time, so the repair is prompt while costing a cached hit
    nothing.
    """

    def _miss():
        # The self-heal stays on the MISS path (see the docstring): with the
        # misses now collapsed, it runs once per TTL instead of once per
        # concurrent caller, which is strictly closer to its own intent.
        try:
            ensure_server_info()
        except Exception:  # noqa: BLE001 — discovery is best-effort, never fatal
            logger.warning("server-discovery self-heal failed", exc_info=True)
        return _compute_health()

    payload, _cached = _HEALTH_CACHE.get("health", _miss)
    return jsonify(payload)
