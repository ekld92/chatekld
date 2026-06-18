import json
import os
import uuid
import tempfile
import logging
import multiprocessing as mp
from core.database import get_db_connection, DB_LOCK
from core.constants import PDF_EXTRACT_TIMEOUT_S, PDF_LIMIT_SIZE_MB

logger = logging.getLogger(__name__)

def _save_upload_limited(file_storage, max_bytes: int) -> str:
    """Stream an uploaded file to disk without exceeding the byte budget."""
    tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
    total = 0
    try:
        with tmp:
            while True:
                chunk = file_storage.stream.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise ValueError("PDF exceeds configured size limit")
                tmp.write(chunk)
        return tmp.name
    except Exception:
        try:
            os.remove(tmp.name)
        except OSError:
            pass
        raise

def _extract_all_pages(pdf_path: str, ocr_cb) -> str:
    """Extract the full text of a PDF, looping page ranges when necessary.

    ``extract_structured_from_pdf`` refuses ranges above
    ``EXTRACT_MAX_PAGES_PER_CALL`` (1000) pages per call, so uploads of
    bigger PDFs used to hard-fail with the extractor's internal "Page range
    too large" error.  This mirrors the vault indexer's range loop: extract
    in 1000-page ranges and concatenate.  Concatenation is correct here —
    the text goes to SQLite for summarisation, not to an embedder, so
    nothing downstream depends on per-range identity.

    Memory stays bounded by the largest *range*, not the file, inside each
    extraction call; the concatenated result is what the route would have
    stored anyway.  ``PDF_MAX_PAGES`` is the shared sanity ceiling (the
    subprocess's PDF_EXTRACT_TIMEOUT_S wall clock still applies on top).
    """
    from pdf_extractor import (
        extract_structured_from_pdf,
        get_pdf_page_count,
        EXTRACT_MAX_PAGES_PER_CALL,
    )
    from core.constants import PDF_MAX_PAGES

    page_count = get_pdf_page_count(pdf_path)
    if page_count > PDF_MAX_PAGES:
        raise ValueError(
            f"PDF has {page_count} pages; the maximum supported is "
            f"{PDF_MAX_PAGES}."
        )

    def _text_of(sections) -> str:
        text = sections.full_text
        if not text and sections.sections:
            # Layout parse produced sections but no flat text — fall back to
            # a markdown-ish join so the summariser still gets something.
            text = "\n\n".join(
                f"## {name}\n{body}" for name, body in sections.sections.items()
            )
        return text or ""

    if page_count <= EXTRACT_MAX_PAGES_PER_CALL:
        # Single-call fast path — identical to the pre-range-loop behaviour.
        return _text_of(extract_structured_from_pdf(pdf_path, ocr_cb=ocr_cb))

    parts: list[str] = []
    for start in range(0, page_count, EXTRACT_MAX_PAGES_PER_CALL):
        end = min(start + EXTRACT_MAX_PAGES_PER_CALL, page_count)
        text = _text_of(
            extract_structured_from_pdf(
                pdf_path, start_page=start, end_page=end, ocr_cb=ocr_cb
            )
        )
        if text:
            parts.append(text)
    # Same separator the vault loader uses between ranges.
    return "\n\n".join(parts)

def _extract_worker(pdf_path: str, result_path: str) -> None:
    try:
        from services.vision import glm_ocr_manager
        from core.config import load_config

        # The spawn child starts with a fresh services.vision module whose
        # glm_ocr_manager is built from DEFAULT_OCR_MODEL.  Re-apply the user's
        # configured OCR provider/model so the subprocess OCR honours the same
        # selection the UI shows.
        cfg = load_config()
        ocr_provider = cfg.get("ocr_provider")
        if ocr_provider:
            glm_ocr_manager.set_provider(str(ocr_provider))
        ocr_model = cfg.get("ocr_model")
        if ocr_model:
            glm_ocr_manager.set_model(str(ocr_model))

        extracted_text = _extract_all_pages(
            pdf_path, ocr_cb=glm_ocr_manager.extract_page_text
        )
        payload = {"status": "ok", "text": extracted_text}
    except Exception as exc:
        payload = {"status": "error", "error": str(exc)}
    # Atomic hand-off to the parent: the parent escalates a timed-out child
    # from SIGTERM to SIGKILL, either of which can land mid-write.  Writing
    # to a sibling temp file and promoting it with os.replace() means the
    # parent only ever sees the pristine empty file from its mkstemp (mapped
    # to a clean worker-failure error) or a complete JSON payload — never a
    # truncated one.  The deterministic ".tmp" suffix cannot collide because
    # result_path itself is unique per extraction (parent mkstemp).
    tmp_path = result_path + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        os.replace(tmp_path, result_path)
    except BaseException:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise

