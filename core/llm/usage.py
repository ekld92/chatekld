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


PRICING_TABLE: dict[str, ModelPricing] = {
    # OpenAI — published list prices as of early 2026; treat as defaults
    # that the user can override via llm_pricing_overrides.
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

    # Anthropic — per platform.claude.com pricing as of 2026-05. The Opus
    # tier dropped to $5/$25 with Opus 4.5 (2025-11); the old $15/$75 only
    # applies to Claude 3 Opus. Retired-model entries are kept so historical
    # usage records still cost out correctly.
    "claude-fable-5": ModelPricing(input=10.00, output=50.00),
    "claude-opus-4-8": ModelPricing(input=5.00, output=25.00),
    "claude-opus-4-7": ModelPricing(input=5.00, output=25.00),
    "claude-opus-4-6": ModelPricing(input=5.00, output=25.00),
    "claude-opus-4-5": ModelPricing(input=5.00, output=25.00),
    "claude-sonnet-4-6": ModelPricing(input=3.00, output=15.00),
    "claude-sonnet-4-5": ModelPricing(input=3.00, output=15.00),
    "claude-haiku-4-5": ModelPricing(input=1.00, output=5.00),
    "claude-3-5-sonnet-20241022": ModelPricing(input=3.00, output=15.00),
    "claude-3-5-sonnet-latest": ModelPricing(input=3.00, output=15.00),
    "claude-3-5-haiku-20241022": ModelPricing(input=0.80, output=4.00),
    "claude-3-5-haiku-latest": ModelPricing(input=0.80, output=4.00),
    "claude-3-opus-20240229": ModelPricing(input=15.00, output=75.00),

    # Google Gemini
    "gemini-2.5-pro": ModelPricing(input=1.25, output=10.00),
    "gemini-2.5-flash": ModelPricing(input=0.30, output=2.50),
    "gemini-2.5-flash-lite": ModelPricing(input=0.10, output=0.40),
    "gemini-2.0-flash": ModelPricing(input=0.10, output=0.40),
    "gemini-2.0-flash-exp": ModelPricing(input=0.10, output=0.40),
    "gemini-1.5-pro": ModelPricing(input=1.25, output=5.00),
    "gemini-1.5-flash": ModelPricing(input=0.075, output=0.30),
}

LOCAL_PROVIDER_NAMES = frozenset({"ollama", "lm_studio"})


def estimate_cost_usd(model: str, usage: LLMUsage, overrides: Optional[dict] = None) -> float:
    """Return the estimated USD cost for *usage* on *model*.

    Falls back to 0.0 when the model is unknown — better to under-report
    than to surface a fabricated number. Pricing overrides accept the
    shape ``{"model_id": {"input": <usd_per_mtoken>, "output": <usd_per_mtoken>}}``.
    """
    pricing = None
    if overrides and model in overrides:
        try:
            pricing = ModelPricing(
                input=float(overrides[model]["input"]),
                output=float(overrides[model]["output"]),
                cached_input=overrides[model].get("cached_input"),
            )
        except (KeyError, TypeError, ValueError):
            pricing = None
    if pricing is None:
        pricing = PRICING_TABLE.get(model) or PRICING_TABLE.get(model.split(":")[0])
    if pricing is None:
        return 0.0
    cost = (usage.output_tokens / 1_000_000) * pricing.output
    cached = usage.cached_input_tokens
    if cached and pricing.cached_input is not None:
        regular_input = max(0, usage.input_tokens - cached)
        cost += (regular_input / 1_000_000) * pricing.input
        cost += (cached / 1_000_000) * pricing.cached_input
    else:
        cost += (usage.input_tokens / 1_000_000) * pricing.input
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
    # Per-record unique id used to de-duplicate the in-memory ring against
    # the on-disk JSONL in summary().  Defaults to "" so legacy records
    # written before this field still deserialize via UsageRecord(**data).
    uid: str = ""

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

    def configure(self, log_path: str) -> None:
        """Point the tracker at *log_path* and drop the parsed-disk cache.

        Resetting the cache forces the next :meth:`summary` to re-read the new file
        (the old (size, mtime) key is meaningless for a different path).
        """
        with self._lock:
            self._log_path = log_path
            self._disk_cache = []
            self._disk_cache_key = None

    def record(
        self,
        *,
        provider: str,
        model: str,
        usage: LLMUsage,
        latency_ms: int,
        stream: bool,
        success: bool = True,
        error_category: str = "",
        pricing_overrides: Optional[dict] = None,
    ) -> UsageRecord:
        """Cost out a request, append it to the ring, and persist it to JSONL.

        Thread-safety: the ring append and the ``log_path`` read happen under
        ``self._lock``; the (slower, fallible) file append is done AFTER releasing
        the lock — snapshotting ``log_path`` first — so disk I/O never serialises
        every concurrent recorder. A failed request is recorded with ``cost=0.0``
        (no charge for a call that produced nothing) and a fresh ``uid`` is minted
        for cross-store de-duplication. A disk-write failure is swallowed (the
        in-memory ring still has it); recording must never break the caller's path.
        """
        cost = estimate_cost_usd(model, usage, pricing_overrides) if success else 0.0
        record = UsageRecord(
            timestamp=datetime.now(timezone.utc).isoformat(),
            provider=provider,
            model=model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cached_input_tokens=usage.cached_input_tokens,
            cost_usd=cost,
            latency_ms=int(latency_ms),
            stream=stream,
            success=success,
            error_category=error_category,
            uid=uuid.uuid4().hex,
        )
        with self._lock:
            self._recent.append(record)
            log_path = self._log_path
        if log_path:
            try:
                os.makedirs(os.path.dirname(log_path) or ".", exist_ok=True)
                with open(log_path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(record.as_dict()) + "\n")
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

    def summary(self, since_iso: Optional[str] = None) -> UsageSummary:
        """Aggregate across the in-memory ring plus on-disk JSONL.

        ``since_iso`` is an ISO-8601 cutoff; records older than that are
        skipped. Use ``None`` for the lifetime total.
        """
        summary = UsageSummary()
        # De-dup the in-memory ring against the on-disk JSONL by per-record
        # uid (not (timestamp, provider), which could collide two distinct
        # requests and under-count one when a disk write failed).
        seen: set[str] = set()

        for record in self._disk_records():
            if since_iso and record.timestamp < since_iso:
                continue
            if record.uid:
                seen.add(record.uid)
            self._accumulate(summary, record)

        with self._lock:
            recent = list(self._recent)
        for record in recent:
            if since_iso and record.timestamp < since_iso:
                continue
            if record.uid and record.uid in seen:
                continue
            self._accumulate(summary, record)

        return summary

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
            return []
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
        """
        log_path = self._log_path
        if not log_path or not os.path.exists(log_path):
            return
        try:
            with open(log_path, encoding="utf-8") as fh:
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
            return

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


def configure_default_usage_tracker(base_dir: str) -> None:
    """Point the singleton tracker at ``<base_dir>/llm_usage.jsonl``."""
    usage_tracker.configure(os.path.join(base_dir, "llm_usage.jsonl"))


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
