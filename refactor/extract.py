"""On-demand, single-image vision re-extraction (the ONLY vision-calling path).

Never called by the read-only ``plan`` — only by ``POST /api/refactor/extract-image``
for one user-chosen image at a time. ``extract_table`` routes through the
low-level ``services.vision`` transports directly (with a table prompt and **no**
description-path downscale, per design §6) and does a **double read for
self-consistency**, flagging cells that disagree as suspect; ``redescribe`` reuses
``vision_manager.describe_image``. Results persist via ``cache.write_mode`` under
``obsidian_cache/`` only — never the indexer's base cache, never the vault.
"""
from __future__ import annotations

import base64
import hashlib
import re
import threading
from pathlib import Path

from core.config import load_config
from core.constants import (
    DEFAULT_OCR_MAX_TOKENS,
    DEFAULT_VISION_MODEL,
    DEFAULT_VISION_TIMEOUT_S,
)
from rag.vault import obsidian_manager
from services.vision import (
    _cfg_bounded_int,
    _chat_lm_studio_image,
    _chat_ollama_image,
    vision_manager,
)

from refactor import cache

_TABLE_PROMPT = (
    "This image contains a table. Transcribe it EXACTLY as a GitHub-flavored "
    "markdown table. Preserve every row and column and every number, unit and "
    "symbol verbatim — do not round, convert units, summarize, reorder, or "
    "invent cells. One '|'-delimited row per table row, with a header separator "
    "line. Leave an empty cell empty. Output ONLY the markdown table and nothing "
    "else. If the image is not a table, output the single line: NO_TABLE"
)

NO_TABLE_SENTINEL = "NO_TABLE"

# One cheap classification pass: the model picks exactly one of these labels.
# ``handwritten`` is the one that matters most (it drives the "can't OCR" badge
# and the suggested ignore), so the synonym scan below biases toward it.
CLASSIFY_LABELS = ("printed-table", "figure-diagram", "handwritten", "photo", "other")
_CLASSIFY_MAX_TOKENS = 32

_CLASSIFY_PROMPT = (
    "Classify this image into EXACTLY ONE category and reply with only the "
    "category word, nothing else:\n"
    "- printed-table : a printed or typeset table / grid of rows and columns\n"
    "- figure-diagram : a diagram, chart, graph, schema or illustration\n"
    "- handwritten : handwritten notes or a hand-drawn sketch\n"
    "- photo : a photograph of a real-world scene or object\n"
    "- other : anything else\n"
    "Answer with one word: printed-table, figure-diagram, handwritten, photo, or other."
)

# Fuzzy fallback for chatty replies. Checked in this order, so the more
# consequential ``handwritten`` wins over ``table`` for "a handwritten table".
_LABEL_SYNONYMS = (
    ("handwritten", ("handwritten", "hand-written", "hand written", "handwriting",
                     "manuscrit", "écriture", "ecriture", "sketch", "croquis")),
    ("printed-table", ("printed-table", "printed table", "table", "tableau",
                       "grid", "grille", "spreadsheet")),
    ("figure-diagram", ("figure-diagram", "figure", "diagram", "diagramme", "schema",
                        "schéma", "chart", "graph", "graphique", "graphe",
                        "illustration", "plot", "courbe")),
    ("photo", ("photo", "photograph", "photographie", "picture", "image of")),
    ("other", ("other", "autre", "none", "aucun", "unknown", "inconnu")),
)

# Serialize the tool's own vision calls. The UI disables a single image's two
# buttons during its call, but the user can trigger extractions on *different*
# images concurrently; without this lock those would fire simultaneous requests
# at the local model (on top of whatever the indexer is doing) and risk
# overloading/stalling it. We hold the lock only around the inference calls (not
# the disk read / base64 / cache write) to keep the critical section short.
# NOTE: this does NOT synchronize against the indexer's own vision calls — they
# use a different code path. That is acceptable because the indexer issues image
# descriptions strictly one-at-a-time, and extraction here is a deliberate,
# low-frequency user action; full cross-subsystem serialization is out of scope
# for Phase 1.
_VISION_LOCK = threading.Lock()


