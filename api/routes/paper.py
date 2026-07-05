"""Single Paper + uploads + feedback endpoints.

This blueprint backs the Single Paper workflow and its supporting routes:

- ``POST /api/upload`` / ``DELETE /api/upload/<id>`` — accept a PDF (size- and
  extension-checked), extract its text in a subprocess, and store it keyed by an
  upload id; or delete a stored upload.
- ``POST /api/summarise`` — stream a tunable summary as SSE. Generation knobs
  resolve **body → persisted ``paper_*`` config → hard-coded clamp default**
  (see :func:`_extract_summarise_params`); the document-type, audience, focus
  question, and (capped) custom system prompt shape the prompt. The uploaded
  PDF text is treated as untrusted source material by the summariser.
- ``POST /api/export-summary`` — atomically write a ``.txt``/``.md`` summary to
  the user's Downloads folder.
- ``POST /api/feedback`` / ``GET /api/feedback/history`` — store/read feedback,
  recursively capping every string field to bound disk writes.

All routes are gated by the app-level before_request origin guard
(api.security.register_origin_guard). SSE routes use
the shared ``{info}`` / ``{token}`` / ``{error}`` + ``[DONE]`` contract.
"""
import logging
import os
import re
import unicodedata
from flask import Blueprint, request, jsonify
from werkzeug.utils import secure_filename

from core.constants import (
    PDF_LIMIT_SIZE_MB,
    PROMPT_PRESETS,
    SYSTEM_PROMPT_LIMIT,
    TARGET_AUDIENCE_OPTIONS,
    SSE_SINGLE_SHOT_FLOOR_S,
    SSE_STALL_MARGIN_S,
)
from core.database import get_db_connection, DB_LOCK
from core.config import load_config, load_report_types
from core.utils import (
    cap, parse_temperature, parse_num_ctx, parse_num_predict,
    parse_top_p, parse_repeat_penalty, write_text_atomic
)
from api.security import sanitise_error_msg
from api.validators import (
    coerce_enum,
    coerce_int_in_range,
    coerce_non_empty_string,
    coerce_regex,
)
from api.sse import run_sse_worker
from rag.summarizer import summarise_stream

paper_bp = Blueprint('paper', __name__)
logger = logging.getLogger(__name__)

# Windows-reserved filename characters, replaced with "_" in export names.
_RESERVED_FILENAME_CHARS_RE = re.compile(r'[<>:"|?*]')


def _safe_export_basename(raw: str, max_len: int = 128) -> str:
    """Filesystem-safe export basename that PRESERVES accented / CJK letters.

    werkzeug's ``secure_filename`` strips every non-ASCII byte, so a French
    ("résumé") or CJK title collapses to ASCII or the generic "summary" — and
    because the export is written with ``os.replace`` a second collapsing title
    silently overwrites the first. This keeps Unicode letters and only removes
    what is actually unsafe in a path component: separators, NUL, control chars,
    and the Windows-reserved set. NFC-normalised, length-capped, with a
    ``"summary"`` fallback so the basename is never empty.
    """
    s = unicodedata.normalize("NFC", str(raw))
    s = s.replace("/", "_").replace("\\", "_").replace("\x00", "")
    s = "".join(ch for ch in s if ord(ch) >= 32)        # drop control chars
    s = _RESERVED_FILENAME_CHARS_RE.sub("_", s)          # Windows-reserved
    s = re.sub(r"\s+", " ", s).strip().strip(".")        # collapse ws, no edge dots
    s = s[:max_len].strip()
    return s or "summary"


def _extract_summarise_params(data: dict) -> dict:
    """Resolve generation params: request body wins, else the persisted
    ``paper_*`` config defaults (set in the Settings window), else the
    hard-coded clamp default.  The nested ``parse_*`` re-clamps the config
    value too, so a hand-edited config.json cannot inject an out-of-range
    default."""
    cfg = load_config()
    return {
        "temperature": parse_temperature(
            data.get("temperature"),
            default=parse_temperature(cfg.get("paper_temperature"), 0.3),
        ),
        "max_tokens": parse_num_predict(
            data.get("max_tokens"),
            default=parse_num_predict(cfg.get("paper_max_tokens"), 4096),
        ),
        "num_ctx": parse_num_ctx(
            data.get("num_ctx"),
            default=parse_num_ctx(cfg.get("paper_num_ctx"), 32768),
        ),
        "top_p": parse_top_p(
            data.get("top_p"),
            default=parse_top_p(cfg.get("paper_top_p"), 0.9),
        ),
        "repeat_penalty": parse_repeat_penalty(
            data.get("repeat_penalty"),
            default=parse_repeat_penalty(cfg.get("paper_repeat_penalty"), 1.1),
        ),
    }

