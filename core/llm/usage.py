"""Token / cost accounting for LLM requests.

The tracker keeps an in-memory rolling window of recent requests plus
an aggregate counter persisted to ``BASE_DIR/llm_usage.jsonl``. The
``/api/usage`` route reads from this same store. Pricing is sourced
from :data:`PRICING_TABLE` and may be overridden per-model via the
``llm_pricing_overrides`` config key — useful when a provider raises
a price between releases of this app.
"""
from __future__ import annotations

import functools
import json
import logging
import os
import threading
import uuid
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Optional

from core.llm.types import LLMUsage

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ModelPricing:
    """USD per 1 million tokens."""

    input: float
    output: float
    cached_input: Optional[float] = None


# Multiplier applied to reported cache-WRITE tokens
# (usage.cache_creation_input_tokens): a 5-minute-TTL cache write bills at
# 1.25× the model's input rate (per platform.claude.com prompt-caching docs,
# 2026-07). GPT-6.1 Sol reports the same 1.25x write premium; the OpenAI
# adapter captures it only for that model. It is inert for other providers. The 1-hour TTL (2×) is not used by the app.
ANTHROPIC_CACHE_WRITE_MULTIPLIER = 1.25

PRICING_TABLE: dict[str, ModelPricing] = {
    # OpenAI — published list prices as of early 2026; treat as defaults
    # that the user can override via llm_pricing_overrides.
    "gpt-6.1-sol": ModelPricing(input=2.00, output=10.00, cached_input=0.10),
    "gpt-5.6-sol": ModelPricing(input=4.00, output=20.00, cached_input=0.40),
    "gpt-5.6-terra": ModelPricing(input=2.00, output=12.00, cached_input=0.20),
    "gpt-5.6-luna": ModelPricing(input=0.20, output=1.20, cached_input=0.02),
    "gpt-5": ModelPricing(input=1.25, output=10.00, cached_input=0.125),
    "gpt-5-mini": ModelPricing(input=0.25, output=2.00, cached_input=0.025),
    "gpt-5-nano": ModelPricing(input=0.05, output=0.40, cached_input=0.005),
    "gpt-4o": ModelPricing(input=2.50, output=10.00, cached_input=1.25),
    "gpt-4o-mini": ModelPricing(input=0.15, output=0.60, cached_input=0.075),
    "gpt-4-turbo": ModelPricing(input=10.00, output=30.00),
    "gpt-4.1": ModelPricing(input=2.00, output=8.00, cached_input=0.50),
    "gpt-4.1-mini": ModelPricing(input=0.40, output=1.60, cached_input=0.10),
    "gpt-4.1-nano": ModelPricing(input=0.10, output=0.40, cached_input=0.025),
    "gpt-3.5-turbo": ModelPricing(input=0.50, output=1.50),
    "o1-preview": ModelPricing(input=15.00, output=60.00),
    "o1-mini": ModelPricing(input=3.00, output=12.00),
    "o1": ModelPricing(input=15.00, output=60.00),
    "o3-mini": ModelPricing(input=1.10, output=4.40),

    # Anthropic — per platform.claude.com pricing, re-verified 2026-07-04
    # Updated 2026-09-23 from official pricing: newer families have different
    # cache-read ratios (Fable 5.1 is 0.025×, Opus 5.5 is 0.05×). The module
    # multiplier applies to 5-minute cache writes. Historical rows remain
    # for older usage records.
    "claude-fable-5": ModelPricing(input=10.00, output=50.00, cached_input=1.00),
    "claude-fable-5-1": ModelPricing(input=10.00, output=50.00, cached_input=0.25),
    "claude-opus-5-5": ModelPricing(input=4.00, output=20.00, cached_input=0.20),
    "claude-opus-5": ModelPricing(input=5.00, output=25.00, cached_input=0.50),
    "claude-mythos-5": ModelPricing(input=10.00, output=50.00, cached_input=1.00),
    "claude-opus-4-8": ModelPricing(input=5.00, output=25.00, cached_input=0.50),
    "claude-opus-4-7": ModelPricing(input=5.00, output=25.00, cached_input=0.50),
    "claude-opus-4-6": ModelPricing(input=5.00, output=25.00, cached_input=0.50),
    "claude-opus-4-5": ModelPricing(input=5.00, output=25.00, cached_input=0.50),
    "claude-sonnet-5": ModelPricing(input=2.00, output=10.00, cached_input=0.20),
    "claude-sonnet-4-6": ModelPricing(input=3.00, output=15.00, cached_input=0.30),
    "claude-sonnet-4-5": ModelPricing(input=3.00, output=15.00, cached_input=0.30),
    "claude-haiku-4-5": ModelPricing(input=1.00, output=5.00, cached_input=0.10),
    "claude-3-5-sonnet-20241022": ModelPricing(input=3.00, output=15.00, cached_input=0.30),
    "claude-3-5-sonnet-latest": ModelPricing(input=3.00, output=15.00, cached_input=0.30),
    "claude-3-5-haiku-20241022": ModelPricing(input=0.80, output=4.00, cached_input=0.08),
    "claude-3-5-haiku-latest": ModelPricing(input=0.80, output=4.00, cached_input=0.08),
    "claude-3-opus-20240229": ModelPricing(input=15.00, output=75.00, cached_input=1.50),

    # Google Gemini
    # cached_input filled 2026-07-22 (audit B9): the Google adapter captures
    # cachedContentTokenCount into cached_input_tokens, so a None here billed
    # Gemini cache reads at the FULL input rate (systematic over-report —
    # published cached rate is 25 % of input). Known residual: gemini-2.5-pro's
    # >200k-prompt long-context tier (higher $/Mtok) is not modelled; these
    # single-rate rows under-report very large-context calls.
    "gemini-2.5-pro": ModelPricing(input=1.25, output=10.00, cached_input=0.31),
    "gemini-3.8-flash": ModelPricing(input=0.75, output=3.75, cached_input=0.075),
    "google/gemini-3.8-flash": ModelPricing(input=0.75, output=3.75, cached_input=0.075),
    "gemini-3.6-flash": ModelPricing(input=0.75, output=3.75, cached_input=0.075),
    "gemini-2.5-flash": ModelPricing(input=0.30, output=2.50, cached_input=0.075),
    "gemini-2.5-flash-lite": ModelPricing(input=0.10, output=0.40, cached_input=0.025),
    "gemini-2.0-flash": ModelPricing(input=0.10, output=0.40, cached_input=0.025),
    "gemini-2.0-flash-exp": ModelPricing(input=0.10, output=0.40, cached_input=0.025),
    "gemini-1.5-pro": ModelPricing(input=1.25, output=5.00, cached_input=0.3125),
    "gemini-1.5-flash": ModelPricing(input=0.075, output=0.30, cached_input=0.01875),

    # DeepSeek — rechecked 2026-09-23. Current Flash is V4.1; the old V4
    # Flash id is only a temporary alias. Published pricing changes by time
    # of day, so these static rows use peak rates to avoid under-reporting.
    "deepseek-flash": ModelPricing(input=0.30, output=1.20, cached_input=0.006),
    "deepseek-v4-pro": ModelPricing(input=1.32, output=3.96, cached_input=0.044),

    # Z.AI / Zhipu GLM — verified 2026-09-23; the Flash rows are free tier.
    "glm-5.2": ModelPricing(input=1.40, output=4.40, cached_input=0.26),
    "glm-5.3": ModelPricing(input=1.40, output=4.40, cached_input=0.26),
    "glm-5": ModelPricing(input=1.00, output=3.20, cached_input=0.20),
    "glm-4.7": ModelPricing(input=0.60, output=2.20, cached_input=0.11),
    "glm-4.7-flash": ModelPricing(input=0.0, output=0.0),
    "glm-4.6": ModelPricing(input=0.60, output=2.20, cached_input=0.11),
    "glm-4.5": ModelPricing(input=0.60, output=2.20, cached_input=0.11),
    "glm-4.5-air": ModelPricing(input=0.20, output=1.10, cached_input=0.03),
    "glm-4.5-flash": ModelPricing(input=0.0, output=0.0),

    # OpenRouter — verified against public GET /api/v1/models on 2026-09-23;
    # per-token prices converted to USD per million tokens.
    # A ":variant" suffix (:free, :batch) has its OWN price — hence the
    # explicit :free row at $0 and the namespaced-id guard in
    # estimate_cost_usd (no base-id fallback for vendor/model ids).
    "anthropic/claude-haiku-4.5": ModelPricing(input=1.00, output=5.00, cached_input=0.10),
    "anthropic/claude-sonnet-5": ModelPricing(input=2.00, output=10.00, cached_input=0.20),
    "openai/gpt-5.4-mini": ModelPricing(input=0.75, output=4.50, cached_input=0.075),
    "openai/gpt-5.4": ModelPricing(input=2.50, output=15.00, cached_input=0.25),
    "google/gemini-3.6-flash": ModelPricing(input=0.75, output=3.75, cached_input=0.075),
    "google/gemini-2.5-pro": ModelPricing(input=1.25, output=10.00, cached_input=0.125),
    "deepseek/deepseek-v4-flash": ModelPricing(input=0.08554, output=0.17108, cached_input=0.017108),
    "meta-llama/llama-4-maverick": ModelPricing(input=0.1875, output=0.6525),
    "moonshotai/kimi-k2.6": ModelPricing(input=0.95, output=4.00, cached_input=0.16),
    "nvidia/nemotron-3-super-120b-a12b:free": ModelPricing(input=0.0, output=0.0),
}