def _resolve_model(cfg: dict) -> str:
    """Pick the vision model for an extraction pass.

    Prefers the refactor-specific override (``refactor_extract_model``) so the
    user can point table/redescribe/classify at a stronger model than the
    indexer's ``vision_model``, then that model, then the hard default.
    """
    return cfg.get("refactor_extract_model") or cfg.get("vision_model") or DEFAULT_VISION_MODEL


def _read_image(rel_path: str, vault_root: Path) -> tuple[bytes, str]:
    """Read image bytes (materializing iCloud placeholders) + sha256 digest.

    Raises ``OSError`` (missing/unreadable) or ``ValueError`` (over the 20 MB
    cap) — the route maps both to a 400 with a sanitized message.
    """
    p = vault_root / rel_path
    size = p.stat().st_size
    if size > obsidian_manager._IMAGE_MAX_BYTES:
        raise ValueError(f"image exceeds the {obsidian_manager._IMAGE_MAX_BYTES}-byte cap")
    data = p.read_bytes()
    return data, hashlib.sha256(data).hexdigest()


def _vision_call(prompt: str, b64: str, *, max_tokens: int) -> str:
    """Send one prompt+image to the configured vision transport; "" on no output.

    Routes to the LM Studio or Ollama low-level transport per ``vision_provider``,
    applying the always-on ``vision_timeout_s`` bound (read per call so a Settings
    change takes effect without restart) so a stuck local model cannot hang the
    user-triggered extraction.
    """
    cfg = load_config()
    provider = cfg.get("vision_provider", "ollama")
    model = _resolve_model(cfg)
    timeout = _cfg_bounded_int("vision_timeout_s", DEFAULT_VISION_TIMEOUT_S, 5, 600)
    if provider == "lm_studio":
        return _chat_lm_studio_image(model, prompt, b64, timeout=timeout, max_tokens=max_tokens) or ""
    return _chat_ollama_image(model, prompt, b64, timeout=timeout, max_tokens=max_tokens) or ""


def _parse_md_table(text: str) -> list[list[str]]:
    """Parse a markdown table into a grid of trimmed cell strings.

    Separator rows (``|---|---|``) are dropped; non-pipe lines are ignored.
    """
    rows: list[list[str]] = []
    for line in text.splitlines():
        s = line.strip()
        if "|" not in s:
            continue
        stripped = s.strip("|")
        compact = stripped.replace("|", "").replace(":", "").replace(" ", "")
        if compact and set(compact) <= {"-"}:
            continue  # header separator row
        rows.append([c.strip() for c in stripped.split("|")])
    return rows


def _suspect_cells(first: str, second: str) -> list[list[int]]:
    """Return ``[row, col]`` positions where the two reads disagree.

    A shape mismatch marks the union of positions as suspect (low confidence).
    """
    g1, g2 = _parse_md_table(first), _parse_md_table(second)
    suspect: list[list[int]] = []
    nrows = max(len(g1), len(g2))
    for r in range(nrows):
        row1 = g1[r] if r < len(g1) else []
        row2 = g2[r] if r < len(g2) else []
        ncols = max(len(row1), len(row2))
        for c in range(ncols):
            v1 = row1[c] if c < len(row1) else None
            v2 = row2[c] if c < len(row2) else None
            if v1 != v2:
                suspect.append([r, c])
    return suspect


def extract_table(rel_path: str, vault_root: Path, *, double_read: bool = True) -> dict:
    """Extract a markdown table from one image. Caches a non-empty result."""
    result = {"mode": "table", "text": "", "suspect_cells": [], "cached": False, "error": ""}
    try:
        data, digest = _read_image(rel_path, vault_root)
    except (OSError, ValueError) as exc:
        result["error"] = str(exc)
        return result
    b64 = base64.b64encode(data).decode("ascii")
    max_tokens = _cfg_bounded_int("ocr_max_tokens", DEFAULT_OCR_MAX_TOKENS, 64, 8192)
    # Both reads run inside one lock acquisition so a single image's double-read
    # is never interleaved with another extraction's calls.
    with _VISION_LOCK:
        try:
            first = _vision_call(_TABLE_PROMPT, b64, max_tokens=max_tokens).strip()
        except Exception as exc:  # noqa: BLE001 — surface any transport error
            result["error"] = f"{type(exc).__name__}: {exc}"
            return result
        if double_read:
            try:
                second = _vision_call(_TABLE_PROMPT, b64, max_tokens=max_tokens).strip()
            except Exception:  # noqa: BLE001 — a failed second read just skips the check
                second = ""
            if second:
                result["suspect_cells"] = _suspect_cells(first, second)
    result["text"] = first
    if first and first.upper() != NO_TABLE_SENTINEL:
        try:
            cache.write_mode(digest, vault_root, "table", first)
            result["cached"] = True
        except Exception:  # noqa: BLE001 — cache write is best-effort
            result["cached"] = False
    return result