def _normalise_prompt_preset(preset) -> dict:
    """Coerce a preset entry into a ``{system, user_template}`` dict.

    ``PROMPT_PRESETS`` entries are normally structured dicts, but tolerate a
    bare string (used for both slots) and fall back to a minimal "Summarize."
    preset for an unknown/None lookup, so the summariser always has both slots.
    """
    if isinstance(preset, dict):
        return preset
    if isinstance(preset, str):
        return {"system": preset, "user_template": preset}
    return {"system": "Summarize.", "user_template": "Summarize."}

def _resolve_system_prompt(data: dict, preset: dict) -> tuple[str, str | None, str]:
    """Return (system_prompt, error_message, doc_type_name).

    doc_type_name is the human-readable report type name (e.g. "Clinical Trial
    (RCT)") used to fill the {document_type_line} slot in the prompt template.
    Falls back to "Research Paper" when no report type is selected.
    """
    _raw_prompt = data.get("system_prompt")
    custom = _raw_prompt.strip() if isinstance(_raw_prompt, str) else ""

    if len(custom) > SYSTEM_PROMPT_LIMIT:
        return (
            "",
            f"System prompt exceeds {SYSTEM_PROMPT_LIMIT} character limit",
            "Research Paper",
        )

    doc_type = "Research Paper"
    report_type_id = data.get("report_type_id")
    if report_type_id:
        types = load_report_types()
        rt = next((t for t in types if t["id"] == report_type_id), None)
        if rt:
            doc_type = rt.get("name", "Research Paper")
            if not custom:
                custom = rt.get("system_prompt", preset.get("system", ""))
    if not custom:
        custom = preset.get("system", "")

    return custom, None, doc_type

@paper_bp.route("/api/summarise", methods=["POST"])
def api_summarise():
    """Stream a tunable single-paper summary as SSE.

    Looks up the stored extracted text by ``upload_id`` (400 if missing/empty),
    resolves the prompt (preset → optional report-type override → optional
    capped custom ``system_prompt``) and the generation params (body → ``paper_*``
    config → default), then streams ``summarise_stream``. ``language`` and
    ``focus_question`` are length-capped before entering the prompt so a
    malicious body cannot inject an unbounded string. The provider is the body's
    ``provider`` if it is a known provider, else the configured default. Tokens
    are emitted as ``{token}`` frames, stage messages as ``{info}``, any failure
    as a sanitised ``{error}``, ending with ``[DONE]``.
    """

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        # isinstance (not `if not data`): a truthy non-dict body (JSON array /
        # scalar) would pass `if not data` and then AttributeError on data.get(),
        # surfacing as a 500 instead of this clean 400.
        return jsonify({"error": "Invalid or missing JSON body"}), 400

    model = coerce_non_empty_string(data.get("model"), max_len=128)
    upload_id = coerce_non_empty_string(data.get("upload_id"), max_len=128)
    if not model:
        return jsonify({"error": "Missing model"}), 400
    if not upload_id:
        return jsonify({"error": "Missing upload_id"}), 400

    with DB_LOCK:
        with get_db_connection() as conn:
            row = conn.execute(
                "SELECT extracted_text, filename FROM uploads WHERE upload_id = ?",
                (upload_id,)
            ).fetchone()

    if not row or row[0] is None:
        return jsonify({"error": "Upload not found or text unavailable"}), 400

    stored_text = row[0]
    
    preset_name = str(data.get("preset", "concise")).lower()
    if preset_name == "standard":
        preset_name = "concise"
    preset = _normalise_prompt_preset(PROMPT_PRESETS.get(preset_name))

    sys_prompt, prompt_err, doc_type = _resolve_system_prompt(data, preset)
    if prompt_err:
        return jsonify({"error": prompt_err}), 400

    params = _extract_summarise_params(data)
    # Audience is a small system-prompt suffix, not a separate prompt template.
    audience_modifier = TARGET_AUDIENCE_OPTIONS.get(str(data.get("audience", "")), "")

    from core.llm.factory import ALL_PROVIDER_NAMES
    # str(...) first: a non-string `provider` (JSON number/array) would
    # AttributeError on .strip() → 500; coerce so an invalid type just falls
    # through to the configured default. Mirrors the `format` handling below.
    req_provider = str(data.get("provider", "")).strip().lower()
    summarise_provider = (
        req_provider if req_provider in ALL_PROVIDER_NAMES
        else load_config().get("provider", "ollama")
    )

    def _paper_worker(put, cancel):
        def _info_cb(msg: str) -> None:
            put({"info": msg})

        try:
            for token in summarise_stream(
                stored_text,
                model,
                system_prompt=sys_prompt,
                user_template=preset.get("user_template"),
                doc_type=doc_type,
                audience_modifier=audience_modifier,
                language=cap(data.get("language", "English"), limit=100) or "English",
                focus_question=cap(data.get("focus_question", ""), limit=2_000),
                temperature=params["temperature"],
                max_tokens=params["max_tokens"],
                num_ctx=params["num_ctx"],
                top_p=params["top_p"],
                repeat_penalty=params["repeat_penalty"],
                provider_name=summarise_provider,
                info_cb=_info_cb,
            ):
                if cancel.is_set():
                    break
                if token:
                    put({"token": token})
        except Exception as e:
            if not cancel.is_set():
                put({"error": sanitise_error_msg(e)})

    return run_sse_worker(
        _paper_worker,
        consumer_timeout_s=SSE_SINGLE_SHOT_FLOOR_S + SSE_STALL_MARGIN_S,
    )