LOCAL_PROVIDER_NAMES = frozenset({"ollama", "lm_studio"})


def estimate_cost_usd(model: str, usage: LLMUsage, overrides: Optional[dict] = None) -> float:
    """Return the estimated USD cost for *usage* on *model*.

    Falls back to 0.0 when the model is unknown — better to under-report
    than to surface a fabricated number. Pricing overrides accept the
    shape ``{"model_id": {"input": <usd_per_mtoken>, "output": <usd_per_mtoken>}}``.
    """
    pricing = None
    custom_pricing = False
    if overrides and model in overrides:
        try:
            # cached_input MUST be float()ed inside this guard too: a quoted
            # number in a hand-edited config ('"0.125"') previously survived
            # construction and only blew up as a TypeError at the cost
            # multiplication — inside the adapters' streaming `finally`, so a
            # fully successful stream ended in an error (2026-07-22 audit M1).
            raw_cached = overrides[model].get("cached_input")
            pricing = ModelPricing(
                input=float(overrides[model]["input"]),
                output=float(overrides[model]["output"]),
                cached_input=None if raw_cached is None else float(raw_cached),
            )
            custom_pricing = True
        except (KeyError, TypeError, ValueError):
            pricing = None
    if pricing is None:
        pricing = PRICING_TABLE.get(model)
    if pricing is None and "/" not in model:
        # split(":"): ollama ids are name:tag ("llama3.2:8b"); the bare name is
        # retried so a hypothetical priced local model still matches. Local
        # models are unpriced today, so this is future-proofing, not dead code.
        # Gated to UN-namespaced ids (2026-08-08): OpenRouter ids are
        # "vendor/model[:variant]" and a :variant carries its OWN price,
        # distinct from its base (typically cheaper — :free is $0 — but not
        # guaranteed for every variant kind), so falling back to the base row
        # would mis-report — the unknown-model $0 fallback under-reports
        # instead, which is the house rule. Curated :variant ids get exact rows.
        pricing = PRICING_TABLE.get(model.split(":")[0])
    if pricing is None:
        return 0.0
    # Explicit overrides are final per-token rates; do not scale them again.
    if model == "gpt-6.1-sol" and usage.input_tokens > 272_000 and not custom_pricing:
        pricing = ModelPricing(input=pricing.input * 2, output=pricing.output * 1.5,
                               cached_input=pricing.cached_input * 2
                               if pricing.cached_input is not None else None)
    cost = (usage.output_tokens / 1_000_000) * pricing.output
    # Cache accounting (Track 5.5). ``input_tokens`` is the TOTAL prompt size
    # (the adapters normalise Anthropic's exclusive counts — see
    # LLMUsage's field notes); the cached subset bills at the model's
    # cached_input (read) rate and the reported cache-write subset at
    # 1.25× input. ``getattr`` default keeps legacy call sites passing a
    # pre-field LLMUsage (or a test stub) costing out as before.
    cached = usage.cached_input_tokens
    creation = getattr(usage, "cache_creation_input_tokens", 0) or 0
    if creation:
        cost += (creation / 1_000_000) * pricing.input * ANTHROPIC_CACHE_WRITE_MULTIPLIER
    if cached and pricing.cached_input is not None:
        regular_input = max(0, usage.input_tokens - cached - creation)
        cost += (regular_input / 1_000_000) * pricing.input
        cost += (cached / 1_000_000) * pricing.cached_input
    else:
        regular_input = max(0, usage.input_tokens - creation)
        cost += (regular_input / 1_000_000) * pricing.input
    return round(cost, 6)


