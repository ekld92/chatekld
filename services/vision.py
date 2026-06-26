"""Image-understanding singletons: vault image description + scanned-PDF OCR.

Two long-lived managers, instantiated once at module load:

* :class:`VisionManager` (``vision_manager``) — describes note-referenced images
  during vault indexing so both their visual content and any embedded text become
  searchable.
* :class:`GLMOCRManager` (``glm_ocr_manager``) — pure OCR for scanned PDF pages
  (single-paper upload + vault PDF indexing).

Both are touched from multiple threads (the indexing worker, the request thread
that flips the configured model in Settings, the availability probe), so each
holds a ``threading.Lock`` over its small mutable state — the configured
model/provider, the availability cache, and the negative-result cooldown. The
governing discipline, applied in every public method: **snapshot the fields you
need under the lock, then make the slow network call OUTSIDE the lock**, so an
in-flight multi-second vision call never blocks ``set_model`` / ``check_availability``
or another describe call's bookkeeping. The configured model is ALWAYS the model
that gets called; ``check_availability`` is informational only (cached, never
gating) and the cooldown only *fast-fails* after a recent failure — neither ever
rewrites ``self.model``.

Call bounds (timeout + max-tokens) are read fresh per call via
:func:`_cfg_bounded_int` so a Settings change applies on the next call without a
restart; they are always on (vision/OCR is never left unbounded) so a stuck local
model cannot stall a multi-hour indexing run. See the root ``CLAUDE.md`` →
*OCR / Vision Availability Caching*.
"""
import hashlib
import base64
import io
import logging
import threading
import time
import ollama

from PIL import Image

from core.providers import get_provider
from core.constants import (
    DEFAULT_OCR_MODEL,
    DEFAULT_VISION_MODEL,
    DEFAULT_VISION_TIMEOUT_S,
    DEFAULT_VISION_MAX_TOKENS,
    DEFAULT_OCR_MAX_TOKENS,
    VISION_MAX_RETRIES,
    VISION_IMAGE_MAX_SIDE,
    OLLAMA_HOST,
)

logger = logging.getLogger(__name__)


def _cfg_bounded_int(key: str, default: int, lo: int, hi: int) -> int:
    """Read an int from config, clamped to ``[lo, hi]``, else *default*.

    Read per call (lazy ``core.config`` import) so a Settings change applies to
    the next vision/OCR call without an app restart — the same discipline as
    ``core.providers.base.local_request_timeout()``.  Unlike that helper the
    result is ALWAYS a usable bound (vision/OCR are never unbounded):

      * a missing / unparseable / non-positive value (``None``, a list, ``0``
        or a negative — all read as "unset / garbage") falls back to the hard
        *default*;
      * a positive but out-of-range value is CLAMPED into ``[lo, hi]``.

    The POST /api/config validator already clamps these keys on save, so the
    clamp here only re-asserts the bound for a hand-edited ``config.json`` that
    bypassed the route — a value of e.g. ``1`` (a stray ``true`` also gives
    ``int(True) == 1``) cannot turn into a 1-second timeout that fails every
    call. The ``lo``/``hi`` mirror ``api/routes/config.py::_CONFIG_VALIDATORS``.
    """
    try:
        from core.config import load_config
        v = int(load_config().get(key, default))
    except Exception:
        return default
    if v <= 0:
        return default
    return max(lo, min(v, hi))

def _model_matches(configured: str, installed: list[str]) -> bool:
    """Return True if *configured* is exactly listed in *installed*, or shares
    a base name (the part before the first colon) with one of them.

    Used for informational availability hints only.  Never substitutes the
    configured model — the user's selection is always honoured at call time.
    """
    if not configured:
        return False
    if configured in installed:
        return True
    base = configured.split(":", 1)[0]
    if not base:
        return False
    return any(m.split(":", 1)[0] == base for m in installed)


