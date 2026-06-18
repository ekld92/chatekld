import hashlib
import base64
import io
import logging
import threading
import time
import ollama

from PIL import Image

from core.providers import get_provider
from core.constants import DEFAULT_OCR_MODEL, DEFAULT_VISION_MODEL

logger = logging.getLogger(__name__)

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
    """Handles image description via vision models."""
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
        with self._lock:
            current_model = self.model
            current_provider = self.provider
            failure_at = self._call_failure_at
        if failure_at is not None and (time.monotonic() - failure_at) < self._CALL_FAILURE_COOLDOWN_S:
            return ""

        try:
            prompt = 'Extract all text from this scanned document page. Return only the extracted text, preserving reading order and paragraph breaks. Ignore page numbers.'
            if current_provider == "lm_studio":
                result = _chat_lm_studio_image(current_model, prompt, base64_data)
            else:
                result = _chat_ollama_image(current_model, prompt, base64_data)
        except Exception as e:
            logger.warning("Vision error: %s", e)
            with self._lock:
                self._call_failure_at = time.monotonic()
            return ""
        with self._lock:
            self._call_failure_at = None
        return result

    def set_model(self, model: str) -> None:
        with self._lock:
            self.model = model
            self._is_available = None
            self._availability_checked_at = None
            self._call_failure_at = None

    def set_provider(self, provider: str) -> None:
        with self._lock:
            self.provider = provider if provider in ("ollama", "lm_studio") else "ollama"
            self._is_available = None
            self._availability_checked_at = None
            self._call_failure_at = None

class GLMOCRManager:
    """Handles full-page OCR for scanned PDFs."""
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
        with self._lock:
            self.model = model
            self._is_available = None
            self._availability_checked_at = None
            self._call_failure_at = None

    def set_provider(self, provider: str) -> None:
        with self._lock:
            self.provider = provider if provider in ("ollama", "lm_studio") else "ollama"
            self._is_available = None
            self._availability_checked_at = None
            self._call_failure_at = None

    def extract_page_text(self, base64_png: str) -> str:
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
        result = ""
        for attempt in range(2):
            image_payload = base64_png if attempt == 0 else _downscale_base64_png(base64_png, 0.8)
            if not image_payload:
                continue
            try:
                if current_provider == "lm_studio":
                    result = _chat_lm_studio_image(current_model, prompt, image_payload)
                else:
                    result = _chat_ollama_image(current_model, prompt, image_payload)
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
    message = str(exc).lower()
    return "exceeds the available context size" in message or "context" in message and "exceed" in message

def _downscale_base64_png(base64_png: str, scale: float) -> str:
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

def _chat_ollama_image(model: str, prompt: str, base64_data: str) -> str:
    response = ollama.chat(
        model=model,
        messages=[{
            "role": "user",
            "content": prompt,
            "images": [base64_data],
        }],
    )
    return response.message.content.strip()

def _chat_lm_studio_image(model: str, prompt: str, base64_data: str) -> str:
    import openai
    from core.constants import LM_STUDIO_HOST

    client = openai.OpenAI(base_url=f"{LM_STUDIO_HOST}/v1", api_key="lm-studio")
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
        temperature=0.0,
    )
    return (response.choices[0].message.content or "").strip()

# Singletons
vision_manager = VisionManager()
glm_ocr_manager = GLMOCRManager()