def _read_extract_result(result_path: str, exitcode) -> str:
    """Parse the worker's result file, mapping every failure mode to a clean error.

    Factored out of _extract_with_timeout so the truncated/garbage-result
    paths are unit-testable without spawning a real subprocess.  The child
    writes the result atomically (temp + os.replace), so an empty file means
    "worker died before producing a result" and unparseable JSON should be
    impossible — but a malicious/corrupt tmpfs entry must still surface as a
    RuntimeError for the route's error handling, never a raw JSONDecodeError.
    """
    if exitcode and (not os.path.exists(result_path) or os.path.getsize(result_path) == 0):
        raise RuntimeError("PDF extraction worker failed")
    try:
        with open(result_path, encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, ValueError) as exc:
        raise RuntimeError("PDF extraction worker produced an unreadable result") from exc
    if payload.get("status") != "ok":
        raise RuntimeError(str(payload.get("error") or "PDF extraction failed"))
    return str(payload.get("text") or "")

def _extract_with_timeout(pdf_path: str) -> str:
    """Run PDF extraction in a subprocess so timeouts actually stop work.

    Uses an explicit ``spawn`` context (not ``fork``) so the child does not
    inherit the parent's threads / open file descriptors / loaded Python
    objects — crucial because pdf_extractor pulls in PyMuPDF, MarkItDown,
    and PIL which are not always fork-safe.

    The child is NOT a daemon: daemonic processes cannot spawn their own
    children under multiprocessing, which would prevent PyMuPDF or
    MarkItDown from using any internal worker pools.  Explicit terminate
    on timeout still bounds runaway extraction.
    """
    fd, result_path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    try:
        ctx = mp.get_context("spawn")
        proc = ctx.Process(target=_extract_worker, args=(pdf_path, result_path))
        proc.start()
        proc.join(PDF_EXTRACT_TIMEOUT_S)
        if proc.is_alive():
            proc.terminate()
            proc.join(5)
            if proc.is_alive():
                # terminate() sent SIGTERM; if still alive, escalate to SIGKILL.
                proc.kill()
                proc.join(2)
            raise TimeoutError(f"PDF extraction timed out after {PDF_EXTRACT_TIMEOUT_S} seconds")
        return _read_extract_result(result_path, proc.exitcode)
    finally:
        # Also reap the child's atomic-write temp file: a kill that lands
        # between the child's write and its os.replace orphans it.
        for leftover in (result_path, result_path + ".tmp"):
            try:
                os.remove(leftover)
            except OSError:
                pass

def process_pdf_upload(file_storage, filename: str, max_bytes: int | None = None) -> tuple[str, str]:
    """Process an uploaded PDF: extract text and store in DB."""
    upload_id = str(uuid.uuid4())
    max_bytes = max_bytes or PDF_LIMIT_SIZE_MB * 1024 * 1024
    tmp_path = _save_upload_limited(file_storage, max_bytes)
        
    try:
        extracted_text = _extract_with_timeout(tmp_path)
        
        # Store in DB
        with DB_LOCK:
            with get_db_connection() as conn:
                conn.execute(
                    "INSERT INTO uploads (upload_id, filename, extracted_text) VALUES (?, ?, ?)",
                    (upload_id, filename, extracted_text)
                )
                conn.commit()
        
        return upload_id, extracted_text
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
