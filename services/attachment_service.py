"""Plain Chat attachments — store a user's file and extract it to text.

Plain Chat is deliberately RAG-free: no vault retrieval, no agent loop, no
tools. Attachments must not smuggle any of that back in, so this module does
exactly one thing — turn a file the user picked into a bounded, untrusted-wrapped
block of text that rides ONE chat turn.

**Extract locally, send text** (the "Path A" decision, 2026-08-29). A PDF goes
through the same subprocess-bounded extractor the Single Paper workflow uses, a
raster image through the configured local vision model, and ``.md`` / ``.txt``
are read directly. Nothing here touches ``core/llm/types.py::Message``, whose
``content`` stays a plain ``str``: native multimodal content blocks would mean
teaching each online adapter its own wire format, which is precisely the
provider-wire-format territory that produced the 2026-07-02 P0 bugs, and the
mocked suite cannot see provider-side validation. Local providers would need
this path as a fallback regardless.

**Nothing here writes to the vault.** Uploads land in the app data directory
(the SQLite DB under ``BASE_DIR``), so ``log_vault_write`` does not apply.

**Extraction happens at UPLOAD time, not at send time.** It costs the user a
wait they can see — and in exchange they learn before sending whether the file
yielded anything, how much of it was kept, and can remove it if not. Deferring
to send time would hide a failed extraction inside a chat request and produce a
confident answer about a document the model never received.
"""

import logging
import os
import sqlite3
import tempfile
import time
import uuid

from core.agent.tools import wrap_untrusted
from core.config import load_config
from core.constants import (
    CHAT_ATTACHMENT_EXTS,
    CHAT_ATTACHMENT_IMAGE_EXTS,
    CHAT_ATTACHMENT_PDF_EXTS,
    CHAT_ATTACHMENT_TEXT_EXTS,
    CHAT_ATTACHMENT_TTL_S,
    DEFAULT_CHAT_ATTACHMENT_MAX_CHARS,
)
from core.database import DB_LOCK, get_db_connection

logger = logging.getLogger("chatekld")

#: Provenance clause for the untrusted wrapper. A chat attachment is NOT vault
#: content, and the wrapper's sentence exists to be believed by the model.
_ATTACHMENT_SOURCE = "from a file the user attached to this conversation"

#: What an EXTRACTED file that yielded nothing usable is worth saying about.
#: Below this a PDF or image extraction is reported as empty rather than
#: attached as a stub, because a two-word "description" of a failed vision call
#: is worse than an honest refusal: it would let the model answer confidently
#: about nothing. Deliberately NOT applied to ``.md`` / ``.txt``, which are read
#: directly — there a short file is short, not unreadable.
_MIN_USEFUL_CHARS = 16


class AttachmentError(ValueError):
    """A file this app cannot accept or could not read. Message is user-facing."""


def attachment_max_chars() -> int:
    """The per-file extracted-text cap, read fresh so Settings applies at once.

    Re-clamped here as well as in the ``/api/config`` validator: a hand-edited
    ``config.json`` is the case the validator never sees.
    """
    try:
        raw = int(load_config().get("chat_attachment_max_chars",
                                    DEFAULT_CHAT_ATTACHMENT_MAX_CHARS))
    except (TypeError, ValueError):
        return DEFAULT_CHAT_ATTACHMENT_MAX_CHARS
    return max(1_000, min(400_000, raw))


def classify(filename: str) -> str:
    """``pdf`` | ``image`` | ``text`` for *filename*, else raise.

    Refuses by NAME rather than accepting and silently yielding nothing: a
    ``.svg`` or ``.heic`` has no extraction path here, and "attached" followed
    by an empty context is the failure mode this is written to avoid.
    """
    ext = os.path.splitext(filename or "")[1].lower()
    if ext in CHAT_ATTACHMENT_PDF_EXTS:
        return "pdf"
    if ext in CHAT_ATTACHMENT_IMAGE_EXTS:
        return "image"
    if ext in CHAT_ATTACHMENT_TEXT_EXTS:
        return "text"
    allowed = ", ".join(sorted(CHAT_ATTACHMENT_EXTS))
    raise AttachmentError(f"Unsupported file type. Accepted: {allowed}.")


