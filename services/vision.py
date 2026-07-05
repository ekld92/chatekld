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

from PIL import Image

# Register the HEIF/HEIC opener so PIL can decode .heic — the standard macOS
# screenshot/photo format. Without it, a referenced .heic is undecodable and is
# forwarded raw to a local vision model that cannot read it (empty description →
# silently skipped). Import-guarded: a missing pillow-heif just leaves HEIC
# unsupported (the prior behaviour), it never blocks startup.
try:
    import pillow_heif  # type: ignore
    pillow_heif.register_heif_opener()
    _HEIF_SUPPORTED = True
except Exception:  # pragma: no cover - optional dependency
    _HEIF_SUPPORTED = False

from core.providers import get_provider
from core.constants import (
    DEFAULT_OCR_MODEL,
    DEFAULT_VISION_MODEL,
    DEFAULT_VISION_TIMEOUT_S,
    DEFAULT_VISION_MAX_TOKENS,
    DEFAULT_OCR_MAX_TOKENS,
    DEFAULT_VISION_FAILURE_COOLDOWN_S,
    VISION_IMAGE_MAX_SIDE,
)

logger = logging.getLogger(__name__)


def _failure_cooldown_s() -> float:
    """Per-call read of ``vision_failure_cooldown_s`` (improvement plan 1.5).

    The fast-fail window after a failed vision/OCR call. Unlike the other
    vision bounds this one legitimately supports **0 = disabled** (retry every
    image immediately), so it does not go through :func:`_cfg_bounded_int`
    (whose ``<= 0 ⇒ default`` contract would erase the disable). Missing /
    garbage / negative ⇒ the hard default; values above 600 clamp.

    Item 4.6: uses ``load_config_readonly`` — a no-deepcopy
    ``MappingProxyType`` read — instead of ``load_config`` because this is a
    read-only ``.get()`` on a hot path (called 3× per image × thousands of
    images per indexing run; the old ``load_config`` deep-copied the entire
    config dict every call).
    """
    try:
        from core.config import load_config_readonly
        v = int(load_config_readonly().get(
            "vision_failure_cooldown_s", DEFAULT_VISION_FAILURE_COOLDOWN_S))
    except Exception:
        return float(DEFAULT_VISION_FAILURE_COOLDOWN_S)
    if v < 0:
        return float(DEFAULT_VISION_FAILURE_COOLDOWN_S)
    return float(min(v, 600))


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

    Item 4.6: uses ``load_config_readonly`` (no-deepcopy ``MappingProxyType``)
    because this is a read-only ``.get()`` on the vision/OCR hot path.
    """
    try:
        from core.config import load_config_readonly
        v = int(load_config_readonly().get(key, default))
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
    * a **negative-result cooldown** (``_call_failure_at``; window from the
      ``vision_failure_cooldown_s`` config key via :func:`_failure_cooldown_s`,
      default 30 s, 0 disables) — after a failed call, subsequent calls
      fast-fail to "" for the cooldown window so a vault full of images cannot
      hammer a model that is not loaded. Cleared by set_model / set_provider
      so a UI change retries immediately.

    All of this state is guarded by ``self._lock``; the slow vision call runs
    *outside* the lock (see the module docstring).
    """
    _AVAILABILITY_TTL_S: float = 60.0

    def __init__(self, model: str = DEFAULT_VISION_MODEL, provider: str = "ollama"):
        self.model = model
        self.provider = provider
        self._is_available = None
        self._availability_checked_at: float | None = None
        self._call_failure_at: float | None = None
        self._lock = threading.Lock()

    def check_availability(self) -> bool:
        """Best-effort informational probe; never gates a call."""
        # Item 4.10: Fix lock discipline by performing the network call outside the lock.
        # Defect/Scenario: Calling provider.get_models() while holding self._lock blocks any other
        # thread calling describe_image() or set_model(), wedging workers if the model backend is slow or hangs.
        # Safety: Snapshots the configurations under the lock, calls get_models() outside, and re-acquires
        # the lock to cache the result only if the configuration hasn't changed in the window.
        # Invariant: self._lock is never held during external network I/O.
        with self._lock:
            now = time.monotonic()
            if (
                self._is_available is not None
                and self._availability_checked_at is not None
                and (now - self._availability_checked_at) < self._AVAILABILITY_TTL_S
            ):
                return self._is_available
            provider_name = self.provider
            model_name = self.model

        try:
            provider = get_provider(provider_name)
            models, _ = provider.get_models()
            is_available = _model_matches(model_name, models)
        except Exception:
            is_available = False

        with self._lock:
            if self.provider == provider_name and self.model == model_name:
                self._is_available = is_available
                self._availability_checked_at = time.monotonic()
                return is_available
            return self._is_available if self._is_available is not None else False

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
        if failure_at is not None and (time.monotonic() - failure_at) < _failure_cooldown_s():
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
            # Prompt Hub capture: the vision instruction is sent as a USER-role
            # message to the vision model (there is no system field on this
            # path), so it is recorded under the "vision_describe" row which the
            # Hub labels as a user-instruction, not a system prompt.
            from core import prompt_capture

            prompt_capture.record(
                "vision_describe", prompt,
                provider=current_provider, model=current_model,
            )
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
    # Same negative-result cooldown as VisionManager (window from the
    # vision_failure_cooldown_s config key via _failure_cooldown_s(); default
    # 30 s, 0 disables). Stops a 1000-page scanned PDF from making 1000 failed
    # round-trips when the configured OCR model is not loaded on the provider.

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
        # Item 4.10: Fix lock discipline by performing the network call outside the lock.
        # Defect/Scenario: Calling provider.get_models() while holding self._lock blocks any other
        # thread calling extract_page_text() or set_model(), wedging workers if the model backend is slow or hangs.
        # Safety: Snapshots the configurations under the lock, calls get_models() outside, and re-acquires
        # the lock to cache the result only if the configuration hasn't changed in the window.
        # Invariant: self._lock is never held during external network I/O.
        with self._lock:
            now = time.monotonic()
            if (
                self._is_available is not None
                and self._availability_checked_at is not None
                and (now - self._availability_checked_at) < self._AVAILABILITY_TTL_S
            ):
                return self._is_available
            provider_name = self.provider
            model_name = self.model

        try:
            provider = get_provider(provider_name)
            models, _ = provider.get_models()
            is_available = _model_matches(model_name, models)
        except Exception:
            is_available = False

        with self._lock:
            if self.provider == provider_name and self.model == model_name:
                self._is_available = is_available
                self._availability_checked_at = time.monotonic()
                return is_available
            return self._is_available if self._is_available is not None else False

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
        # Hash the components incrementally instead of building an
        # f"{provider}:{model}:{base64_png}" string first: base64_png for a
        # rendered page is multi-MB, so the f-string concatenation allocated a
        # second multi-MB string (then .encode() a third) on every call — pure
        # transient churn over a 1000-page scan. update() feeds each part directly.
        # This is the IN-MEMORY OCR result cache only (per process); the digest
        # value is never persisted, so changing how it's computed cannot affect
        # any on-disk cache — it just means a cold in-memory cache on first use.
        _h = hashlib.sha256()
        _h.update(current_provider.encode())
        _h.update(b":")
        _h.update(current_model.encode())
        _h.update(b":")
        _h.update(base64_png.encode())
        img_hash = _h.hexdigest()
        with self._cache_lock:
            if img_hash in self._cache:
                return self._cache[img_hash]

        if failure_at is not None and (time.monotonic() - failure_at) < _failure_cooldown_s():
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
        # Prompt Hub capture: the OCR instruction is a USER-role message to the
        # OCR model (no system field), recorded under "ocr_extract".
        from core import prompt_capture

        prompt_capture.record(
            "ocr_extract", prompt,
            provider=current_provider, model=current_model,
        )
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