class VisionManager:
    """Describes vault images via a vision model (indexing-time, thread-safe singleton).

    Holds the configured model/provider plus two caches that exist only to avoid
    pointless traffic, never to change behaviour:

    * an **availability cache** (``_is_available`` + ``_availability_checked_at``,
      TTL ``_AVAILABILITY_TTL_S``) — an informational hint surfaced to the UI; it
      never gates :meth:`describe_image`.
    * a **negative-result cooldown** (``_call_failure_at``,
      ``_CALL_FAILURE_COOLDOWN_S``) — after a failed call, subsequent calls
      fast-fail to "" for the cooldown window so a vault full of images cannot
      hammer a model that is not loaded.

    All of this state is guarded by ``self._lock``; the slow vision call runs
    *outside* the lock (see the module docstring).
    """
    _AVAILABILITY_TTL_S: float = 60.0
    # Short cool-down after a failed call: avoids hammering the provider with
    # per-image traffic when the configured model is not actually loaded.
    # Cleared by set_model / set_provider so a UI change retries immediately.
    _CALL_FAILURE_COOLDOWN_S: float = 30.0

    def __init__(self, model: str = DEFAULT_VISION_MODEL, provider: str = "ollama"):
        self.model = model
        self.provider = provider
        self._is_available = None
        self._availability_checked_at: float | None = None
        self._call_failure_at: float | None = None
        self._lock = threading.Lock()

    def check_availability(self) -> bool:
        """Best-effort informational probe; never gates a call."""
        with self._lock:
            now = time.monotonic()
            if (
                self._is_available is not None
                and self._availability_checked_at is not None
                and (now - self._availability_checked_at) < self._AVAILABILITY_TTL_S
            ):
                return self._is_available
            try:
                provider = get_provider(self.provider)
                models, _ = provider.get_models()
                self._is_available = _model_matches(self.model, models)
            except Exception:
                self._is_available = False
            self._availability_checked_at = time.monotonic()
            return self._is_available

    def describe_image(self, base64_data: str) -> str:
        """Return a search-oriented description of *base64_data*, or "" on failure.

        Thread-safety: snapshots the configured model/provider and the cooldown
        timestamp under ``self._lock``, then releases it before the (slow) network
        call so a concurrent ``set_model`` is never blocked. A failure records the
        cooldown timestamp (under the lock) and returns ""; a success clears it.
        Returns "" — never raises — so one bad image never aborts an indexing run.
        """
        with self._lock:
            current_model = self.model
            current_provider = self.provider
            failure_at = self._call_failure_at
        # Cooldown is checked AFTER snapshotting and OUTSIDE the lock: a recent
        # failure short-circuits to "" without touching the provider.
        if failure_at is not None and (time.monotonic() - failure_at) < self._CALL_FAILURE_COOLDOWN_S:
            return ""

        try:
            # Indexing-time description for vault images (figures, diagrams,
            # charts, photos, screenshots) — NOT the scanned-PDF OCR path
            # (that is GLMOCRManager.extract_page_text).  A pure "extract all
            # text" prompt returned nothing for text-light visuals, so those
            # images embedded as empty and were dropped; this asks for a short
            # description of what the image depicts AND a transcription of any
            # text/labels/data, so both visual and textual content are
            # searchable.
            prompt = (
                "Describe this image for search and retrieval. In one or two "
                "sentences state what it depicts (e.g. figure, diagram, chart, "
                "photo, screenshot, and its subject), then transcribe any text, "
                "labels, axis titles, numbers, or data visible in it. If it is "
                "simply a scanned page of text, return that text. Report only "
                "what is visible; do not speculate or add commentary."
            )
            # Pre-emptively shrink oversized images so a giant render cannot
            # stall the model on prefill (a 6000px figure is wasted resolution —
            # most VL models downsample internally anyway).  Best-effort: an
            # undecodable image (e.g. HEIC without pillow-heif) is sent as-is.
            payload = _fit_base64_image_to_max_side(base64_data, VISION_IMAGE_MAX_SIDE)
            # Per-call timeout + generation cap, read fresh so a Settings change
            # applies on the next call (ranges mirror the /api/config validator).
            # On hitting vision_max_tokens the model returns
            # finish_reason="length" and the (possibly mid-sentence) text is
            # still cached/embedded — an accepted trade vs. an unbounded runaway,
            # since a truncated description is still useful for retrieval.
            timeout = _cfg_bounded_int("vision_timeout_s", DEFAULT_VISION_TIMEOUT_S, 5, 600)
            max_tokens = _cfg_bounded_int("vision_max_tokens", DEFAULT_VISION_MAX_TOKENS, 64, 8192)
            if current_provider == "lm_studio":
                result = _chat_lm_studio_image(
                    current_model, prompt, payload,
                    timeout=timeout, max_tokens=max_tokens,
                )
            else:
                result = _chat_ollama_image(
                    current_model, prompt, payload,
                    timeout=timeout, max_tokens=max_tokens,
                )
        except Exception as e:
            logger.warning("Vision error: %s", e)
            with self._lock:
                self._call_failure_at = time.monotonic()
            return ""
        with self._lock:
            self._call_failure_at = None
        return result

    def set_model(self, model: str) -> None:
        """Switch the configured model and invalidate both caches immediately.

        Clearing the availability cache + cooldown under the lock means a UI model
        change retries on the very next call instead of waiting out a stale TTL or
        a cooldown earned by the *previous* model.
        """
        with self._lock:
            self.model = model
            self._is_available = None
            self._availability_checked_at = None
            self._call_failure_at = None

    def set_provider(self, provider: str) -> None:
        """Switch the image backend (``ollama``/``lm_studio``) and reset both caches.

        An unrecognised provider is coerced to ``ollama`` — image calls only ever
        target the two local backends. Cache invalidation mirrors :meth:`set_model`.
        """
        with self._lock:
            self.provider = provider if provider in ("ollama", "lm_studio") else "ollama"
            self._is_available = None
            self._availability_checked_at = None
            self._call_failure_at = None