@paper_bp.route("/api/upload", methods=["POST"])
def api_upload():
    """Accept a PDF upload, extract its text, and store it under a new id.

    Validates the multipart ``file`` part: a sanitised ``.pdf`` filename and a
    ``Content-Length`` within the ``PDF_LIMIT_SIZE_MB`` cap (a pre-read guard;
    ``process_pdf_upload`` enforces the same byte cap while streaming to disk).
    Extraction runs in a subprocess so a hang can be killed by timeout. Returns
    ``{upload_id, filename}``; a ``ValueError`` (bad/empty PDF) is a 400 and any
    other failure a 500, both sanitised.
    """
    
    if 'file' not in request.files:
        return jsonify({"error": "No file part"}), 400
    
    file = request.files['file']
    if file.filename == '':
        return jsonify({"error": "No selected file"}), 400
    
    filename = secure_filename(file.filename)
    if not filename.lower().endswith(".pdf"):
        return jsonify({"error": "Only PDF files are supported"}), 400

    content_length = request.content_length or 0
    max_bytes = PDF_LIMIT_SIZE_MB * 1024 * 1024
    if content_length > max_bytes:
        return jsonify({"error": f"PDF exceeds {PDF_LIMIT_SIZE_MB} MB limit"}), 400

    from services.pdf_service import process_pdf_upload
    try:
        upload_id, _text = process_pdf_upload(file, filename, max_bytes=max_bytes)
        return jsonify({"upload_id": upload_id, "filename": filename})
    except ValueError as e:
        return jsonify({"error": sanitise_error_msg(e)}), 400
    except Exception as e:
        return jsonify({"error": sanitise_error_msg(e)}), 500

@paper_bp.route("/api/upload/<upload_id>", methods=["DELETE"])
def api_delete_upload(upload_id: str):
    """Delete a stored upload by id.

    The path-segment ``upload_id`` is shape-validated with ``coerce_regex``
    (1-128 of ``[A-Za-z0-9._-]``) before it reaches the parameterised DELETE,
    as defence in depth. Idempotent: deleting an absent id still returns ok.
    """
    if coerce_regex(upload_id, r"[A-Za-z0-9._-]{1,128}") is None:
        return jsonify({"error": "Invalid upload id"}), 400
    with DB_LOCK:
        with get_db_connection() as conn:
            conn.execute("DELETE FROM uploads WHERE upload_id = ?", (upload_id,))
    return jsonify({"ok": True})