def _save_limited(file_storage, max_bytes: int, suffix: str) -> str:
    """Stream to a temp file, refusing past *max_bytes*.

    Streamed rather than read whole so an oversized upload is refused without
    ever being fully resident — the ``services/pdf_service`` discipline. The
    Content-Length header is not trusted; the count is of bytes actually read.
    """
    tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    total = 0
    try:
        with tmp:
            while True:
                chunk = file_storage.stream.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise AttachmentError(
                        f"File exceeds the {max_bytes // (1024 * 1024)} MB "
                        "attachment limit.")
                tmp.write(chunk)
        return tmp.name
    except Exception:
        try:
            os.remove(tmp.name)
        except OSError:
            pass
        raise


#: Characters of extracted text a page of a research PDF is worth, used only to
#: turn the caller's CHARACTER cap into a PAGE budget. Deliberately pessimistic:
#: overshooting costs a little extra extraction, undershooting would truncate a
#: file the user was told was read whole. Measured across 400 cached extractions
#: from the maintainer's library the median is ~1,900 chars/page; 1,200 leaves
#: comfortable headroom for a dense one.
_CHARS_PER_PAGE_ESTIMATE = 1200

#: Never extract fewer than this many pages however small the character cap is
#: — a 1,000-char cap (the validator's floor) must still read a real document
#: rather than one page of front matter.
_MIN_PDF_PAGES = 8


def _pdf_page_budget(cap: int) -> int:
    """Pages worth extracting to fill a *cap*-character attachment, plus slack.

    A PDF was extracted IN FULL — up to the 600 s subprocess timeout, OCR
    included — and then truncated to `cap` characters one layer up, so on a
    long document almost all of that work was discarded. The bound is derived
    rather than fixed because `chat_attachment_max_chars` is user-configurable:
    raising it must actually buy more of the document.

    The +50% slack means the estimate has to be wrong by half before the
    attachment is short of its cap, and even then the result is disclosed as
    truncated rather than silently short (see `store_attachment`).
    """
    return max(_MIN_PDF_PAGES,
               int(cap / _CHARS_PER_PAGE_ESTIMATE * 1.5) + 1)


def _extract_pdf(path: str) -> tuple[str, dict]:
    """Extract a bounded prefix of *path*, and SAY whether it was bounded.

    The page budget is what stops a 2,000-page PDF from being extracted in full
    only to have 95% of it discarded. But a sparse document (a scanned deck, a
    slide export) can fall UNDER the character cap while still being cut short
    by pages — in which case reporting `truncated=False` and a character total
    would tell the user the whole file was read when it was not. That is the
    one thing this module exists to never do, so the page count is read in the
    parent (a cheap PyMuPDF open, no text extraction) and the clipping is
    reported explicitly rather than inferred from the character count.
    """
    from pdf_extractor import get_pdf_page_count
    from services.pdf_service import _extract_with_timeout

    budget = _pdf_page_budget(attachment_max_chars())
    try:
        total_pages = int(get_pdf_page_count(path))
    except Exception:  # noqa: BLE001 — a page count is a disclosure, not a gate
        total_pages = 0
    text = _extract_with_timeout(path, max_pages=budget) or ""
    meta = {}
    if total_pages and total_pages > budget:
        meta = {"pages_read": budget, "pages_total": total_pages}
    return text, meta


def _extract_image_pair(path: str) -> tuple[str, dict]:
    return _extract_image(path), {}


def _extract_text_pair(path: str) -> tuple[str, dict]:
    return _extract_text(path), {}


#: How long an image attachment waits for the shared local-model gate before
#: reporting the backend busy. Short on purpose: the caller is a person who
#: just dropped a file, and "busy, try again" beats a silent minutes-long wait.
_VISION_GATE_WAIT_S = 15.0


class AttachmentBusy(Exception):
    """The local model is occupied by another user-triggered call (503).

    Distinct from :class:`AttachmentError`, which means "your file is the
    problem" and maps to 400. Nothing is wrong with the file here.
    """


