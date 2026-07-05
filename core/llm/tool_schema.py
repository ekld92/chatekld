"""Per-provider serialisation and parsing for the tool-use wire format.

Each online provider speaks a slightly different dialect for declaring
tools and emitting tool calls:

* **OpenAI** wraps each tool as ``{"type": "function", "function": {...}}``
  and returns ``tool_calls[i].function.arguments`` as a JSON *string*.
* **Anthropic** uses ``{"name", "description", "input_schema"}`` and
  returns ``input`` already parsed.
* **Gemini** declares functions via ``{"function_declarations": [...]}``
  and returns ``functionCall.args`` as an object, with no provider-side
  ID — the adapter synthesises one.

Keeping the conversions in one module makes the adapters thin and the
serialisation logic unit-testable without an HTTP mock.
"""
from __future__ import annotations

import json
import uuid
from typing import Any, Optional

from core.llm.types import LLMRequest, ToolCall, ToolSchema, ToolTurn


def jsonschema_to_openai_tool(spec: ToolSchema) -> dict[str, Any]:
    """Serialise a :class:`ToolSchema` to OpenAI's
    ``{"type": "function", "function": {... "parameters": <json-schema>}}``
    dialect. Also used verbatim by the local adapter for Ollama 0.4+ and LM
    Studio, which accept the same shape. An empty schema becomes the permissive
    ``{"type": "object", "properties": {}}``."""
    return {
        "type": "function",
        "function": {
            "name": spec.name,
            "description": spec.description,
            "parameters": spec.parameters or {"type": "object", "properties": {}},
        },
    }


def jsonschema_to_anthropic_tool(spec: ToolSchema) -> dict[str, Any]:
    """Serialise a :class:`ToolSchema` to Anthropic's dialect, where the
    parameter schema is keyed ``input_schema`` (not ``parameters``)."""
    return {
        "name": spec.name,
        "description": spec.description,
        "input_schema": spec.parameters or {"type": "object", "properties": {}},
    }


def jsonschema_to_gemini_tool(spec: ToolSchema) -> dict[str, Any]:
    """Return one function declaration in Gemini's dialect.

    The adapter wraps a list of these in
    ``{"function_declarations": [...]}`` at the tools-list level.
    """
    return {
        "name": spec.name,
        "description": spec.description,
        "parameters": _sanitise_for_gemini(
            spec.parameters or {"type": "object", "properties": {}}
        ),
    }


def _sanitise_for_gemini(schema: Any) -> Any:
    """Strip JSON-Schema fields Gemini's parameters spec rejects.

    Gemini's function-calling endpoint rejects ``default`` and
    ``additionalProperties`` (and a few others). Strip recursively
    without mutating the input.
    """
    if isinstance(schema, dict):
        out: dict[str, Any] = {}
        for key, value in schema.items():
            if key in ("default", "additionalProperties"):
                continue
            out[key] = _sanitise_for_gemini(value)
        return out
    if isinstance(schema, list):
        return [_sanitise_for_gemini(v) for v in schema]
    return schema


def parse_openai_tool_call(raw: Any) -> Optional[ToolCall]:
    """Parse one entry of ``choices[i].message.tool_calls``.

    Returns ``None`` if the structure is wrong or the JSON arguments
    fail to parse — the agent loop counts a ``None`` here as a
    malformed call.
    """
    if not isinstance(raw, dict):
        return None
    call_id = raw.get("id")
    function = raw.get("function")
    if not isinstance(call_id, str) or not isinstance(function, dict):
        return None
    name = function.get("name")
    raw_args = function.get("arguments", "")
    if not isinstance(name, str):
        return None
    if isinstance(raw_args, dict):
        args = raw_args
        try:
            raw_args_str = json.dumps(raw_args)
        except (TypeError, ValueError):
            raw_args_str = ""
    elif isinstance(raw_args, str):
        raw_args_str = raw_args
        if raw_args:
            try:
                args = json.loads(raw_args)
            except (json.JSONDecodeError, TypeError):
                return None
            if not isinstance(args, dict):
                return None
        else:
            args = {}
    else:
        return None
    return ToolCall(id=call_id, name=name, arguments=args, raw_arguments=raw_args_str)


def parse_anthropic_tool_use(raw: Any) -> Optional[ToolCall]:
    """Parse one content block of type ``tool_use`` from Anthropic."""
    if not isinstance(raw, dict):
        return None
    if raw.get("type") != "tool_use":
        return None
    call_id = raw.get("id")
    name = raw.get("name")
    inp = raw.get("input", {})
    if not isinstance(call_id, str) or not isinstance(name, str):
        return None
    if not isinstance(inp, dict):
        return None
    try:
        raw_args = json.dumps(inp)
    except (TypeError, ValueError):
        raw_args = ""
    return ToolCall(id=call_id, name=name, arguments=inp, raw_arguments=raw_args)


