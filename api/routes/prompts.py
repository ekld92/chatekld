"""Prompt Hub endpoint — read-only system-prompt transparency.

``GET /api/prompts`` returns the effective system prompt last sent to the LLM
for each app workflow (see :mod:`core.prompt_capture`), plus a placeholder for
workflows not yet exercised this session. It is a pure read of the in-memory
capture sink — it never triggers a model call and writes nothing.

Local-origin gated by the global ``before_request`` hook (``api.security``);
every returned string is already API-key-redacted at capture time, so an
accidentally-logged key can never reach the panel.
"""
from flask import Blueprint, jsonify

from core import prompt_capture

prompts_bp = Blueprint("prompts", __name__)


@prompts_bp.route("/api/prompts", methods=["GET"])
def api_prompts():
    """Return ``{enabled, workflows:[…]}`` for the Prompt Hub panel.

    A thin serialization of :func:`core.prompt_capture.snapshot`. No arguments,
    no side effects — the whole point is that inspecting prompts costs nothing
    and changes nothing.
    """
    return jsonify(prompt_capture.snapshot())
