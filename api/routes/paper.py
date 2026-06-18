import logging
import json
import os
from flask import Blueprint, request, jsonify, Response
from werkzeug.utils import secure_filename

from core.constants import (
    PDF_LIMIT_SIZE_MB,
    PROMPT_PRESETS,
    SYSTEM_PROMPT_LIMIT,
    TARGET_AUDIENCE_OPTIONS,
)
from core.database import get_db_connection, DB_LOCK
from core.config import load_config, load_report_types
from core.utils import (
    cap, parse_temperature, parse_num_ctx, parse_num_predict,
    parse_top_p, parse_repeat_penalty, write_text_atomic
)
from api.security import origin_is_local, sanitise_error_msg
from api.validators import (
    coerce_enum,
    coerce_int_in_range,
    coerce_non_empty_string,
    coerce_regex,
)
from rag.summarizer import summarise_stream

paper_bp = Blueprint('paper', __name__)
logger = logging.getLogger(__name__)

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
    if not origin_is_local():
        return jsonify({"error": "Forbidden"}), 403

    data = request.get_json(silent=True)
    if not data:
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
    req_provider = data.get("provider", "").strip().lower()
    summarise_provider = (
        req_provider if req_provider in ALL_PROVIDER_NAMES
        else load_config().get("provider", "ollama")
    )

    def generate():
        info_queue: list[str] = []

        def _info_cb(msg: str) -> None:
            info_queue.append(msg)

        try:
            for token in summarise_stream(
                stored_text,
                model,
                system_prompt=sys_prompt,
                user_template=preset.get("user_template"),
                doc_type=doc_type,
                audience_modifier=audience_modifier,
                # Cap language like focus_question so a malicious body cannot
                # push an unbounded string into the prompt; fall back to the
                # default for empty / non-string values.
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
                while info_queue:
                    msg = info_queue.pop(0)
                    yield f"data: {json.dumps({'info': msg})}\n\n"
                yield f"data: {json.dumps({'token': token})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': sanitise_error_msg(e)})}\n\n"
        yield "data: [DONE]\n\n"

    return Response(generate(), mimetype="text/event-stream")

@paper_bp.route("/api/upload", methods=["POST"])
def api_upload():
    if not origin_is_local():
        return jsonify({"error": "Forbidden"}), 403
    
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
    if not origin_is_local():
        return jsonify({"error": "Forbidden"}), 403
    if coerce_regex(upload_id, r"[A-Za-z0-9._-]{1,128}") is None:
        return jsonify({"error": "Invalid upload id"}), 400
    with DB_LOCK:
        with get_db_connection() as conn:
            conn.execute("DELETE FROM uploads WHERE upload_id = ?", (upload_id,))
    return jsonify({"ok": True})


@paper_bp.route("/api/export-summary", methods=["POST"])
def api_export_summary():
    if not origin_is_local():
        return jsonify({"error": "Forbidden"}), 403
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Invalid request body"}), 400
    
    raw_format = str(data.get("format", "txt")).lower()
    export_format = coerce_enum(raw_format, ("txt", "md"))
    if export_format is None:
        return jsonify({"error": "Unsupported export format"}), 400

    raw_name = data.get("filename", "summary").replace(".pdf", "")
    safe_name = secure_filename(raw_name) or "summary"
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
    if isinstance(val, str):
        return val[:max_chars]
    if isinstance(val, dict):
        return {k: _cap_strings_recursive(v, max_chars) for k, v in val.items()}
    if isinstance(val, list):
        return [_cap_strings_recursive(v, max_chars) for v in val]
    return val

@paper_bp.route("/api/feedback", methods=["POST"])
def api_feedback():
    if not origin_is_local():
        return jsonify({"error": "Forbidden"}), 403
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
    if not origin_is_local():
        return jsonify({"error": "Forbidden"}), 403
    from core.feedback import load_feedback
    limit = coerce_int_in_range(request.args.get("limit", 50), 1, 500) or 50
    offset = coerce_int_in_range(request.args.get("offset", 0), 0, 1_000_000_000) or 0
    records = load_feedback()
    return jsonify({"records": records[offset:offset + limit], "total": len(records)})