def redescribe(rel_path: str, vault_root: Path) -> dict:
    """Run a fresh description for one image (downscaled path). Caches non-empty."""
    result = {"mode": "redescribe", "text": "", "suspect_cells": [], "cached": False, "error": ""}
    try:
        data, digest = _read_image(rel_path, vault_root)
    except (OSError, ValueError) as exc:
        result["error"] = str(exc)
        return result
    b64 = base64.b64encode(data).decode("ascii")
    # describe_image catches its own errors and returns "" on failure. Held
    # under the same lock as the table path so concurrent user-triggered
    # extractions on different images don't pile onto the local model at once.
    with _VISION_LOCK:
        text = (vision_manager.describe_image(b64) or "").strip()
    result["text"] = text
    if not text:
        result["error"] = "Vision model returned no description."
        return result
    try:
        cache.write_mode(digest, vault_root, "redescribe", text)
        result["cached"] = True
    except Exception:  # noqa: BLE001 — best-effort
        result["cached"] = False
    return result


def _parse_label(raw: str) -> str:
    """Map a (possibly chatty) model reply to one canonical label; default 'other'.

    Word-boundary matching (not bare substring) so e.g. "photograph" matches
    ``photo`` not ``graph`` — substring matching mis-routed it to figure-diagram.
    """
    low = (raw or "").strip().lower()
    if not low:
        return "other"
    for label in CLASSIFY_LABELS:        # exact one-word reply (the happy path)
        if low == label:
            return label
    for label, tokens in _LABEL_SYNONYMS:  # fuzzy fallback, handwritten-biased
        for tok in tokens:
            if re.search(r"\b" + re.escape(tok) + r"\b", low):
                return label
    return "other"


def classify(rel_path: str, vault_root: Path) -> dict:
    """Classify one image into a single label via one vision pass.

    Returns ``{mode, label, text, suspect_cells, cached, error}`` (the
    ``suspect_cells`` key is empty — kept for response-shape parity with the
    table/redescribe modes). Caches the parsed label under the ``classify`` mode
    only; never touches the indexer's base ``<sha256>.txt`` or the vault.
    """
    result = {"mode": "classify", "label": "", "text": "",
              "suspect_cells": [], "cached": False, "error": ""}
    try:
        data, digest = _read_image(rel_path, vault_root)
    except (OSError, ValueError) as exc:
        result["error"] = str(exc)
        return result
    b64 = base64.b64encode(data).decode("ascii")
    with _VISION_LOCK:
        try:
            raw = _vision_call(_CLASSIFY_PROMPT, b64, max_tokens=_CLASSIFY_MAX_TOKENS).strip()
        except Exception as exc:  # noqa: BLE001 — surface any transport error
            result["error"] = f"{type(exc).__name__}: {exc}"
            return result
    result["text"] = raw
    if not raw:
        # An empty (non-exception) reply is a *failed* pass, not a genuine
        # "other" classification. Caching "other" here would make a future plan
        # run show a confident-but-bogus label with no signal it was a miss, and
        # would suppress a retry. So surface an error and write nothing — mirrors
        # redescribe (empty ⇒ error) and extract_table (skips caching NO_TABLE).
        result["error"] = "Vision model returned no classification."
        return result
    result["label"] = _parse_label(raw)
    try:
        cache.write_mode(digest, vault_root, "classify", result["label"])
        result["cached"] = True
    except Exception:  # noqa: BLE001 — cache write is best-effort
        result["cached"] = False
    return result