class GLMOCRManager:
    """Pure full-page OCR for scanned PDFs (thread-safe singleton).

    Same model/provider + availability-cache + cooldown machinery as
    :class:`VisionManager` (guarded by ``self._lock``), plus a small bounded
    in-memory result cache (``self._cache``, ≤ ``_OCR_CACHE_MAX_SIZE`` entries,
    FIFO eviction) guarded by a SEPARATE ``self._cache_lock`` so a cache hit never
    contends with the model-config lock. The cache key includes provider + model
    because OCR output is backend-dependent. Unlike the description path this is
    deliberately a pure "extract the text" prompt and does NOT pre-emptively
    downscale (shrinking text hurts legibility); a one-shot smaller-render retry
    is reserved for genuine context-overflow recovery.
    """
    _OCR_CACHE_MAX_SIZE: int = 256
    _AVAILABILITY_TTL_S: float = 60.0  # mirrors VisionManager; prevents stale cache after Ollama restarts
    # Same negative-result cooldown as VisionManager.  Stops a 1000-page
    # scanned PDF from making 1000 failed round-trips when the configured
    # OCR model is not loaded on the provider.
    _CALL_FAILURE_COOLDOWN_S: float = 30.0

    def __init__(self, model: str = DEFAULT_OCR_MODEL, provider: str = "ollama"):
        self.model = model
        self.provider = provider
        self._is_available: bool | None = None
        self._availability_checked_at: float | None = None
        self._call_failure_at: float | None = None
        self._lock = threading.Lock()
        self._cache: dict[str, str] = {}
        self._cache_lock = threading.Lock()

    def check_availability(self) -> bool:
        """Best-effort informational probe; never gates a call or rewrites the
        configured model."""
        with self._lock:
            now = time.monotonic()
            if (
                self._is_available is not None
                and self._availability_checked_at is not None
                and (now - self._availability_checked_at) < self._AVAILABILITY_TTL_S
            ):
                return self._is_available
            try:
                provider = get_provider(self.provider)
                models, _ = provider.get_models()
                self._is_available = _model_matches(self.model, models)
            except Exception:
                self._is_available = False
            self._availability_checked_at = time.monotonic()
            return self._is_available

    def set_model(self, model: str) -> None:
        """Switch the configured model and invalidate both caches immediately.

        Clearing the availability cache + cooldown under the lock means a UI model
        change retries on the very next call instead of waiting out a stale TTL or
        a cooldown earned by the *previous* model.
        """
        with self._lock:
            self.model = model
            self._is_available = None
            self._availability_checked_at = None
            self._call_failure_at = None

    def set_provider(self, provider: str) -> None:
        """Switch the image backend (``ollama``/``lm_studio``) and reset both caches.

        An unrecognised provider is coerced to ``ollama`` — image calls only ever
        target the two local backends. Cache invalidation mirrors :meth:`set_model`.
        """
        with self._lock:
            self.provider = provider if provider in ("ollama", "lm_studio") else "ollama"
            self._is_available = None
            self._availability_checked_at = None
            self._call_failure_at = None

    def extract_page_text(self, base64_png: str) -> str:
        """OCR one page image, with caching, a failure cooldown, and a downscale retry.

        Order matters and is load-bearing:

        1. Snapshot model/provider/cooldown under ``self._lock`` (then release it).
        2. Check the result cache (under ``self._cache_lock``) BEFORE the cooldown
           gate — an already-OCR'd page must always be served, even if an unrelated
           later call put us in cooldown.
        3. Only then honour the cooldown short-circuit.
        4. Call the model; on a context-overflow error retry once with a smaller
           render; on any other failure record the cooldown and return "".

        Returns "" — never raises — so one bad page never aborts indexing.
        """
        with self._lock:
            current_provider = self.provider
            current_model = self.model
            failure_at = self._call_failure_at

        # Check the cache *before* the cooldown short-circuit so an already-OCR'd
        # page is always served — an unrelated failure must never withhold text
        # we already have.
        # OCR output can differ by provider/model, so cache keys include both.
        img_hash = hashlib.sha256(f"{current_provider}:{current_model}:{base64_png}".encode()).hexdigest()
        with self._cache_lock:
            if img_hash in self._cache:
                return self._cache[img_hash]

        if failure_at is not None and (time.monotonic() - failure_at) < self._CALL_FAILURE_COOLDOWN_S:
            return ""

        prompt = "Extract all text from this scanned document page. Return only the extracted text, preserving reading order and paragraph breaks. Ignore page numbers."
        # Bound the call so a runaway / stuck model cannot hang a long scanned
        # PDF.  No pre-emptive downscale here (unlike the description path) —
        # shrinking text hurts legibility; the existing attempt-1 downscale is
        # reserved for genuine context-overflow recovery only.  ocr_max_tokens
        # caps generation; a dense page that hits the cap is truncated (accepted
        # — 4096 tokens covers a normal page comfortably). Ranges mirror the
        # /api/config validator.
        timeout = _cfg_bounded_int("vision_timeout_s", DEFAULT_VISION_TIMEOUT_S, 5, 600)
        max_tokens = _cfg_bounded_int("ocr_max_tokens", DEFAULT_OCR_MAX_TOKENS, 64, 8192)
        result = ""
        for attempt in range(2):
            image_payload = base64_png if attempt == 0 else _downscale_base64_png(base64_png, 0.8)
            if not image_payload:
                continue
            try:
                if current_provider == "lm_studio":
                    result = _chat_lm_studio_image(
                        current_model, prompt, image_payload,
                        timeout=timeout, max_tokens=max_tokens,
                    )
                else:
                    result = _chat_ollama_image(
                        current_model, prompt, image_payload,
                        timeout=timeout, max_tokens=max_tokens,
                    )
                break
            except Exception as e:
                if attempt == 0 and _is_context_overflow_error(e):
                    logger.warning("GLM-OCR page exceeded context; retrying with a smaller render.")
                    continue
                logger.warning("GLM-OCR error on page: %s", e)
                with self._lock:
                    self._call_failure_at = time.monotonic()
                return ""

        if result:
            with self._lock:
                self._call_failure_at = None
            with self._cache_lock:
                if len(self._cache) >= self._OCR_CACHE_MAX_SIZE:
                    oldest_key = next(iter(self._cache))
                    del self._cache[oldest_key]
                self._cache[img_hash] = result
        return result

