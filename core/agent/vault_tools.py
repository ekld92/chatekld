"""Concrete tool implementations for the Obsidian vault agent.

Exposes :func:`build_vault_tools` which returns a list of
:class:`~core.agent.tools.ToolSpec` for the three v1 tools:

* ``vault.search`` — hybrid + rerank retrieval over the indexed vault.
* ``vault.read_note`` — full-text fetch of a single .md / .pdf by path.
* ``vault.list_materials`` — list what's currently in the index.

The tools wrap :class:`~rag.vault.ObsidianVaultManager` only — they
never touch the indexer's internal data structures directly, which
keeps the agent layer separable from the indexing layer.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from core.agent.tools import ToolSpec
from core.llm.types import ToolSchema


# Per-result snippet cap for vault.search — keeps each chunk small so a
# top_k=12 query stays under ~10 KB total.
_SEARCH_SNIPPET_CHARS = 800
_LIST_MATERIALS_DEFAULT_LIMIT = 100
_LIST_MATERIALS_MAX_LIMIT = 200
_READ_NOTE_MAX_CHARS = 32000


@dataclass(frozen=True)
class VaultToolContext:
    """Per-turn runtime config the vault tools need to talk to the manager.

    Carried in a small immutable struct so the agent loop can build
    tools once at the top of a turn and share them across every
    iteration. All fields are taken from the same config the route
    handler used to set up the chat, so the agent's retrieval is
    apples-to-apples with the legacy single-shot RAG path.
    """
    llm_name: str
    embed_name: str
    provider_name: str
    similarity_cutoff: float = 0.25
    hybrid_enabled: bool = False
    reranker_enabled: bool = False
    reranker_model: str = ""


def build_vault_tools(manager: Any, ctx: VaultToolContext) -> list[ToolSpec]:
    """Construct the three vault tool specs bound to *manager*.

    *manager* is duck-typed: it must expose ``retrieve``, ``read_note``,
    and ``get_indexed_materials`` matching :class:`ObsidianVaultManager`.
    The seam exists so tests can pass a MagicMock without needing the
    full vault stack.
    """
    return [
        _build_search_tool(manager, ctx),
        _build_read_note_tool(manager),
        _build_list_materials_tool(manager),
    ]


# ---------------------------------------------------------------------------
# vault.search
# ---------------------------------------------------------------------------

_SEARCH_SCHEMA = ToolSchema(
    name="vault.search",
    description=(
        "Search the indexed Obsidian vault for passages relevant to a query. "
        "Returns chunks with source filename, relevance score, and a snippet. "
        "Use this when you need evidence from the user's notes."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Natural-language search query.",
            },
            "top_k": {
                "type": "integer",
                "description": "How many chunks to return (1–12).",
                "minimum": 1,
                "maximum": 12,
            },
        },
        "required": ["query"],
    },
)


def _build_search_tool(manager: Any, ctx: VaultToolContext) -> ToolSpec:
    def _run(args: dict) -> str:
        query = args["query"]
        top_k = int(args.get("top_k", 6))
        chunks = manager.retrieve(
            query,
            llm_name=ctx.llm_name,
            embed_name=ctx.embed_name,
            top_k=top_k,
            provider_name=ctx.provider_name,
            similarity_cutoff=ctx.similarity_cutoff,
            top_k_explicit=True,
            hybrid_enabled=ctx.hybrid_enabled,
            reranker_enabled=ctx.reranker_enabled,
            reranker_model=ctx.reranker_model,
        )
        results = []
        any_truncated = False
        for i, chunk in enumerate(chunks, start=1):
            snippet = chunk.text or ""
            snippet_truncated = len(snippet) > _SEARCH_SNIPPET_CHARS
            if snippet_truncated:
                snippet = snippet[:_SEARCH_SNIPPET_CHARS] + " ..."
                any_truncated = True
            results.append({
                "index": i,
                "source": chunk.source,
                "score": round(float(chunk.score), 4),
                "snippet": snippet,
            })
        return json.dumps({
            "results": results,
            "result_count": len(results),
            "truncated": any_truncated,
        }, ensure_ascii=False)

    return ToolSpec(schema=_SEARCH_SCHEMA, runner=_run, max_output_chars=12000)


# ---------------------------------------------------------------------------
# vault.read_note
# ---------------------------------------------------------------------------

_READ_NOTE_SCHEMA = ToolSchema(
    name="vault.read_note",
    description=(
        "Read the full text of a markdown note or PDF in the vault by "
        "relative path. Use this after vault.search when a snippet is not "
        "enough. Returns truncated text if the document exceeds the cap."
    ),
    parameters={
        "type": "object",
        "properties": {
            "rel_path": {
                "type": "string",
                "description": (
                    "Vault-relative path, POSIX-style with forward slashes "
                    "(e.g. 'work/2026/meeting.md')."
                ),
            },
        },
        "required": ["rel_path"],
    },
)


def _build_read_note_tool(manager: Any) -> ToolSpec:
    def _run(args: dict) -> str:
        rel_path = args["rel_path"]
        text, truncated = manager.read_note(rel_path, max_chars=_READ_NOTE_MAX_CHARS)
        return json.dumps({
            "rel_path": rel_path,
            "text": text,
            "truncated": bool(truncated),
            "char_count": len(text),
        }, ensure_ascii=False)

    return ToolSpec(
        schema=_READ_NOTE_SCHEMA,
        runner=_run,
        max_output_chars=_READ_NOTE_MAX_CHARS + 512,
    )


# ---------------------------------------------------------------------------
# vault.list_materials
# ---------------------------------------------------------------------------

_LIST_MATERIALS_SCHEMA = ToolSchema(
    name="vault.list_materials",
    description=(
        "List files currently indexed in the vault. Useful to discover "
        "what's available before searching. Optional case-insensitive "
        "substring filter on the path."
    ),
    parameters={
        "type": "object",
        "properties": {
            "filter": {
                "type": "string",
                "description": "Case-insensitive substring match on the relative path.",
            },
            "limit": {
                "type": "integer",
                "description": "Max materials to return (1–200, default 100).",
                "minimum": 1,
                "maximum": _LIST_MATERIALS_MAX_LIMIT,
            },
        },
        "required": [],
    },
)


def _build_list_materials_tool(manager: Any) -> ToolSpec:
    def _run(args: dict) -> str:
        filter_text = (args.get("filter") or "").lower()
        limit = int(args.get("limit") or _LIST_MATERIALS_DEFAULT_LIMIT)
        if limit < 1:
            limit = 1
        if limit > _LIST_MATERIALS_MAX_LIMIT:
            limit = _LIST_MATERIALS_MAX_LIMIT

        manifest = manager.get_indexed_materials() or {}
        all_materials = manifest.get("materials") or []
        if filter_text:
            filtered = [
                m for m in all_materials
                if isinstance(m, dict)
                and filter_text in str(m.get("source", "")).lower()
            ]
        else:
            filtered = [m for m in all_materials if isinstance(m, dict)]
        total = len(filtered)
        returned = filtered[:limit]
        return json.dumps({
            "materials": returned,
            "total": total,
            "returned": len(returned),
            "truncated": total > len(returned),
        }, ensure_ascii=False)

    return ToolSpec(schema=_LIST_MATERIALS_SCHEMA, runner=_run, max_output_chars=20000)