@dataclass
class UsageRecord:
    """One LLM request's accounting row — the unit stored in both the ring and JSONL.

    The same record is appended to the in-memory ring AND serialized to the
    on-disk log; ``uid`` ties the two copies together so :meth:`UsageTracker.summary`
    can de-duplicate them (a record present in both must be counted once).
    """
    timestamp: str
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int
    cost_usd: float
    latency_ms: int
    stream: bool
    success: bool = True
    error_category: str = ""
    # Anthropic cache-write tokens (Track 5.5); defaulted so legacy JSONL
    # rows written before this field still deserialize via UsageRecord(**data).
    cache_creation_input_tokens: int = 0
    # Per-record unique id used to de-duplicate the in-memory ring against
    # the on-disk JSONL in summary().  Defaults to "" so legacy records
    # written before this field still deserialize via UsageRecord(**data).
    uid: str = ""
    # Which app feature made the call (the LLMRequest.workflow Prompt-Hub tag:
    # "vault_chat", "tagging_propose", …). Added 2026-07-18 because the field
    # was threaded through requests but never persisted — every JSONL row read
    # workflow-less, which made the tagging-propose truncation bug undiagnosable
    # from the usage log (nothing attributed the 7-token Gemini replies to
    # Propose). Defaulted so legacy rows still deserialize.
    workflow: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class UsageSummary:
    """Aggregated usage figures across an arbitrary window."""

    total_requests: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cost_usd: float = 0.0
    by_provider: dict[str, dict] = field(default_factory=dict)
    by_model: dict[str, dict] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "total_requests": self.total_requests,
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
            "total_cost_usd": round(self.total_cost_usd, 6),
            "by_provider": self.by_provider,
            "by_model": self.by_model,
        }