@paper_bp.route("/api/export-summary", methods=["POST"])
def api_export_summary():
    """Write a summary to ``~/Downloads`` as ``.txt`` or ``.md``.

    ``format`` is enum-clamped to ``txt``/``md`` (400 otherwise); the filename is
    run through ``secure_filename`` (defaulting to ``summary``) and the content
    is capped at 500k chars. The ``md`` variant gets a ``# Summary - <title>``
    heading. The file is written with ``write_text_atomic`` (temp sibling +
    rename) so a crash/disk-full mid-write cannot leave a truncated file the
    user only notices after deleting the original chat. Returns the output path.
    """
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Invalid request body"}), 400

    raw_format = str(data.get("format", "txt")).lower()
    export_format = coerce_enum(raw_format, ("txt", "md"))
    if export_format is None:
        return jsonify({"error": "Unsupported export format"}), 400

    # str(...) first so a non-string `filename` (JSON number/array) cannot
    # AttributeError on .replace() → 500. Then a Unicode-aware sanitiser instead
    # of secure_filename, which strips ALL non-ASCII and would collapse a French
    # ('résumé') or CJK title to the generic 'summary' (silently overwriting a
    # prior export). See _safe_export_basename.
    raw_name = str(data.get("filename", "summary")).replace(".pdf", "")
    safe_name = _safe_export_basename(raw_name)
    content = cap(data.get("content", ""), limit=500_000)
    if export_format == "md":
        title = safe_name.replace("_", " ").strip() or "Summary"
        content = f"# Summary - {title}\n\n{content}\n"
    
    try:
        downloads = os.path.expanduser("~/Downloads")
        os.makedirs(downloads, exist_ok=True)
        out_path = os.path.join(downloads, f"Summary_{safe_name}.{export_format}")
        # Atomic write (temp sibling in ~/Downloads + rename): a crash or
        # disk-full mid-write must not leave a silently truncated summary
        # that the user only discovers after deleting the original chat.
        write_text_atomic(out_path, content)
        return jsonify({"ok": True, "path": out_path})
    except OSError as e:
        return jsonify({"error": "Failed to write summary file", "details": sanitise_error_msg(e)}), 500

def _cap_strings_recursive(val, max_chars: int):
    """Truncate every string in a nested dict/list structure to *max_chars*.

    Feedback bodies can nest strings inside dict/list fields, so a flat cap on
    top-level values would leave a deeply nested string unbounded. This walks
    the structure and caps each string leaf, leaving non-string scalars intact.
    """
    if isinstance(val, str):
        return val[:max_chars]
    if isinstance(val, dict):
        return {k: _cap_strings_recursive(v, max_chars) for k, v in val.items()}
    if isinstance(val, list):
        return [_cap_strings_recursive(v, max_chars) for v in val]
    return val

@paper_bp.route("/api/feedback", methods=["POST"])
def api_feedback():
    """Persist a feedback record (every string field capped to bound disk use).

    The whole JSON object is passed through :func:`_cap_strings_recursive` with a
    10k-char cap per string leaf before ``save_feedback`` appends it, so a
    crafted body cannot write an unbounded amount to the feedback log.
    """
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Invalid or missing JSON body"}), 400

    # Recursively cap all string values to prevent unbounded disk writes,
    # including strings nested inside dict/list fields.
    _MAX_FIELD = 10_000
    sanitised = {k: _cap_strings_recursive(v, _MAX_FIELD) for k, v in data.items()}

    from core.feedback import save_feedback
    save_feedback(**sanitised)
    return jsonify({"ok": True})

@paper_bp.route("/api/feedback/history")
def api_feedback_history():
    """Return a paginated slice of stored feedback records.

    ``?limit=`` (clamped 1-500, default 50) and ``?offset=`` (clamped, default 0)
    page the full record list; ``total`` reports the unpaginated count.
    """
    from core.feedback import load_feedback
    limit = coerce_int_in_range(request.args.get("limit", 50), 1, 500) or 50
    offset = coerce_int_in_range(request.args.get("offset", 0), 0, 1_000_000_000) or 0
    records = load_feedback()
    return jsonify({"records": records[offset:offset + limit], "total": len(records)})