def _extract_image(path: str) -> str:
    """Describe an image with the configured LOCAL vision model.

    ``describe_image`` returns "" and never raises — for the indexer that is the
    right contract (one bad image must not abort a multi-hour run), but here an
    empty result is the whole answer, so it is turned into a REFUSAL naming the
    likely cause. Silently attaching an empty image would let the model answer
    about a picture it never saw.

    PERF (2026-09-02): this call joins the shared local-model gate.
      Before:    a chat image attachment was a user-triggered LOCAL vision call
                 running OUTSIDE `services/local_model.py::LOCAL_MODEL_LOCK`,
                 whose docstring puts the worst case at 1 (user-triggered) + 1
                 (indexer). The real worst case was 1 + 1 + 1 against a single
                 local backend — the concurrent load the deck's per-section
                 retry layer exists to paper over. Recorded in CLAUDE.md's
                 Known Issues as accepted, with the fix shape named.
      Change:    the vision call runs under the gate, with a bounded wait.
      Safe because:
                 this only TIGHTENS concurrency, and since 2026-09-02 the
                 extraction runs on a daemon thread — so waiting here blocks a
                 daemon, never a Flask worker, which is the precondition the
                 gate's own docstring states. Nothing nests: this path calls
                 vision only, so a plain non-reentrant Lock stays correct.
      Preserves: an empty description is still a REFUSAL naming the local-vision
                 cause, never a silently-empty attachment.
      Lock/thread contract:
                 acquires LOCAL_MODEL_LOCK for the duration of the vision call
                 and releases it in a `finally`; held across no other lock. A
                 failed acquisition raises AttachmentBusy (503) rather than
                 waiting out the request deadline, so the user is told the
                 backend is busy instead of being told their file timed out.
      Pinned by: test_chat_attachment.py::TestImageSharesTheLocalModelGate
    """
    import base64

    # The local-model gate is a stdlib-only service; retain lazy initialization.
    from services.local_model import LOCAL_MODEL_LOCK
    from services.vision import vision_manager

    with open(path, "rb") as fh:
        data = fh.read()
    b64 = base64.b64encode(data).decode("ascii")
    if not LOCAL_MODEL_LOCK.acquire(timeout=_VISION_GATE_WAIT_S):
        raise AttachmentBusy(
            "The local vision model is busy with another request. "
            "Wait for it to finish and attach the image again.")
    try:
        text = (vision_manager.describe_image(b64) or "").strip()
    finally:
        LOCAL_MODEL_LOCK.release()
    if len(text) < _MIN_USEFUL_CHARS:
        raise AttachmentError(
            "Could not read that image. Images are described by a LOCAL vision "
            "model (Ollama or LM Studio), so this needs one running with the "
            "configured vision model installed — an online chat provider does "
            "not supply it. Check Settings → OCR / Vision.")
    return text


def _extract_text(path: str) -> str:
    """Read a text/Markdown file, decoding leniently.

    ``errors='replace'`` rather than a refusal: a note with one stray byte is
    still worth discussing, and the substitution character is visible in the
    preview the user approves.
    """
    with open(path, "rb") as fh:
        return fh.read().decode("utf-8", "replace")


#: Each returns ``(text, meta)``; `meta` carries anything about HOW the text
#: was obtained that the user must be told (currently only a page-bounded
#: PDF read).
_EXTRACTORS = {"pdf": _extract_pdf, "image": _extract_image_pair,
               "text": _extract_text_pair}


def _prune_expired(conn) -> None:
    """Drop attachments older than the TTL.

    The Plain Chat transcript is ephemeral (lost on reload), so an attachment
    whose conversation was abandoned has no owner left to delete it. Swept on
    upload rather than on a timer: no scheduler, and the only moment the table
    is guaranteed to be growing.
    """
    conn.execute("DELETE FROM chat_attachments WHERE created_at < ?",
                 (time.time() - CHAT_ATTACHMENT_TTL_S,))


