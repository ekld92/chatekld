"""Plain Chat attachments — upload a file for one chat turn, or drop it.

A SIBLING of ``POST /api/upload`` rather than a widening of it. ``/api/upload``
feeds ``/api/summarise``, which assumes its stored text is "one PDF's full
text"; accepting images and ``.txt`` there would let a vision description or a
40-line note flow into the paper summariser through a path nobody designed, and
would put Plain Chat's cleanup in the same id space as the paper flow's.

Two routes, both under the app-level origin guard:

* ``POST /api/chat-attachment`` — multipart ``file``; extracts it to text and
  returns ``{attachment_id, filename, kind, chars, attached_chars, truncated}``.
* ``DELETE /api/chat-attachment/<id>`` — drop one (the chip's × in the composer).

Nothing here writes to the vault, so ``log_vault_write`` does not apply; the
extracted text lives in the app's own SQLite under ``BASE_DIR``.
"""

import logging

from flask import Blueprint, jsonify, request
from werkzeug.utils import secure_filename

from api.bounded import run_bounded_action
from api.security import sanitise_error_msg
from api.validators import coerce_int_in_range
from core.config import load_config_readonly
from core.constants import (
    CHAT_ATTACHMENT_EXTS,
    CHAT_ATTACHMENT_LIMIT_SIZE_MB,
    DEFAULT_VISION_TIMEOUT_S,
    PDF_EXTRACT_TIMEOUT_S,
    SSE_STALL_MARGIN_S,
)
from services.attachment_service import (
    AttachmentBusy,
    AttachmentError,
    delete_attachment,
    store_attachment,
)

logger = logging.getLogger("chatekld")

chat_attachment_bp = Blueprint("chat_attachment", __name__)

#: Wall-clock ceiling per extraction KIND, in seconds. Each sits just OUTSIDE
#: the bound the extractor already has, so this never kills a legal extraction
#: and throws away work the user is waiting for — the timeout chain's
#: outer-must-exceed-inner rule:
#:   pdf   — the subprocess join is PDF_EXTRACT_TIMEOUT_S (600); + margin.
#:   image — one vision call, bounded by `vision_timeout_s` (5-600, default
#:           120) with VISION_MAX_RETRIES = 0; resolved per request below.
#:   text  — a local file read of at most CHAT_ATTACHMENT_LIMIT_SIZE_MB.
#: A dict, not constants, so a test can narrow one without touching the others.
_EXTRACT_DEADLINES_S = {
    "pdf": float(PDF_EXTRACT_TIMEOUT_S + SSE_STALL_MARGIN_S),
    "image": 0.0,     # resolved per request from `vision_timeout_s`
    "text": 60.0,
}


class _ExtractTimedOut(Exception):
    """The extraction outran its deadline; the request thread is freed (504)."""
    _attachment_runner_signal = True


class _ExtractBusy(Exception):
    """Another extraction of this kind is running (429)."""
    _attachment_runner_signal = True


def _image_deadline_s() -> float:
    """`vision_timeout_s` plus a margin, read per request like the transport does."""
    cfg = load_config_readonly()
    t = coerce_int_in_range(cfg.get("vision_timeout_s"), 5, 600)
    return float((t or DEFAULT_VISION_TIMEOUT_S) + SSE_STALL_MARGIN_S)