def parse_gemini_function_call(
    raw: Any, *, thought_signature: str = "",
) -> Optional[ToolCall]:
    """Parse a ``functionCall`` payload from a Gemini response part.

    Gemini does not return an ID with each call; the adapter
    synthesises a stable-enough one so the agent loop can route
    ``ToolResult.tool_call_id`` back through the response side.

    ``thought_signature`` is the part-level ``thoughtSignature`` (the
    signature lives on the *part*, not inside ``functionCall``, so the
    adapter passes it in). Gemini 3.x requires it echoed back on the
    corresponding part in the next request; see
    :func:`build_gemini_contents`.
    """
    if not isinstance(raw, dict):
        return None
    name = raw.get("name")
    args = raw.get("args", {})
    if not isinstance(name, str):
        return None
    if not isinstance(args, dict):
        return None
    call_id = f"call_{uuid.uuid4().hex[:12]}"
    try:
        raw_args = json.dumps(args)
    except (TypeError, ValueError):
        raw_args = ""
    return ToolCall(
        id=call_id, name=name, arguments=args, raw_arguments=raw_args,
        thought_signature=thought_signature or "",
    )


def build_openai_messages(request: LLMRequest) -> list[dict[str, Any]]:
    """Build OpenAI-shape ``messages`` including tool history.

    Also used by the local adapter for Ollama (0.4+) and LM Studio's
    OpenAI-compatible endpoint, both of which accept the same shape.
    """
    msgs: list[dict[str, Any]] = []
    if request.system_prompt:
        msgs.append({"role": "system", "content": request.system_prompt})
    for msg in request.messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if content:
            msgs.append({"role": role, "content": content})
    for turn in request.tool_history:
        if turn.calls:
            msgs.append({
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": c.id,
                        "type": "function",
                        "function": {
                            "name": c.name,
                            "arguments": c.raw_arguments or json.dumps(c.arguments or {}),
                        },
                    }
                    for c in turn.calls
                ],
            })
        for r in turn.results:
            msgs.append({
                "role": "tool",
                "tool_call_id": r.tool_call_id,
                "content": r.content,
            })
    return msgs


def build_anthropic_messages(request: LLMRequest) -> list[dict[str, Any]]:
    """Build Anthropic-shape ``messages`` including tool history.

    Anthropic uses content blocks of type ``tool_use`` on the assistant
    turn and ``tool_result`` blocks (still inside a ``user`` role) for
    observations. The system prompt is sent separately as the top-level
    ``system`` field by the caller; we ignore any ``role=="system"`` in
    ``request.messages``.
    """
    msgs: list[dict[str, Any]] = []
    for msg in request.messages:
        role = msg.get("role", "user")
        if role == "system":
            continue
        content = msg.get("content", "")
        if content:
            msgs.append({"role": role, "content": content})
    for turn in request.tool_history:
        if turn.calls:
            blocks: list[dict[str, Any]] = []
            for c in turn.calls:
                blocks.append({
                    "type": "tool_use",
                    "id": c.id,
                    "name": c.name,
                    "input": c.arguments or {},
                })
            msgs.append({"role": "assistant", "content": blocks})
        if turn.results:
            result_blocks: list[dict[str, Any]] = []
            for r in turn.results:
                block = {
                    "type": "tool_result",
                    "tool_use_id": r.tool_call_id,
                    "content": r.content,
                }
                if r.is_error:
                    block["is_error"] = True
                result_blocks.append(block)
            msgs.append({"role": "user", "content": result_blocks})
    return msgs


def build_gemini_contents(request: LLMRequest) -> list[dict[str, Any]]:
    """Build Gemini-shape ``contents`` including tool history.

    Gemini ties responses back to calls by ``name`` rather than ID, so
    we look up the call name within each turn when emitting the
    ``functionResponse`` block. Tool errors are signalled by placing the
    body under an ``error`` key instead of ``content``.
    """
    contents: list[dict[str, Any]] = []
    for msg in request.messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if not content:
            continue
        gemini_role = "user" if role in ("user", "system") else "model"
        contents.append({"role": gemini_role, "parts": [{"text": content}]})
    for turn in request.tool_history:
        if turn.calls:
            call_parts: list[dict[str, Any]] = []
            for c in turn.calls:
                part: dict[str, Any] = {
                    "functionCall": {"name": c.name, "args": c.arguments or {}}
                }
                # Gemini 3.x REQUIRES the thoughtSignature captured from the
                # model's functionCall part to be echoed back verbatim on that
                # part — omitting it 400s the second tool turn. Older Gemini
                # models never set it, so this stays absent for them.
                if c.thought_signature:
                    part["thoughtSignature"] = c.thought_signature
                call_parts.append(part)
            contents.append({"role": "model", "parts": call_parts})
        if turn.results:
            parts: list[dict[str, Any]] = []
            for r in turn.results:
                name = _lookup_call_name(turn, r.tool_call_id)
                response_body: dict[str, Any]
                if r.is_error:
                    response_body = {"error": r.content}
                else:
                    response_body = {"content": r.content}
                parts.append({
                    "functionResponse": {
                        "name": name,
                        "response": response_body,
                    }
                })
            contents.append({"role": "user", "parts": parts})
    if not contents:
        contents.append({"role": "user", "parts": [{"text": ""}]})
    return contents


def _lookup_call_name(turn: ToolTurn, tool_call_id: str) -> str:
    for c in turn.calls:
        if c.id == tool_call_id:
            return c.name
    return ""