def store_attachment(file_storage, filename: str, max_bytes: int,
                     runner=None) -> dict:
    """Extract *file_storage* to text and store it. Returns the row's summary.

    Raises :class:`AttachmentError` with a user-facing message for anything the
    user can fix (wrong type, too big, unreadable); the caller maps that to 400.

    *runner*, when given, is ``runner(kind, fn) -> fn()`` — a seam the route
    uses to run the EXTRACTION (and only the extraction) on a bounded daemon
    thread. It may raise its own exception to signal a timeout or a busy slot,
    which propagates to the caller unchanged.

    PERF (2026-09-02): the split is where it is on purpose.
      Before:    the whole call ran inline on the Waitress worker — a PDF
                 joined the extraction subprocess for up to
                 PDF_EXTRACT_TIMEOUT_S (600 s) and an image made a vision call,
                 each holding one of thirty-two pool slots with no ceiling of
                 its own and no way to answer "busy".
      Change:    the streamed save stays on the request thread; the extractor
                 call goes through *runner*.
      Safe because:
                 the save must read the request stream while the request is
                 alive (and is already bounded by *max_bytes*), whereas the
                 extractor only ever touches the temp path it is handed — so
                 the seam sits exactly at the boundary between "needs the
                 request" and "does not". The `finally` that removes the temp
                 file still encloses the runner, so an abandoned daemon cannot
                 strand it.
      Preserves: extraction still happens at UPLOAD time, so the user still
                 learns before spending a turn whether the file yielded text
                 and how much of it was kept.
      Pinned by: test_chat_attachment.py::TestExtractionIsBounded
    """
    kind = classify(filename)
    suffix = os.path.splitext(filename)[1].lower()
    path = _save_limited(file_storage, max_bytes, suffix)
    if runner is None:
        def runner(_kind, fn):
            return fn()
    try:
        try:
            text, extract_meta = runner(kind, lambda: _EXTRACTORS[kind](path))
        except (AttachmentError, AttachmentBusy):
            raise
        except Exception as exc:
            if getattr(exc, "_attachment_runner_signal", False):
                raise
            logger.warning("Attachment extraction failed for %s: %s", kind, exc)
            raise AttachmentError("Could not read that file.") from exc
    finally:
        try:
            os.remove(path)
        except OSError:
            pass

    text = (text or "").strip()
    # The floor applies to EXTRACTED kinds only. For a PDF or an image a
    # two-word result means the extractor failed, and attaching it would let the
    # model answer confidently about nothing. A text file is different: it was
    # read without difficulty and a short one is simply short, so refusing
    # "Buy milk." while blaming a missing OCR layer names a cause that cannot
    # apply (2026-08-29 review). Only genuinely empty text is refused there.
    if not text:
        raise AttachmentError("That file is empty.")
    if kind != "text" and len(text) < _MIN_USEFUL_CHARS:
        raise AttachmentError(
            "No readable text found in that file. A scanned PDF with no OCR "
            "layer is the usual cause.")

    cap = attachment_max_chars()
    full_chars = len(text)
    # A page-bounded read is truncation even when the text fits the cap: the
    # file continues past what was read. Folding it in here means the chip, the
    # untrusted wrapper's disclosure and the `truncated` flag all agree, and
    # none of them can claim a partial document was read whole.
    pages_read = extract_meta.get("pages_read")
    pages_total = extract_meta.get("pages_total")
    truncated = full_chars > cap or bool(pages_read)
    if full_chars > cap:
        text = text[:cap]

    attachment_id = str(uuid.uuid4())
    with DB_LOCK:
        with get_db_connection() as conn:
            _prune_expired(conn)
            conn.execute(
                "INSERT INTO chat_attachments (attachment_id, filename, kind, "
                "extracted_text, chars, truncated, created_at) VALUES (?,?,?,?,?,?,?)",
                (attachment_id, filename, kind, text, full_chars,
                 1 if truncated else 0, time.time()),
            )
            conn.commit()
    return {
        "attachment_id": attachment_id,
        "filename": filename,
        "kind": kind,
        # `chars` is the file's FULL extracted length and `attached_chars` what
        # the model will actually see. Reporting only one of them is how a user
        # ends up believing a 400,000-character report was read whole.
        "chars": full_chars,
        "attached_chars": len(text),
        "truncated": truncated,
        # Present only when the PAGE budget was the binding constraint, so a
        # client can say "the first 151 of 2,000 pages" instead of quoting a
        # character total that describes only what was extracted.
        **({"pages_read": pages_read, "pages_total": pages_total}
           if pages_read else {}),
    }


def delete_attachment(attachment_id: str) -> bool:
    with DB_LOCK:
        with get_db_connection() as conn:
            cur = conn.execute("DELETE FROM chat_attachments WHERE attachment_id = ?",
                               (attachment_id,))
            conn.commit()
            return cur.rowcount > 0