class UsageTracker:
    """In-memory ring buffer plus append-only JSONL on disk.

    The JSONL persists across restarts so monthly rollups remain useful;
    the ring buffer keeps recent activity cheap to query for the
    ``/api/usage?window=recent`` view.
    """

    _RING_SIZE = 500

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._recent: deque[UsageRecord] = deque(maxlen=self._RING_SIZE)
        self._log_path: Optional[str] = None
        # Parsed-JSONL cache so the /api/usage poll does not re-read and
        # re-parse the whole append-only log on every request.  Keyed by
        # the file's (size, mtime_ns); any append or rotation changes the
        # key and triggers a re-parse.
        self._disk_cache: list[UsageRecord] = []
        self._disk_cache_key: Optional[tuple[int, int]] = None
        # Incremental summary folds, one per `since_iso` window (perf plan
        # Phase 4.1). See _disk_fold.
        self._folds: dict[Optional[str], dict] = {}

    def configure(self, log_path: Optional[str]) -> None:
        """Point the tracker at *log_path* and drop the parsed-disk cache.

        Resetting the cache forces the next :meth:`summary` to re-read the new file
        (the old (size, mtime) key is meaningless for a different path).
        """
        with self._lock:
            self._log_path = log_path
            self._disk_cache = []
            self._disk_cache_key = None
            self._folds = {}

    @property
    def log_path(self) -> Optional[str]:
        """The JSONL this tracker appends to, or ``None`` when unconfigured."""
        with self._lock:
            return self._log_path

    def record(
        self,
        *,
        provider: str,
        model: str,
        usage: LLMUsage,
        latency_ms: int,
        stream: bool,
        success: bool = True,
        billable: Optional[bool] = None,
        error_category: str = "",
        pricing_overrides: Optional[dict] = None,
        workflow: str = "",
    ) -> UsageRecord:
        """Cost out a request, append it to the ring, and persist it to JSONL.

        Thread-safety: the ring append and the ``log_path`` read happen under
        ``self._lock``; the (slower, fallible) file append is done AFTER releasing
        the lock — snapshotting ``log_path`` first — so disk I/O never serialises
        every concurrent recorder. A failed request is recorded with ``cost=0.0``
        (no charge for a call that produced nothing), unless ``billable``
        explicitly preserves reported or estimated usage on failure. A fresh ``uid`` is minted
        for cross-store de-duplication. A disk-write failure is swallowed (the
        in-memory ring still has it); recording must never break the caller's path.

        Cost write-back (improvement plan 0.3): the computed cost is also
        assigned to ``usage.estimated_cost_usd``. Every adapter passes the SAME
        ``LLMUsage`` object it attaches to its ``LLMResponse``, so this one line
        is what makes the agent loop's per-turn ``UsageBudget`` (and any other
        response-side consumer) see real dollars instead of the dataclass's 0.0
        default — no per-adapter costing code needed. When the caller supplies
        no ``pricing_overrides``, the persisted ``llm_pricing_overrides`` config
        is resolved here (stat-cached read; best-effort) so the documented knob
        actually applies to recorded costs.
        """
        if pricing_overrides is None:
            try:
                from core.config import load_config_readonly
                overrides = load_config_readonly().get("llm_pricing_overrides")
                pricing_overrides = dict(overrides) if isinstance(overrides, dict) else None
            except Exception:  # noqa: BLE001 — costing must never break recording
                pricing_overrides = None
        # Belt-and-braces on the same contract: record() runs inside the
        # adapters' stream `finally` blocks, so ANY exception out of the cost
        # estimator turns a successful generation into a reported failure.
        try:
            cost = estimate_cost_usd(model, usage, pricing_overrides) if (success if billable is None else billable) else 0.0
        except Exception:  # noqa: BLE001 — costing must never break recording
            cost = 0.0
        usage.estimated_cost_usd = cost
        record = UsageRecord(
            timestamp=datetime.now(timezone.utc).isoformat(),
            provider=provider,
            model=model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cached_input_tokens=usage.cached_input_tokens,
            cache_creation_input_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
            cost_usd=cost,
            latency_ms=int(latency_ms),
            stream=stream,
            success=success,
            error_category=error_category,
            uid=uuid.uuid4().hex,
            workflow=workflow,
        )
        with self._lock:
            self._recent.append(record)
            log_path = self._log_path
        if log_path:
            try:
                os.makedirs(os.path.dirname(log_path) or ".", exist_ok=True)
                with open(log_path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(record.as_dict()) + "\n")
                    # tell() after the append is the file size for free — no
                    # extra stat on this per-request path.
                    size = fh.tell()
                self._rotate_log_if_large(log_path, size)
            except OSError:
                logger.debug("could not append usage record to %s", log_path, exc_info=True)
        return record

    def recent(self, limit: int = 100) -> list[UsageRecord]:
        """Return up to *limit* most-recent records, snapshotted under the lock.

        Copies the deque inside the lock so the caller iterates a stable list while
        other threads keep appending.
        """
        with self._lock:
            return list(self._recent)[-limit:]

    def records_since(
        self,
        start_iso: str,
        *,
        workflow: Optional[str] = None,
        provider: Optional[str] = None,
        model: Optional[str] = None,
    ) -> list[UsageRecord]:
        """Ring records recorded at/after *start_iso*, optionally filtered.

        Powers the per-answer ``{"usage"}`` SSE frame (export provenance,
        2026-07-22): a streaming route snapshots the UTC ISO timestamp before
        generation and, after the stream ends, sums the matching records. Ring
        only (never disk) — the records of interest were appended seconds ago,
        and the disk parse is the expensive path. ISO-8601 UTC strings compare
        lexicographically, matching :meth:`summary`'s convention. Best-effort by
        design: a concurrent request against the same (workflow, provider,
        model) triple could add its rows to the window — acceptable for a
        single-user app, and the frame is documented as approximate.
        """
        with self._lock:
            snapshot = list(self._recent)
        out: list[UsageRecord] = []
        for record in snapshot:
            if record.timestamp < start_iso:
                continue
            if workflow is not None and record.workflow != workflow:
                continue
            if provider is not None and record.provider != provider:
                continue
            if model is not None and record.model != model:
                continue
            out.append(record)
        return out

    def summary(self, since_iso: Optional[str] = None) -> UsageSummary:
        """Aggregate across the in-memory ring plus on-disk JSONL.

        ``since_iso`` is an ISO-8601 cutoff; records older than that are
        skipped. Use ``None`` for the lifetime total.
        """
        # De-dup the in-memory ring against the on-disk JSONL by per-record
        # uid (not (timestamp, provider), which could collide two distinct
        # requests and under-count one when a disk write failed).
        summary, seen = self._disk_fold(since_iso)

        with self._lock:
            recent = list(self._recent)
        for record in recent:
            if since_iso and record.timestamp < since_iso:
                continue
            if record.uid and record.uid in seen:
                continue
            self._accumulate(summary, record)

        return summary

    # PERF (2026-09-02): the on-disk window is kept as a FOLD, not a list.
    # Before: _disk_records parsed the whole JSONL — up to _MAX_LOG_BYTES
    #   (10 MB) live plus the same again rotated — into UsageRecord
    #   dataclasses, held them for the process lifetime, and summary()
    #   re-folded all of them on every call. The cache key is the log's
    #   (size, mtime_ns) and EVERY LLM call appends, so any /api/usage poll
    #   during generation re-read and re-parsed everything. Measured on a
    #   synthetic 20 000-record log (5.0 MB): 128.6 ms per summary() and
    #   28.2 MB of retained records.
    # Change: per-window incremental folds. Each remembers the byte offset it
    #   has consumed of the live log, the rotated file's signature, the
    #   accumulated UsageSummary and a BOUNDED tail of disk uids; a later
    #   summary() parses only the bytes appended since. Same log: 128.6 ms
    #   cold, 0.6 ms per poll while appending, 1.5 MB retained.
    # Safe because:
    #   The fold accumulates exactly what the full pass accumulated, in the
    #   same order, through the same _accumulate. Anything that could
    #   invalidate an offset — a rotation, a shrunk or replaced live log, a
    #   changed rotated file, a reconfigure — falls back to a full re-fold,
    #   so the incremental path is only ever taken for a pure append. The
    #   ring de-dup is unchanged in kind: the ring holds at most _RING_SIZE
    #   (500) records, so a uid it shares with disk is necessarily among the
    #   last _FOLD_UID_TAIL (1 000) disk uids.
    # Preserves: "Uid de-dup against the ring and the read-both-generations
    #   rotation window" — the rotated generation is still read first and in
    #   full, and a rotation resets the fold rather than skipping records.
    # Lock/thread contract: the module lock is taken only around the fold
    #   dict's read and write, never across the file read or the parse.
    # Pinned by: test_llm.py::TestUsageFold (red before the change:
    #   "20000 != 3" records parsed by the second summary()).
    def _disk_fold(self, since_iso: Optional[str]) -> tuple[UsageSummary, set]:
        """``(summary, seen_uids)`` for the on-disk window, folded incrementally."""
        with self._lock:
            log_path = self._log_path
            cached = self._folds.get(since_iso)
        if not log_path:
            return UsageSummary(), set()

        rot_path = log_path + _ROTATED_SUFFIX
        rot_sig = _file_sig(rot_path)
        live_sig = _file_sig(log_path)

        reusable = (
            cached is not None
            and cached["rot_sig"] == rot_sig
            and live_sig is not None
            and cached["live_sig"] is not None
            # A live log that shrank (rotation, truncation, replacement) or
            # whose inode changed cannot be an append of the one we folded.
            and live_sig[0] >= cached["live_sig"][0]
            and live_sig[2] == cached["live_sig"][2]
        )
        if reusable and cached is not None and live_sig == cached["live_sig"]:
            return _copy_summary(cached["summary"]), set(cached["uid_tail"])

        if reusable and cached is not None:
            summary = _copy_summary(cached["summary"])
            uid_tail = deque(cached["uid_tail"], maxlen=_FOLD_UID_TAIL)
            offset = cached["offset"]
            paths: tuple[tuple[str, int], ...] = ((log_path, offset),)
        else:
            summary = UsageSummary()
            uid_tail = deque(maxlen=_FOLD_UID_TAIL)
            paths = ((rot_path, 0), (log_path, 0))

        end_offset = 0
        for path, start in paths:
            consumed, records = _read_records_from(path, start)
            if path == log_path:
                end_offset = consumed
            for record in records:
                if record.uid:
                    uid_tail.append(record.uid)
                if since_iso and record.timestamp < since_iso:
                    continue
                self._accumulate(summary, record)

        with self._lock:
            self._folds[since_iso] = {
                "rot_sig": rot_sig,
                "live_sig": _file_sig(log_path),
                "offset": end_offset,
                "summary": summary,
                "uid_tail": deque(uid_tail, maxlen=_FOLD_UID_TAIL),
            }
        return _copy_summary(summary), set(uid_tail)

    def _disk_records(self) -> list[UsageRecord]:
        """Return the on-disk records, re-parsing only when the file changed."""
        with self._lock:
            log_path = self._log_path
            cached_key = self._disk_cache_key
            cached = self._disk_cache
        if not log_path:
            return []
        try:
            stat = os.stat(log_path)
            key = (stat.st_size, stat.st_mtime_ns)
        except OSError:
            # Right after a rotation the live log does not exist until the
            # next append, but the .1 generation holds every record —
            # returning [] here lost the whole on-disk window (2026-08-08
            # audit lead, confirmed + fixed 2026-08-09).
            try:
                rstat = os.stat(log_path + _ROTATED_SUFFIX)
            except OSError:
                return []
            key = ("rotated-only", rstat.st_size, rstat.st_mtime_ns)
        if cached_key == key:
            return cached
        records = list(self._iter_disk_records())
        with self._lock:
            self._disk_cache = records
            self._disk_cache_key = key
        return records

    def _iter_disk_records(self):
        """Yield records from the JSONL, skipping any unparseable / schema-drifted line.

        Deliberately tolerant: a truncated final line (crash mid-append), a bad JSON
        line, or a row with unexpected keys (``TypeError`` from ``UsageRecord(**data)``)
        is skipped rather than aborting the whole summary. Called only by
        :meth:`_disk_records`, which caches the result by the file's (size, mtime).

        Reads the rotated ``<log>.1`` FIRST, then the live log (2026-07-30), so a
        rotation does not punch a hole in a summary window that spans it. Records
        are de-duplicated by ``uid`` downstream, so an overlap would be harmless
        anyway.
        """
        log_path = self._log_path
        if not log_path:
            return
        for path in (log_path + _ROTATED_SUFFIX, log_path):
            if not os.path.exists(path):
                continue
            try:
                with open(path, encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            data = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        try:
                            yield UsageRecord(**data)
                        except TypeError:
                            continue
            except OSError:
                continue

    def _rotate_log_if_large(self, log_path: str, size: int) -> None:
        """Roll the append-only log over to ``<log>.1`` once it exceeds the cap.

        ``llm_usage.jsonl`` was append-only and unbounded — one of the Tier-3
        items deferred from the 2026-07-03 perf pass (fixed 2026-07-30). One
        generation is kept: ``os.replace`` is atomic and overwrites any previous
        ``.1``, and :meth:`_iter_disk_records` reads both, so a summary window
        spanning the rotation still sees its records. Best-effort — a failed
        rotation must never break the caller's request path; the log simply
        keeps growing until the next attempt.
        """
        if size <= _MAX_LOG_BYTES:
            return
        # DURABILITY (2026-09-02, performance plan Phase 5): re-stat UNDER the
        # lock before rotating.
        # Before: two recorders whose appends both crossed the cap each ran
        #   os.replace(log, log.1). The first moved the full generation aside;
        #   the second then moved the NEW, nearly-empty live log over it,
        #   destroying every record the first had just preserved.
        # Change: the size is re-read inside the lock and the rotation is
        #   skipped when the live log is already under the cap — i.e. when
        #   someone else has just rotated it.
        # Safe because: the check and the rename are now one critical section,
        #   so at most one rotation happens per generation; a rotation that
        #   loses the race simply does nothing, which is what the caller's
        #   best-effort contract already allows.
        # Pinned by: test_llm.py::TestUsageRotationRace.
        with self._lock:
            try:
                if os.stat(log_path).st_size <= _MAX_LOG_BYTES:
                    return          # another recorder rotated it already
            except OSError:
                return
            try:
                os.replace(log_path, log_path + _ROTATED_SUFFIX)
            except OSError:
                logger.debug("could not rotate usage log %s", log_path,
                             exc_info=True)
                return
            # The parsed-disk cache is keyed on the LIVE file's (size, mtime),
            # which the rotation just invalidated; drop it so the next summary
            # re-reads both generations instead of trusting a stale parse. The
            # incremental folds key on the same signatures and would fall back
            # to a full re-fold on their own; dropping them is cheaper than
            # reasoning about it.
            self._disk_cache = []
            self._disk_cache_key = None
            self._folds = {}

    @staticmethod
    def _accumulate(summary: UsageSummary, record: UsageRecord) -> None:
        """Fold one record into the running totals + per-provider/per-model breakdowns.

        Pure (no shared state, no lock): mutates only the caller-owned *summary*, so
        it is safe to call while iterating a snapshot of the ring/disk records.
        """
        summary.total_requests += 1
        summary.total_input_tokens += record.input_tokens
        summary.total_output_tokens += record.output_tokens
        summary.total_cost_usd += record.cost_usd
        p = summary.by_provider.setdefault(record.provider, {
            "requests": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cost_usd": 0.0,
        })
        p["requests"] += 1
        p["input_tokens"] += record.input_tokens
        p["output_tokens"] += record.output_tokens
        p["cost_usd"] = round(p["cost_usd"] + record.cost_usd, 6)
        m = summary.by_model.setdefault(record.model, {
            "requests": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cost_usd": 0.0,
        })
        m["requests"] += 1
        m["input_tokens"] += record.input_tokens
        m["output_tokens"] += record.output_tokens
        m["cost_usd"] = round(m["cost_usd"] + record.cost_usd, 6)


usage_tracker = UsageTracker()


def usage_frame_payload(records: list[UsageRecord]) -> Optional[dict]:
    """Sum *records* into the ``{"usage"}`` SSE frame payload, or ``None``.

    Shared by the streaming routes (plain chat, single-paper summarise, vault
    single-shot) that attribute an answer's real token/cost figures for export
    provenance. Returns ``None`` when there is nothing to report so callers can
    skip the frame entirely. ``requests`` counts the underlying LLM calls
    (map-reduce summaries and fallback retries make >1 per answer).
    """
    if not records:
        return None
    payload = {
        "input_tokens": sum(r.input_tokens for r in records),
        "output_tokens": sum(r.output_tokens for r in records),
        "cost_usd": round(sum(r.cost_usd for r in records), 6),
        "requests": len(records),
    }
    if payload["input_tokens"] <= 0 and payload["output_tokens"] <= 0:
        return None
    return payload


def configure_default_usage_tracker(base_dir: str) -> None:
    """Point the singleton tracker at ``<base_dir>/llm_usage.jsonl``."""
    usage_tracker.configure(os.path.join(base_dir, "llm_usage.jsonl"))


def unpriced_curated_models() -> list[tuple[str, str]]:
    """``(provider, model_id)`` pairs for curated ids with no pricing entry.

    Track 5.5 guard: a curated model missing from :data:`PRICING_TABLE`
    silently costs out at $0 (the deliberate unknown-model fallback in
    :func:`estimate_cost_usd`) — fine for arbitrary live-listed ids, but the
    CURATED lists are the app's own defaults, so a gap there means every
    request on a default model under-reports to zero. The adapters are
    imported lazily because they import this module at load time (usage
    recording) — a top-level import here would be a cycle.
    Pinned empty by ``test_all_curated_models_are_priced``.
    """
    pairs: list[tuple[str, str]] = []
    try:
        from core.llm.adapters.anthropic import CURATED_MODELS as anthropic_models
        from core.llm.adapters.google import CURATED_MODELS as google_models
        from core.llm.adapters.openai import CURATED_MODELS as openai_models
        from core.llm.adapters.openai_compat import (
            DEEPSEEK_CURATED_MODELS as deepseek_models,
        )
        from core.llm.adapters.openai_compat import ZAI_CURATED_MODELS as zai_models
        from core.llm.adapters.openai_compat import (
            OPENROUTER_CURATED_MODELS as openrouter_models,
        )
    except Exception:  # noqa: BLE001 — a broken adapter must not break costing
        return pairs
    for provider, models in (
        ("openai", openai_models),
        ("anthropic", anthropic_models),
        ("google", google_models),
        ("deepseek", deepseek_models),
        ("zai", zai_models),
        ("openrouter", openrouter_models),
    ):
        for model_id in models:
            if model_id not in PRICING_TABLE:
                pairs.append((provider, model_id))
    return pairs


def log_unpriced_curated_models() -> None:
    """Startup advisory: name every curated model that would cost out at $0."""
    for provider, model_id in unpriced_curated_models():
        logger.warning(
            "PRICING_TABLE has no entry for curated %s model %r — its usage "
            "will cost out at $0.00 until an entry (or an llm_pricing_overrides "
            "row) is added.",
            provider, model_id,
        )


@functools.lru_cache(maxsize=1)
def _cl100k_encoding():
    """The process-global, immutable ``cl100k_base`` encoder.

    ``tiktoken.get_encoding`` reconstructs the encoder (parses the ~1.7 MB BPE
    merge table) on every call; this fires once per local generation lacking a
    provider usage block, so memoize it. lru_cache never caches the raising
    case, so a missing tiktoken still degrades via the caller's except.
    """
    import tiktoken
    return tiktoken.get_encoding("cl100k_base")


#: ``error_category`` for a stream ABANDONED BY ITS CONSUMER — a client
#: disconnect (or any caller closing the generator) propagates ``GeneratorExit``
#: through the adapters' ``for chunk in stream``, which ``except Exception``
#: deliberately does not catch. Before 2026-07-30 that landed in the ``finally``
#: unmarked, so the record claimed a clean ``success=True`` stream.
#:
#: Recorded with ``success=True`` **on purpose**: `record()` forces
#: ``cost = 0.0`` for a failure, but the provider really did generate — and
#: really will bill — the tokens produced up to the cancel, so flagging it as a
#: failure would zero real spend and make the under-reporting worse rather than
#: visible. The category is what makes a cancelled stream identifiable in
#: ``llm_usage.jsonl`` / ``/api/usage``.
#:
#: Residual, deliberately not papered over: on providers whose usage rides the
#: FINAL chunk (OpenAI + the compat adapters), a cancel means no usage block
#: ever arrived. Output tokens are re-estimated from the text actually streamed
#: (those provably existed), but the prompt's input tokens are not fabricated —
#: the payload is not in the generator's scope, and this file's rule is "better
#: to under-report than to surface a fabricated number". Anthropic (usage on
#: ``message_start``), Gemini (per-chunk ``usageMetadata``) and the local
#: adapter (estimate fallback) are unaffected.
CANCELLED_ERROR_CATEGORY = "cancelled"

#: Size cap for ``llm_usage.jsonl`` before it rolls over to ``<log>.1``, and
#: that suffix. One generation is kept and BOTH are read by the summary, so a
#: rotation never punches a hole in a reporting window. 10 MB is ~40k records —
#: far more than the 500-record ring or any realistic /api/usage window.
#: Disk uids retained per fold for the ring de-dup. The ring holds at most
#: UsageTracker._RING_SIZE (500) records, so a uid shared between ring and
#: disk is always inside this tail; the doubled bound is pure headroom.
_FOLD_UID_TAIL = 1000


def _file_sig(path: str):
    """``(size, mtime_ns, inode)`` for *path*, or None when it does not exist."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_size, st.st_mtime_ns, st.st_ino)


def _copy_summary(summary: "UsageSummary") -> "UsageSummary":
    """A deep-enough copy: callers mutate the per-provider/model sub-dicts."""
    out = UsageSummary(
        total_requests=summary.total_requests,
        total_input_tokens=summary.total_input_tokens,
        total_output_tokens=summary.total_output_tokens,
        total_cost_usd=summary.total_cost_usd,
    )
    out.by_provider = {k: dict(v) for k, v in summary.by_provider.items()}
    out.by_model = {k: dict(v) for k, v in summary.by_model.items()}
    return out


def _read_records_from(path: str, offset: int) -> tuple[int, list["UsageRecord"]]:
    """Parse JSONL from *offset*; return ``(new_offset, records)``.

    Tolerant in exactly the way :meth:`UsageTracker._iter_disk_records` is: a
    truncated final line, a bad JSON line or a schema-drifted row is skipped,
    never fatal. A partial trailing line (a crash mid-append) leaves the
    offset BEFORE it, so the next call re-reads it once it is complete.
    """
    records: list[UsageRecord] = []
    try:
        with open(path, "rb") as fh:
            fh.seek(offset)
            data = fh.read()
    except OSError:
        return offset, records
    consumed = offset
    for raw in data.split(b"\n"):
        line_len = len(raw) + 1
        if consumed + line_len > offset + len(data):
            # The trailing fragment has no newline yet: leave it unconsumed.
            break
        consumed += line_len
        line = raw.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            continue
        try:
            records.append(UsageRecord(**parsed))
        except TypeError:
            continue
    return consumed, records


_MAX_LOG_BYTES = 10 * 1024 * 1024
_ROTATED_SUFFIX = ".1"


def estimate_tokens(text: str) -> int:
    """Cheap heuristic when a provider does not return usage figures.

    Uses tiktoken's ``cl100k_base`` when available, otherwise falls back
    to a 4-chars-per-token approximation. Good enough for cost estimates
    when no usage block is returned by the provider.
    """
    if not text:
        return 0
    try:
        return len(_cl100k_encoding().encode(text))
    except Exception:
        return max(1, len(text) // 4)


def record_local_usage(
    provider: str,
    model: str,
    *,
    input_text: str = "",
    output_text: str = "",
    input_tokens: Optional[int] = None,
    output_tokens: Optional[int] = None,
    latency_ms: int = 0,
    stream: bool = True,
    success: bool = True,
    error_category: str = "",
    workflow: str = "",
) -> None:
    """Record a LOCAL generation (chat / summary / vision) in the usage log.

    Local providers never return a usage block through the ``core.llm``
    adapter seam (some bypass it entirely — LlamaIndex vault RAG, the
    single-paper summariser's direct ``provider.stream_chat``, the vision
    managers), so those calls were invisible to ``/api/usage``. This helper
    estimates tokens with :func:`estimate_tokens` (unless the caller passes
    exact counts) and appends a record so every call is *visible* even though
    local models are unpriced (cost stays $0). Best-effort and never raises —
    usage accounting must not break a generation path.
    """
    try:
        in_tok = input_tokens if input_tokens is not None else estimate_tokens(input_text)
        out_tok = output_tokens if output_tokens is not None else estimate_tokens(output_text)
        usage = LLMUsage(input_tokens=int(in_tok or 0), output_tokens=int(out_tok or 0))
        usage_tracker.record(
            provider=provider or "local",
            model=model or "",
            usage=usage,
            latency_ms=int(latency_ms or 0),
            stream=stream,
            success=success,
            error_category=error_category,
            workflow=workflow,
        )
    except Exception:  # noqa: BLE001 — recording must never break the caller
        logger.debug("record_local_usage failed", exc_info=True)