def _bounded_extract(kind: str, fn):
    """Run the extractor on a bounded daemon, one live per kind.

    PERF (2026-09-02): extraction leaves the Waitress request thread.
      Before:    `store_attachment` ran inline — a PDF joined its extraction
                 subprocess for up to PDF_EXTRACT_TIMEOUT_S (600 s) and an
                 image made a vision call, each pinning one of thirty-two pool
                 slots with no ceiling of its own and no way to answer "busy".
      Change:    the extractor runs through api.bounded.run_bounded_action; the
                 streamed save stays on the request thread, which is where it
                 has to be (it reads the request stream).
      Safe because:
                 nothing the extractor touches needs the Flask request — it is
                 handed a temp path — and `store_attachment`'s `finally` still
                 removes that file, so an abandoned daemon strands nothing. A
                 daemon that finishes after a 504 can still write its row; the
                 24 h TTL sweep collects it, and the user was told the upload
                 failed, which it did as far as they are concerned.
      Preserves: extraction still happens at UPLOAD time, so the user learns
                 before spending a turn whether the file yielded text.
      Honest limit: neither the extraction subprocess nor a vision call is
                 interruptible, so `should_cancel` cannot stop one already in
                 flight — the same residual recorded for the tagging propose
                 gate. What the deadline buys is the WORKER back, and what the
                 single-flight buys is a 429 instead of a second extraction.
      Lock/thread contract:
                 the request thread holds no lock while joining; the daemon
                 takes whatever the extractor takes (the image arm's
                 LOCAL_MODEL_LOCK) entirely off the request thread.
      Pinned by: test_chat_attachment.py::TestExtractionIsBounded
    """
    deadline = _EXTRACT_DEADLINES_S.get(kind) or 0.0
    if kind == "image" and not deadline:
        deadline = _image_deadline_s()
    res, timed_out, busy = run_bounded_action(
        f"chat_attachment_{kind}", lambda should_cancel: fn(),
        deadline_s=deadline)
    if busy:
        raise _ExtractBusy(
            "Another attachment of this type is still being read. "
            "Wait for it to finish and try again.")
    if timed_out:
        raise _ExtractTimedOut(
            f"Reading that file timed out after {int(deadline)} seconds.")
    return res


@chat_attachment_bp.route("/api/chat-attachment", methods=["POST"])
def api_chat_attachment():
    """Store one attachment and return what the model will actually see."""
    if "file" not in request.files:
        return jsonify({"error": "No file part"}), 400
    file = request.files["file"]
    if not file.filename:
        return jsonify({"error": "No selected file"}), 400

    # secure_filename strips directory components and exotic characters. It can
    # return "" for a name that is entirely stripped (e.g. "..", or a purely
    # non-ASCII name), and "" has no extension — which would reach classify()
    # as an unhelpful "unsupported type" rather than the real problem.
    filename = secure_filename(file.filename)
    if not filename:
        return jsonify({"error": "That filename cannot be used. Rename the file "
                                 "using letters, digits, dots or dashes."}), 400

    max_bytes = CHAT_ATTACHMENT_LIMIT_SIZE_MB * 1024 * 1024
    try:
        return jsonify(store_attachment(file, filename, max_bytes,
                                        runner=_bounded_extract))
    except AttachmentBusy as exc:
        # The local model is occupied, not the route — a distinct condition
        # from _ExtractBusy (another upload of this kind) and from a 400
        # (the file is the problem).
        return jsonify({"error": str(exc)}), 503
    except _ExtractBusy as exc:
        return jsonify({"error": str(exc)}), 429
    except _ExtractTimedOut as exc:
        return jsonify({"error": str(exc)}), 504
    except AttachmentError as exc:
        # AttachmentError messages are written to be shown to the user (wrong
        # type, too big, nothing readable) and carry no internal detail.
        return jsonify({"error": str(exc),
                        "accepted": sorted(CHAT_ATTACHMENT_EXTS)}), 400
    except Exception as exc:
        logger.exception("chat attachment upload failed")
        return jsonify({"error": sanitise_error_msg(exc)}), 500


@chat_attachment_bp.route("/api/chat-attachment/<attachment_id>", methods=["DELETE"])
def api_chat_attachment_delete(attachment_id: str):
    """Drop one attachment. Idempotent: an unknown id is a 404, not an error."""
    try:
        if delete_attachment(attachment_id):
            return jsonify({"ok": True})
        return jsonify({"error": "No such attachment"}), 404
    except Exception as exc:
        logger.exception("chat attachment delete failed")
        return jsonify({"error": sanitise_error_msg(exc)}), 500