# Formats local vision models decode directly. A decodable-but-model-unfriendly
# format (HEIC/HEIF/TIFF/BMP) is re-encoded to PNG even when under the size cap,
# so the model receives bytes it can actually read — the whole point of decoding
# HEIC via pillow-heif. Anything PIL cannot open at all (SVG, .img) still falls
# through to the original bytes (and is skipped downstream).
_MODEL_SAFE_IMAGE_FORMATS = frozenset({"PNG", "JPEG", "JPG", "WEBP", "GIF"})


def _fit_base64_image_to_max_side(base64_data: str, max_side: int) -> str:
    """Return a model-friendly PNG re-encode of *base64_data* when needed, else input.

    Re-encodes to PNG (14px-aligned) when the longest side exceeds *max_side*
    OR the source format is not one a local vision model reads directly
    (HEIC/HEIF/TIFF/BMP). A within-budget PNG/JPEG/WebP/GIF passes through
    untouched (no needless re-encode).

    Best-effort: on any failure — non-image input, or a format PIL cannot open at
    all (SVG, the ``.img`` extension) — the ORIGINAL base64 is returned so the
    vision call still proceeds. The image-description cache (``rag.vault``) keys
    on the original bytes, so this transform is invisible to caching.
    """
    try:
        # Open once just to read the header dimensions + format — PIL is lazy, so
        # .size does NOT decompress the pixel data (cheap even for a 20 MB
        # image).  The actual resize re-decodes inside _downscale_base64_png.
        raw = base64.b64decode(base64_data)
        with Image.open(io.BytesIO(raw)) as image:
            longest = max(image.size)
            fmt = (image.format or "").upper()
        # Already within budget AND a format the model reads: return the ORIGINAL
        # bytes untouched (a small JPEG stays a small JPEG).
        if longest <= max_side and fmt in _MODEL_SAFE_IMAGE_FORMATS:
            return base64_data
        # Otherwise re-encode to PNG. Scale down so the longest side lands at
        # max_side (clamped to 1.0 so an under-budget but unfriendly format — e.g.
        # a small HEIC — is converted at its own size, never upscaled).
        # _downscale_base64_png rounds UP to the nearest 14 px, so the result
        # stays <= max_side only because max_side is a multiple of 14
        # (VISION_IMAGE_MAX_SIDE). ``or base64_data`` falls back on encode failure.
        scale = min(1.0, max_side / float(longest)) if longest > 0 else 1.0
        return _downscale_base64_png(base64_data, scale) or base64_data
    except Exception:
        # Undecodable (SVG, the ".img" extension) or non-image input: send the
        # original so the vision call still proceeds (it is skipped if empty).
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
    from core.providers.base import resolve_ollama_host

    client = _ollama_client(resolve_ollama_host(), timeout)
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
    from core.providers.base import resolve_lm_studio_host
    from core.providers.lms import get_lmstudio_client

    # Reuse the SHARED, cached LM Studio client (keyed by base_url+timeout) instead
    # of constructing a fresh openai.OpenAI per image — a per-call client leaked a
    # connection pool on every page of a scanned PDF (the chat path was fixed the
    # same way). get_lmstudio_client already forces max_retries=0 (so one stuck
    # image is bounded by exactly one timeout, not 3x — matching VISION_MAX_RETRIES)
    # and normalises the timeout (non-positive ⇒ SDK default, never "0 = time out
    # immediately"; a positive value is rounded to whole seconds), so it never
    # forwards timeout=None. Passing timeout through unchanged preserves the
    # vision_timeout_s bound.
    client = get_lmstudio_client(f"{resolve_lm_studio_host()}/v1", timeout)
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