def fetch_attachments(ids: list[str]) -> list[sqlite3.Row]:
    """Rows for *ids*, in the caller's order; unknown ids are simply absent.

    Ordered by the request rather than by the database so the block the model
    reads matches the order the chips appear in, and so a caller can detect a
    missing id by comparing lengths (an expired attachment must be reported,
    not silently dropped from the prompt).
    """
    if not ids:
        return []
    with DB_LOCK:
        with get_db_connection() as conn:
            # get_db_connection yields TUPLE rows by default (the app's other
            # consumers index positionally). Set the factory on this connection
            # only — it is opened and closed per call, so this changes nothing
            # for any other call site, and name-indexing here is what keeps the
            # column order out of build_attachment_block.
            conn.row_factory = sqlite3.Row
            marks = ",".join("?" * len(ids))
            rows = conn.execute(
                "SELECT attachment_id, filename, kind, extracted_text, chars, "
                f"truncated FROM chat_attachments WHERE attachment_id IN ({marks})",
                list(ids),
            ).fetchall()
    by_id = {r["attachment_id"]: r for r in rows}
    return [by_id[i] for i in ids if i in by_id]


def fetch_attachments_map(ids: list[str]) -> dict:
    """``{attachment_id: row}`` for *ids* — ONE query for a whole request.

    ``_inflate_attachments`` used to call :func:`fetch_attachments` once per
    message carrying attachments, each opening its own connection under the
    global ``DB_LOCK``: up to 20 acquisitions for a single chat turn, all of
    them contending with whatever else holds it. The ids are known up front, so
    one query answers them all and the caller slices per message.

    Returns a mapping rather than a list because the ORDER that matters is
    per-message (the block the model reads must match the chip order), and only
    the caller knows it.
    """
    unique = list(dict.fromkeys(i for i in ids if i))
    if not unique:
        return {}
    with DB_LOCK:
        with get_db_connection() as conn:
            # See fetch_attachments: get_db_connection yields TUPLE rows, and
            # this connection is opened and closed per call, so setting the
            # factory here changes nothing for any other call site.
            conn.row_factory = sqlite3.Row
            marks = ",".join("?" * len(unique))
            rows = conn.execute(
                "SELECT attachment_id, filename, kind, extracted_text, chars, "
                f"truncated FROM chat_attachments WHERE attachment_id IN ({marks})",
                unique,
            ).fetchall()
    return {r["attachment_id"]: r for r in rows}


def build_attachment_block(rows) -> str:
    """The untrusted-wrapped text for *rows*, prepended to a user turn.

    Each file gets its OWN wrapper — one per-call nonce per file — so a file
    whose text contains something resembling a closing tag cannot make the next
    file's content look like trusted, outside-the-wrapper material.

    Truncation is disclosed INSIDE the wrapper, not only in the UI: a model told
    it holds a whole report will answer as though it does, and "the document
    does not mention X" is exactly the confident wrong answer a silent cut
    produces.
    """
    blocks = []
    for row in rows:
        header = f"{row['filename']} ({row['kind']})"
        body = row["extracted_text"] or ""
        if row["truncated"]:
            # Two different cuts, and the model must not be told the wrong one.
            #
            # A CHARACTER cut always leaves `len(body) < chars`, so the stored
            # total is the file's real extracted length and quoting it is both
            # honest and useful. A PAGE-bounded PDF read is truncation too — the
            # document continues past what was extracted — but there `chars`
            # describes only what was READ, so quoting it as the total would say
            # "the first 45,000 of 45,000 characters", which reads as "all of
            # it". That case is derivable from the row without a schema change:
            # it is the only way `truncated` can be set while the body is the
            # whole of what was stored.
            if len(body) < (row["chars"] or 0):
                body += (f"\n\n[TRUNCATED: this is the first {len(body)} "
                         f"characters of {row['chars']}. Later parts of the "
                         f"file were not included.]")
            else:
                body += ("\n\n[TRUNCATED: only the first pages of this file "
                         "were read. Later parts were not included, so do not "
                         "conclude that something is absent from the document "
                         "merely because it is absent here.]")
        blocks.append(wrap_untrusted(
            "attachment", body,
            truncated=bool(row["truncated"]),
            source=f"{_ATTACHMENT_SOURCE}: {header}",
        ))
    return "\n\n".join(blocks)