def _is_context_overflow_error(exc: Exception) -> bool:
    """True if *exc* looks like a 'page too large for the context window' error.

    Drives the OCR single-retry-with-smaller-render path. Matches the exact LM
    Studio phrasing or any backend message mentioning both "context" and "exceed".
    """
    message = str(exc).lower()
    # Two shapes: the exact LM Studio phrasing, or any backend message that
    # mentions both "context" and "exceed" (Ollama / others). Parenthesised so
    # the and/or grouping is explicit rather than leaning on operator precedence
    # (the first literal is the canonical LM Studio string, kept for clarity).
    return (
        "exceeds the available context size" in message
        or ("context" in message and "exceed" in message)
    )

def _fit_base64_image_to_max_side(base64_data: str, max_side: int) -> str:
    """Return a downscaled (PNG, 14px-aligned) re-encode of *base64_data* when
    its longest side exceeds *max_side*, else the input unchanged.

    Best-effort: on any failure — already small enough, undecodable image
    (HEIC without pillow-heif, the ``.img`` extension, or non-image test input)
    — the ORIGINAL base64 is returned so the vision call still proceeds.  The
    image-description cache (``rag.vault``) keys on the original bytes, so this
    transform is invisible to caching.
    """
    try:
        # Open once just to read the header dimensions — PIL is lazy, so
        # .size does NOT decompress the pixel data (cheap even for a 20 MB
        # image).  The actual resize re-decodes inside _downscale_base64_png;
        # the double-decode is negligible on the low-volume vision path and
        # keeps this a thin size-gate over the existing downscaler.
        raw = base64.b64decode(base64_data)
        with Image.open(io.BytesIO(raw)) as image:
            longest = max(image.size)
        # Already within budget: return the ORIGINAL bytes untouched — no
        # needless re-encode (a small JPEG stays a small JPEG).
        if longest <= max_side:
            return base64_data
        # Scale so the longest side lands at max_side. _downscale_base64_png
        # rounds UP to the nearest 14 px, so this stays <= max_side only because
        # max_side is a multiple of 14 (see VISION_IMAGE_MAX_SIDE). ``or
        # base64_data`` falls back to the original if the downscale itself fails
        # (it returns "" on error).
        return _downscale_base64_png(base64_data, max_side / float(longest)) or base64_data
    except Exception:
        # Undecodable (HEIC without pillow-heif, the ".img" extension) or
        # non-image input: send the original so the vision call still proceeds.
        return base64_data

def _downscale_base64_png(base64_png: str, scale: float) -> str:
    """Re-encode *base64_png* scaled by *scale*, dimensions aligned up to 14 px.

    Returns "" on any failure (the callers treat that as "skip this attempt").
    The 14-px alignment and 28-px floor match the patch grid VL models tile on, so
    a downscaled image still decodes into whole patches.
    """
    try:
        png_bytes = base64.b64decode(base64_png)
        with Image.open(io.BytesIO(png_bytes)) as image:
            width, height = image.size
            scaled_width = max(28, int(width * scale))
            scaled_height = max(28, int(height * scale))
            aligned_width = max(28, ((scaled_width + 13) // 14) * 14)
            aligned_height = max(28, ((scaled_height + 13) // 14) * 14)
            resized = image.resize(
                (aligned_width, aligned_height),
                Image.LANCZOS,
            )
            output = io.BytesIO()
            resized.save(output, format="PNG")
            resized.close()
            return base64.b64encode(output.getvalue()).decode("utf-8")
    except Exception:
        return ""

def _chat_ollama_image(
    model: str,
    prompt: str,
    base64_data: str,
    *,
    timeout: float | None = None,
    max_tokens: int | None = None,
) -> str:
    """Send one image+prompt to Ollama's vision chat and return the stripped text.

    Keyword-only ``timeout``/``max_tokens`` map to a timed client and ``num_predict``;
    they are the always-on call bounds. Raises on a backend/None-content failure
    (the managers catch it and treat it as an empty, cooldown-triggering result).
    """
    # Use a timed client (cached per host+timeout) instead of the module-level
    # ollama.chat so a stuck call is bounded; ``timeout=None`` leaves the SDK
    # default, handled inside _ollama_client.  num_predict caps generation.
    from core.providers.ollama import _ollama_client

    client = _ollama_client(OLLAMA_HOST, timeout)
    # options=None when max_tokens is unset/falsy ⇒ NO num_predict cap (the
    # model's own default applies). In production the managers always pass a
    # positive cap; only a direct/test call can reach the uncapped path.
    options = {"num_predict": max_tokens} if max_tokens else None
    response = client.chat(
        model=model,
        messages=[{
            "role": "user",
            "content": prompt,
            "images": [base64_data],
        }],
        options=options,
    )
    # No `or ""` guard here (unlike the LM Studio path): a model that returns
    # content=None raises AttributeError, which the callers catch and treat as a
    # failed call (empty result + cooldown) — the same outcome as "".
    return response.message.content.strip()

def _chat_lm_studio_image(
    model: str,
    prompt: str,
    base64_data: str,
    *,
    timeout: float | None = None,
    max_tokens: int | None = None,
) -> str:
    """Send one image+prompt to LM Studio's OpenAI-compatible vision endpoint.

    Builds the OpenAI ``image_url`` data-URI message shape. ``max_retries=0`` so a
    timed-out request is not silently retried into 3× the wall-clock; ``timeout`` is
    forwarded only when set (never ``timeout=None``, which the SDK reads as "no
    timeout"). ``temperature=0`` for deterministic transcription.
    """
    import openai
    from core.constants import LM_STUDIO_HOST

    # max_retries=0: the OpenAI SDK otherwise retries a timed-out request twice,
    # turning one stuck image into 3x the wall-clock wait. Never forward
    # timeout=None explicitly (the SDK can read that as "no timeout").
    client_kwargs = {"max_retries": VISION_MAX_RETRIES}
    if timeout is not None:
        client_kwargs["timeout"] = timeout
    client = openai.OpenAI(
        base_url=f"{LM_STUDIO_HOST}/v1", api_key="lm-studio", **client_kwargs
    )
    create_kwargs = {"temperature": 0.0}
    if max_tokens is not None:
        create_kwargs["max_tokens"] = max_tokens
    response = client.chat.completions.create(
        model=model,
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{base64_data}"},
                },
            ],
        }],
        **create_kwargs,
    )
    return (response.choices[0].message.content or "").strip()

# Singletons
vision_manager = VisionManager()
glm_ocr_manager = GLMOCRManager()
