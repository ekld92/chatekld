"""Explicit, one-file online OCR jobs; results stay outside the vault/index."""
from __future__ import annotations

import hashlib
from pathlib import Path
import re
import threading
import time
from typing import cast

import fitz
from flask import Blueprint, jsonify, request, send_file

from api.security import sanitise_error_msg
from core.config import load_config
from core.constants import (BASE_DIR, OBSIDIAN_CACHE_DIR, OBSIDIAN_INDEX_DIR,
                            DEFAULT_EMBED,
                            VAULT_BINARY_EXTS, VAULT_IMAGE_EXTS,
                            VAULT_MD_EXTS)
from core.utils import nfc
from core.online_ocr import ERROR_FIELDS, NativeOnlyOCR, OnlineOCR, extract_image, export_image
from core.page_ocr import _has_substantial_image, _has_visual_figure, extract_pages, export_pages
from rag.vault import _iter_vault_scan_files, obsidian_manager
from rag.storage_io import vault_key

online_ocr_bp = Blueprint("online_ocr", __name__)
_lock = threading.Lock()
_job: dict = {"state": "idle"}
_cancel = threading.Event()
_ONLINE_INDEX_RANGE_PAGES = 50


@online_ocr_bp.get('/api/online-ocr/resources')
def extraction_resources():
    """Observe memory only; never start/retry extraction or access the vault."""
    from core.ocr_resources import MemoryGuard
    guard = MemoryGuard()
    reason = guard()
    sample = guard.samples[-1] if guard.samples else {}
    return jsonify({
        'state': 'blocked' if reason else 'warning' if sample.get('pressure') == 2 else 'ready',
        'stop_reason': reason,
        'pressure': sample.get('pressure'),
        'swap_used_mib': sample.get('swap_used_mib'),
        'message': ('Memory monitoring is unavailable. Review diagnostics before retrying.'
                    if reason == 'memory_monitor_unavailable' else
                    'macOS reports critical memory pressure. Reduce memory use before reviewing a retry.'
                    if reason else
                    'macOS reports warning pressure. Bounded preparation is allowed; safety limits remain enabled.'
                    if sample.get('pressure') == 2 else
                    'Memory check passed. Review unfinished preparation before retrying.'),
    })


def _index_settings(cfg: dict) -> dict:
    from core.config import resolve_chat_model, resolve_embed_provider
    provider = cfg.get("provider", "ollama")
    return {"provider": provider,
            "llm": resolve_chat_model(cfg, provider),
            "embed": cfg.get("embed", DEFAULT_EMBED),
            "embed_provider": resolve_embed_provider(cfg, provider)}


def _continuation_scope(cfg: dict) -> str:
    """Bind recovery to binary inventory and extraction scope, independently of caches."""
    import json
    vault = Path(cfg["obsidian_vault_path"]).resolve(strict=True)
    signatures = obsidian_manager._load_pdf_signature_cache()
    binding = {"vault": str(vault),
               "exclusions": cfg.get("vault_exclude_dirs", []),
               "image_extensions": cfg.get("vault_image_exts"),
               "sources": [{"source": rel, **obsidian_manager._pdf_file_signature(path, signatures, rel)}
                           for path, rel in sorted(_discover_sources(vault), key=lambda row: row[1])]}
    return hashlib.sha256(json.dumps(binding, sort_keys=True).encode()).hexdigest()


def _check_continuation_scope(evidence: dict, cfg: dict) -> None:
    if not evidence.get("scope_binding") or evidence["scope_binding"] != _continuation_scope(cfg):
        raise ValueError("Extraction scope changed or legacy scope evidence is missing; continuation refused")


def _index_outcome(manifest: dict, *, full_rebuild: bool = False) -> dict:
    deferred = set(manifest.get("deferred", [])) | getattr(obsidian_manager, "_scan_failed_sources", set())
    return {"deferred_count": len(deferred),
            "deferred_content": "omitted_from_new_index" if full_rebuild else "retained_if_present",
            "state": ("completed_with_omissions" if deferred else "indexed")
                     if obsidian_manager.get_status() == "done" else "index_incomplete"}


def _checkpoint_details() -> dict:
    return {key: _job[key] for key in ("deferred_count", "deferred_content", "failure") if key in _job}


def _preflight_index_settings(settings: dict, *, force_fresh: bool = False) -> None:
    """Refuse provider spend when the local embed target cannot match the index."""
    from core.providers import get_provider

    embed_provider = settings["embed_provider"]
    if embed_provider not in ("ollama", "lm_studio"):
        raise ValueError("Online index runs require a local embedding provider")
    from rag.index_admission import require_index_admission
    require_index_admission(obsidian_manager, OBSIDIAN_INDEX_DIR,
                            load_config().get("obsidian_vault_path", ""), settings,
                            force_fresh=force_fresh)
    try:
        installed, error = get_provider(embed_provider).get_models()
    except Exception as exc:
        raise ValueError("Local embedding provider is unavailable") from exc
    model = settings["embed"]
    base = model.split(":")[0]
    if error or not installed or not any(
            item == model or item.split(":")[0] == base for item in installed):
        raise ValueError(f"Local embedding model '{model}' is unavailable on {embed_provider}")


def _source(value: str) -> tuple[Path, Path, str]:
    vault_value = load_config().get("obsidian_vault_path")
    if not vault_value or not isinstance(value, str) or not value or len(value) > 1024:
        raise ValueError("Vault or relative source path is missing")
    vault = Path(vault_value).resolve(strict=True)
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("Source must be a vault-relative path")
    source = (vault / relative).resolve(strict=True)
    if not source.is_relative_to(vault) or not source.is_file():
        raise ValueError("Source must be a file inside the vault")
    if source.suffix.lower() not in VAULT_IMAGE_EXTS | {".pdf"}:
        raise ValueError("Unsupported source format")
    return vault, source, source.relative_to(vault).as_posix()


def _digest(source: Path) -> str:
    sha = hashlib.sha256()
    with source.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            sha.update(block)
    return sha.hexdigest()


def _preview(vault: Path, source: Path, rel: str, *, reextract: bool = False,
             allow_large: bool = False, include_visual: bool = False, page_start: int = 1, page_end: int | None = None) -> dict:
    digest = _digest(source)
    cache_kind = "pdf_cache" if source.suffix.lower() == ".pdf" else "image_cache"
    key = vault_key(vault)
    from core.extraction_cache import cache_coverage
    def page_count():
        with fitz.open(source) as doc:
            return len(doc)
    coverage = cache_coverage((OBSIDIAN_CACHE_DIR, OBSIDIAN_INDEX_DIR), cache_kind,
                              key, digest, page_count=page_count)
    cached = coverage in ("whole_unverified", "ranges_unverified")
    if source.suffix.lower() != ".pdf":
        return {"source": rel, "source_sha256": digest, "kind": "image",
                "eligible": 1 if reextract or not cached else 0, "pages": 1,
                "has_cached_text": cached}
    with fitz.open(source) as doc:
        last = len(doc) if page_end is None else page_end
        if type(page_start) is not int or type(last) is not int or not 1 <= page_start <= last <= len(doc):
            raise ValueError("Invalid physical page range")
        if last - page_start + 1 > 1000 and not allow_large:
            raise ValueError("PDF exceeds 1000 pages; select a smaller range with the CLI")
        eligible = []
        ocr_eligible = 0
        for index in range(page_start - 1, last):
            page = doc[index]
            # A nonempty content stream without native text can be an outlined
            # figure. Reading it is cheaper than walking dense vector drawings.
            native = page.get_text('text').strip()
            needs_ocr = len(native) < 40 and (_has_substantial_image(page) or
                        (not native and bool(page.read_contents().strip())))
            ocr_eligible += int(needs_ocr)
            if ((include_visual and _has_visual_figure(page)) or
                    (len(native) < 40 and (_has_substantial_image(page) or
                     (not native and bool(page.read_contents().strip()))))):
                eligible.append(index + 1)
        return {"source": rel, "source_sha256": digest, "kind": "pdf",
                "eligible": len(eligible), "eligible_pages": eligible, "ocr_eligible": ocr_eligible,
                "visual_eligible": len(eligible) - ocr_eligible,
                "pages": len(doc), "page_range": [page_start, last], "has_cached_text": cached}


def _discover_sources(vault: Path) -> list:
    """Discovery only: never open PDF pages or call an extraction provider."""
    cfg = load_config()
    excluded = obsidian_manager._normalised_excluded_dirs(
        vault, cfg.get("vault_exclude_dirs", []))
    configured = cfg.get("vault_image_exts")
    image_exts = frozenset(configured if isinstance(configured, list) else VAULT_IMAGE_EXTS)
    notes: list[Path] = []
    pdfs: list[tuple[Path, str]] = []
    images: dict[str, list[str]] = {}
    failed_prefixes: set[str] = set()
    for source, rel in _iter_vault_scan_files(vault, excluded, failed_prefixes=failed_prefixes):
        ext = source.suffix.lower()
        if ext in VAULT_MD_EXTS:
            notes.append(source)
        elif ext in VAULT_BINARY_EXTS:
            pdfs.append((source, rel))
        elif ext in image_exts:
            images.setdefault(nfc(source.name).lower(), []).append(rel)
    if failed_prefixes:
        raise ValueError("Vault walk was incomplete; session preview refused")
    image_rels = {rel for values in images.values() for rel in values}
    referenced: set[str] = set()
    for note in notes:
        content = note.read_text(encoding="utf-8", errors="replace")
        for rel in obsidian_manager._extract_md_attachments(content, note, vault,
                                                             name_index=images):
            if Path(rel).suffix.lower() in image_exts and rel in image_rels:
                referenced.add(rel)
    sources = pdfs + [(vault / rel, rel) for rel in referenced]
    return sources


def _candidate_page_count(path):
    if path.suffix.lower() != ".pdf":
        return 1
    with fitz.open(path) as document:
        return len(document)


def _session_inventory(vault: Path, *, large_run: bool = False,
                       routine: bool = False, paths: list | None = None,
                       preview_pages: bool = True) -> dict:
    """Preview the same included PDFs and note-referenced images as the indexer."""
    cfg = load_config()
    sources = _discover_sources(vault)
    signatures = obsidian_manager._load_pdf_signature_cache() if routine else {}
    from core.online_runs import known_failed_content, failure_for
    failures = known_failed_content(BASE_DIR, vault_key(vault), all_evidence=True) if routine else []
    decisions = {}
    documents = {}
    reused = []
    candidates = []
    identities = []
    reasons = {}
    if paths is not None and (not isinstance(paths, list) or len(paths) > 500 or
                              any(not isinstance(p, str) for p in paths)):
        raise ValueError("Select at most 500 relative source paths")
    available = {rel for _, rel in sources}
    if paths is not None and not set(paths) <= available:
        raise ValueError("Selection contains excluded, missing or unreferenced sources")
    for path, rel in sorted(sources, key=lambda x: x[1]):
        if not routine and paths is not None and rel not in paths:
            continue
        if routine:
            signature = obsidian_manager._pdf_file_signature(path, signatures, rel)
            digest = signature["sha256"]
            from accession.review import fingerprint
            physical = fingerprint(path)
            documents[rel] = {k: physical[k] for k in ("ino", "birth")}
            decisions[rel] = failure_for(failures, rel, digest, documents[rel])
            identities.append({"source": rel, **signature})
            kind = "pdf_cache" if path.suffix.lower() == ".pdf" else "image_cache"
            from core.extraction_cache import cache_coverage
            def page_count():
                with fitz.open(path) as doc:
                    return len(doc)
            coverage = cache_coverage((OBSIDIAN_CACHE_DIR, OBSIDIAN_INDEX_DIR), kind,
                                      vault_key(vault), digest, page_count=page_count)
            cached = coverage in ("whole_unverified", "ranges_unverified")
            previous = signatures.get(rel, {})
            reasons[rel] = ("selected" if paths is not None and rel in paths else
                            "partial" if coverage == "partial" else
                            "changed" if previous and previous.get("sha256") != digest else "new_or_uncached")
            if cached and not (decisions[rel] and decisions[rel]["reason"] in {"reported_bad", "content_continuity_uncertain"}):
                reused.append({"source": rel, "source_sha256": digest,
                               "provenance": "legacy_or_online_unverified"})
                continue
            if paths is not None and rel not in paths:
                continue
        candidates.append((path, rel))
    source_limit = 100_000 if not preview_pages or (large_run and not routine) else 500
    if len(candidates) > source_limit:
        raise ValueError(f"Selection exceeds {source_limit} sources; choose a smaller selection")
    if routine and preview_pages:
        page_budget = 0
        for path, _ in candidates:
            try:
                page_budget += _candidate_page_count(path)
            except fitz.FileDataError:
                pass  # The per-source preview below records the unreadable PDF.
            if page_budget > 1000:
                raise ValueError("Selected additions exceed 1000 pages/images; choose fewer files or an explicit bounded/advanced run")
    by_source = {row["source"]: row for row in identities}
    rows = []
    for path, rel in candidates:
        if preview_pages:
            try:
                row = _preview(vault, path, rel, reextract=large_run,
                               allow_large=large_run, include_visual=large_run)
            except fitz.FileDataError:
                row = {"source": rel, "source_sha256": _digest(path), "pages": 0,
                       "eligible": 0, "has_cached_text": False, "kind": "pdf",
                       "preview_error": "Unreadable PDF"}
        else:
            row = {"source": rel, "source_sha256": by_source[rel]["sha256"], "pages": 0,
                   "eligible": 0, "has_cached_text": False,
                   "kind": "pdf" if path.suffix.lower() == ".pdf" else "image"}
        rows.append(row)
    for row in rows:
        if not routine:
            from accession.review import fingerprint
            physical = fingerprint(vault / row["source"])
            row["document_identity"] = {k: physical[k] for k in ("ino", "birth")}
        row["selection_reason"] = reasons.get(row["source"], "explicit_reextraction")
        if routine:
            row["document_identity"] = documents[row["source"]]
            if decisions.get(row["source"]):
                row["known_failure"] = decisions[row["source"]]
    if large_run and preview_pages:
        active_profiles = [row["source"] for row in rows
                           if row["kind"] == "pdf" and
                           obsidian_manager._selected_pdf_payload(vault / row["source"], vault)
                           is not None]
        if active_profiles:
            raise ValueError(f"{len(active_profiles)} eligible PDF(s) have active text profiles; "
                             "online cache promotion would be superseded")
    page_limit = 1_000_000 if large_run else 1000
    if sum(row["pages"] for row in rows) > page_limit:
        raise ValueError(f"Session exceeds {page_limit} pages/images; use smaller explicit runs")
    import json
    binding = {"rows": [{k: v for k, v in row.items() if k != "known_failure"} for row in rows], "identities": identities, "routine": routine,
               "paths": sorted(paths) if paths is not None else None,
               "settings": _index_settings(cfg),
               "scope": {k: cfg.get(k) for k in ("vault_exclude_dirs", "vault_image_exts",
                         "online_ocr_provider", "online_ocr_model", "online_vision_provider",
                         "online_vision_model")}}
    token = hashlib.sha256(json.dumps(binding, sort_keys=True).encode()).hexdigest()
    return {"sources": rows, "source_count": len(rows),
            "eligible": sum(row["eligible"] for row in rows
                            if large_run or not row["has_cached_text"]),
            "pages": sum(row["pages"] for row in rows), "session_token": token,
            "reused": reused, "reused_count": len(reused),
            "pending_count": sum("known_failure" in row for row in rows),
            "external_eligible": sum(row["eligible"] for row in rows if not row.get("known_failure")),
            "discovered_count": len(sources), "routine": routine,
            "maximum_attempts": 2 * sum(row["eligible"] for row in rows),
            "index_settings": _index_settings(cfg),
            "selection_counts": {reason: sum(row["selection_reason"] == reason for row in rows)
                                 for reason in ("new_or_uncached", "changed", "partial", "selected", "explicit_reextraction")}}


def _promotion_cache(vault, source, digest):
    root = Path(OBSIDIAN_CACHE_DIR)
    kind = "pdf_cache" if source.suffix.lower() == ".pdf" else "image_cache"
    cache = root / kind / vault_key(vault) / (digest + ".txt")
    if any(path.is_symlink() for path in (root, root / kind, cache.parent, cache)):
        raise ValueError("Symlink cache path refused")
    return cache


def _promotable_text(source: Path, vault: Path, job: dict) -> str:
    """Verify the exact completed attempt; never use partial response text."""
    import json
    from core.page_ocr import _bytes, _sha
    from core.practical_payload import validate

    def read_receipt(path: Path) -> dict:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 64 * 1024**2:
            raise ValueError("Missing, unsafe or oversized OCR receipt")
        return json.loads(path.read_text(encoding="utf-8"))

    directory = (Path(BASE_DIR) / "online_ocr_attempts" / job["recipe"] /
                 job["source_sha256"])
    if source.suffix.lower() == ".pdf":
        snapshot_ids = job.get("snapshots") or [job["snapshot_id"]]
        parts = []
        next_page = 1
        for snapshot_id in snapshot_ids:
            snapshot = read_receipt(directory / ("snapshot-" + snapshot_id + ".json"))
            original = {key: value for key, value in snapshot.items() if key != "snapshot_id"}
            start, end = snapshot["range"]
            if (_sha(_bytes(original)) != snapshot_id or
                    snapshot["coverage"] != "processed_unverified" or
                    start != next_page or end < start or
                    len(snapshot["pages"]) != end - start + 1 or
                    [row["binding"]["page"] for row in snapshot["pages"]]
                    != list(range(start, end + 1)) or
                    any(row["status"] not in ("native", "ocr") for row in snapshot["pages"])):
                raise ValueError("Incomplete or altered PDF extraction cannot be promoted")
            payload = snapshot["payload"]
            if payload["outcome"] != "processed" or payload["source"] != str(source):
                raise ValueError("PDF payload is not a complete match")
            parts.append(validate(payload, source, job["recipe"]))
            next_page = end + 1
        if next_page - 1 != snapshot["page_count"]:
            raise ValueError("PDF range coverage is incomplete")
        text = "\n\n".join(parts)
        if obsidian_manager._selected_pdf_payload(source, vault) is not None:
            raise ValueError("An active PDF text profile supersedes the legacy cache")
    else:
        wrapper = read_receipt(directory / f"image-a{job['attempt']}.json")
        record = wrapper["record"]
        if (wrapper["sha256"] != _sha(json.dumps(record, sort_keys=True,
                                             ensure_ascii=False).encode()) or
                record["status"] != "ocr" or
                record["binding"] != {"source_sha256": job["source_sha256"],
                                      "recipe": job["recipe"], "image": source.name}):
            raise ValueError("Incomplete or altered image extraction cannot be promoted")
        text = record["text"]
    if not text.strip() or _digest(source) != job["source_sha256"]:
        raise ValueError("Empty text or source changed since extraction")
    return text


def _single_preview_token(preview_result):
    import json
    cfg = load_config()
    binding = {"preview": preview_result, "index": _index_settings(cfg),
               "ocr": [cfg.get("online_ocr_provider"), cfg.get("online_ocr_model")]}
    return hashlib.sha256(json.dumps(binding, sort_keys=True).encode()).hexdigest()


@online_ocr_bp.post("/api/online-ocr/preview")
def preview():
    try:
        data = request.get_json(silent=True) or {}
        vault, source, rel = _source(data.get("path"))
        result = _preview(vault, source, rel, page_start=data.get("page_start", 1), page_end=data.get("page_end"))
        cfg = load_config()
        result["reextract"] = data.get("reextract") is True
        result.update(provider=data.get("provider") or cfg.get("online_ocr_provider"),
                      model=data.get("model") or cfg.get("online_ocr_model"))
        result["preview_token"] = _single_preview_token(result)
        return jsonify(result)
    except (OSError, ValueError, TypeError, fitz.FileDataError) as exc:
        return jsonify({"error": str(exc)}), 400


@online_ocr_bp.get("/api/online-ocr/sources")
def source_picker():
    try:
        vault = Path(load_config()["obsidian_vault_path"]).resolve(strict=True)
        offset = max(0, int(request.args.get("offset", 0)))
        query = request.args.get("q", "").casefold()[:200]
        sources = sorted(rel for _, rel in _discover_sources(vault) if query in rel.casefold())
        return jsonify({"sources": sources[offset:offset + 50], "total": len(sources),
                        "next_offset": offset + 50 if offset + 50 < len(sources) else None})
    except (OSError, ValueError, KeyError):
        return jsonify({"error": "Source inventory unavailable"}), 409


def _application_version():
    """Record available build provenance, never configuration or credentials."""
    import plistlib
    import subprocess
    import sys
    if getattr(sys, "frozen", False):
        try:
            info = Path(sys.executable).parent.parent / "Info.plist"
            with info.open("rb") as stream:
                metadata = plistlib.load(stream)
            return str(metadata.get("CFBundleShortVersionString") or metadata.get("CFBundleVersion") or "unknown-bundle-version")[:128]
        except (OSError, ValueError):
            return "unknown-bundle-version"
    try:
        result = subprocess.run(["git", "describe", "--always", "--dirty", "--abbrev=40"],
                                cwd=Path(__file__).resolve().parents[2], capture_output=True,
                                text=True, timeout=2, check=True)
        value = result.stdout.strip()
        return "git:" + value[:128] if value else "unknown-source-version"
    except (OSError, subprocess.SubprocessError):
        return "unknown-source-version"


def _session_intent(data):
    """Legacy callers retain their boolean contract; intent-v1 previews cannot downgrade."""
    mode = data.get("operation_mode")
    if mode is None:
        if str(data.get("session_token", "")).startswith("intent-v1:"):
            raise ValueError("Operation mode is required for this preview")
        return None
    if mode not in ("prepare_text", "update_search"):
        raise ValueError("Invalid operation mode")
    if data.get("run_index") is not True or data.get("full_rebuild") is True:
        raise ValueError("Daily operation mode requires an incremental session")
    if "prepare_only" in data and data["prepare_only"] is not (mode == "prepare_text"):
        raise ValueError("Conflicting operation intent")
    action = data.get("initiating_action")
    allowed = ("library_review", "targeted_retry") if mode == "prepare_text" else ("search_update", "search_only", "final_search_update")
    if action is not None and action not in allowed:
        raise ValueError("Initiating action does not match operation mode")
    return mode


def _intent_token(token, mode, action=None):
    if mode is None:
        return token
    action = action or ("library_review" if mode == "prepare_text" else "search_update")
    return "intent-v1:" + hashlib.sha256((mode + ":" + action + ":" + token).encode()).hexdigest()


def _receipt_prepare_only(run):
    """Do not recover through a contradictory or unrecognized durable intent."""
    mode = run.get("operation_mode")
    if str(run.get("session_token", "")).startswith("intent-v1:") and mode not in ("prepare_text", "update_search"):
        raise ValueError("Explicit receipt operation mode is missing or invalid")
    if mode not in (None, "prepare_text", "update_search", "extract_text"):
        raise ValueError("Invalid receipt operation mode")
    if mode in ("prepare_text", "update_search") and "prepare_only" in run:
        if run["prepare_only"] is not (mode == "prepare_text"):
            raise ValueError("Conflicting receipt operation intent")
    return mode == "prepare_text" or run.get("prepare_only") is True


@online_ocr_bp.post("/api/online-ocr/session/preview")
def session_preview():
    try:
        cfg = load_config()
        vault_value = cfg.get("obsidian_vault_path")
        if not vault_value:
            raise ValueError("Vault path is not configured")
        data = request.get_json(silent=True) or {}
        mode = _session_intent(data)
        large_run = data.get("run_index") is True
        inventory = _session_inventory(Path(vault_value).resolve(strict=True),
                                       large_run=large_run,
                                       routine=large_run and not data.get("reextract", False),
                                       paths=data.get("paths"))
        inventory["session_token"] = _intent_token(inventory["session_token"], mode, data.get("initiating_action"))
        inventory["operation_mode"] = mode
        inventory.pop("reused", None)
        inventory["sources_truncated"] = len(inventory["sources"]) > 100
        inventory["sources"] = inventory["sources"][:100]
        return jsonify(inventory)
    except (OSError, ValueError, TypeError, fitz.FileDataError) as exc:
        return jsonify({"error": str(exc)}), 400


@online_ocr_bp.post("/api/online-ocr/session/start")
def session_start():
    data = request.get_json(silent=True) or {}
    native_only = data.get("no_external") is True
    if data.get("confirm_external") is not True and not native_only:
        return jsonify({"error": "Confirm external image transmission for this session"}), 400
    try:
        cfg = load_config()
        vault_value = cfg.get("obsidian_vault_path")
        if not vault_value:
            raise ValueError("Vault path is not configured")
        vault = Path(vault_value).resolve(strict=True)
        run_index = data.get("run_index") is True
        full_rebuild = data.get("full_rebuild") is True
        mode = _session_intent(data)
        prepare_only = mode == "prepare_text" if mode else data.get("prepare_only") is True
        if prepare_only and (not run_index or full_rebuild):
            raise ValueError("Text preparation requires an incremental session")
        allow_initial_pressure = data.get("allow_initial_memory_pressure") is True
        if allow_initial_pressure and not (run_index and full_rebuild):
            raise ValueError("Memory-pressure exception requires a confirmed full online rebuild")
        if full_rebuild and (not run_index or data.get("confirm_full_rebuild") is not True):
            raise ValueError("Full rebuild requires a confirmed online index run")
        if mode == "prepare_text" and data.get("confirm_index") is True:
            raise ValueError("Preparation must not authorize indexing")
        if run_index and not (mode == "prepare_text") and data.get("confirm_index") is not True:
            raise ValueError("Confirm the online extraction and subsequent index update")
        routine = run_index and data.get("reextract") is not True
        inventory = _session_inventory(vault, large_run=run_index, routine=routine,
                                       paths=data.get("paths"))
        reviewed_token = _intent_token(inventory["session_token"], mode, data.get("initiating_action"))
        if data.get("session_token") != reviewed_token:
            raise ValueError("Vault changed since session preview")
        if routine and not native_only:
            for field, config_key in (("provider", "online_ocr_provider"), ("model", "online_ocr_model"),
                                      ("vision_provider", "online_vision_provider"), ("vision_model", "online_vision_model")):
                if data.get(field) and data[field] != cfg.get(config_key):
                    raise ValueError("Provider settings changed since preview; save settings and preview again")
        external_needed = any(row["eligible"] and
                              (not row.get("known_failure") or data.get("retry_failed") is True)
                              for row in inventory["sources"])
        if native_only or not external_needed:
            native_only = True
            engine = vision_engine = NativeOnlyOCR()
        else:
            engine = OnlineOCR(str(data.get("provider") or cfg.get("online_ocr_provider", "")),
                               str(data.get("model") or cfg.get("online_ocr_model", "")))
            engine.identity()
            vision_engine = OnlineOCR(
                str(data.get("vision_provider") or cfg.get("online_vision_provider", engine.provider)),
                str(data.get("vision_model") or cfg.get("online_vision_model", engine.model)), task="vision")
            if run_index:
                vision_engine.identity()
        _preflight_index_settings(_index_settings(cfg), force_fresh=full_rebuild)
        if not run_index and not any(row["eligible"] and not row["has_cached_text"]
                                     for row in inventory["sources"]):
            raise ValueError("No uncached page or image needs OCR")
    except (OSError, ValueError, TypeError, fitz.FileDataError) as exc:
        return jsonify({"error": str(exc)}), 400
    if not run_index:
        inventory["sources"] = [row for row in inventory["sources"] if row["eligible"] and not row["has_cached_text"]]
        inventory["source_count"] = len(inventory["sources"])
    with _lock:
        if _job.get("state") in ("running", "promoting", "indexing"):
            return jsonify({"error": "An OCR or index job is running"}), 409
        if obsidian_manager.get_status() in ("running", "scanning", "embedding"):
            return jsonify({"error": "Indexing is active; retry when it finishes"}), 409
        epoch = obsidian_manager.try_acquire_lock(ttl=3600)
        if not epoch:
            return jsonify({"error": "Another vault operation is active"}), 409
        run_directory = None
        resumed = {}
        receipt_names = {}
        import json
        from uuid import uuid4
        from core.cache_exports import _directory, _publish_new

        resume_id = data.get("resume_run_id")
        if resume_id is not None and (not isinstance(resume_id, str) or
                                       re.fullmatch(r"[0-9a-f]{32}", resume_id) is None):
            obsidian_manager.release_lock(epoch)
            return jsonify({"error": "Invalid online run ID"}), 400
        run_directory = Path(BASE_DIR) / "online_index_runs" / (resume_id or uuid4().hex)
        attempt_generation = uuid4().hex
        receipt = {"schema": 2, "kind": "session" if run_index else "extraction_session", "vault_key": vault_key(vault),
                   "session_token": reviewed_token,
                   "source_count": inventory["source_count"],
                   "eligible": inventory["eligible"],
                   "ocr": engine.identity(), "vision": vision_engine.identity(),
                   "index_settings": _index_settings(cfg),
                   "attempt_generation": attempt_generation,
                   "full_rebuild": full_rebuild,
                   "allow_initial_memory_pressure": allow_initial_pressure,
                   "retry_failed": data.get("retry_failed") is True,
                   "no_external": native_only,
                   "prepare_only": prepare_only,
                   "routine": routine, "paths": data.get("paths"),
                   "reused_count": inventory.get("reused_count", 0)}
        # Fixed action vocabulary; never persist arbitrary request contents.
        receipt.update(operation_mode=mode or ("prepare_text" if prepare_only else "update_search" if run_index else "extract_text"),
                       initiating_action=(data.get("initiating_action") or ("library_review" if mode == "prepare_text" else "search_update")) if mode else "legacy_session",
                       application_version=_application_version())
        try:
            if len(json.dumps(receipt).encode()) > 65536:
                raise ValueError("Selection metadata exceeds the durable run bound; choose fewer paths")
            if resume_id:
                run_file = run_directory / "run.json"
                if (run_directory.is_symlink() or run_directory.parent.is_symlink() or
                        run_file.is_symlink() or not run_file.is_file() or
                        run_file.stat().st_size > 64 * 1024 or
                        (run_directory / "promotion-started.json").exists()):
                    raise ValueError("Run receipt is missing or promotion already started")
                previous = json.loads(run_file.read_text(encoding="utf-8"))
                expected_receipt = {**receipt,
                                    "attempt_generation": previous.get("attempt_generation"),
                                    "retry_failed": previous.get("retry_failed")}
                for field in ("operation_mode", "initiating_action", "application_version"):
                    if field not in previous:
                        expected_receipt.pop(field, None)
                    else:
                        expected_receipt[field] = previous[field]
                if previous.get("operation_mode") == "prepare_text" and not prepare_only:
                    raise ValueError("Preparation receipt cannot authorize indexing")
                for optional in ("no_external", "prepare_only"):
                    if optional not in previous and not expected_receipt.get(optional):
                        expected_receipt.pop(optional, None)
                if previous != expected_receipt:
                    raise ValueError("Run identity or inventory changed; resume refused")
                attempt_generation = previous["attempt_generation"]
                if data.get("retry_failed") is not True:
                    raise ValueError("Resume requires explicit failed-attempt retry")
                for index, row in enumerate(inventory["sources"]):
                    prefix = f"source-{index:06d}"
                    candidates = [run_directory / f"{prefix}.json"]
                    candidates.extend(sorted(run_directory.glob(f"{prefix}-r[0-9][0-9][0-9].json")))
                    existing = [path for path in candidates if path.exists()]
                    if not existing:
                        receipt_names[index] = f"{prefix}.json"
                        continue
                    latest = existing[-1]
                    if latest.is_symlink() or latest.stat().st_size > 64 * 1024:
                        raise ValueError("Unsafe source receipt; resume refused")
                    outcome = json.loads(latest.read_text(encoding="utf-8"))
                    if outcome.get("source") != row["source"]:
                        raise ValueError("Source receipt identity mismatch")
                    if outcome.get("status") in ("processed_unverified", "ocr"):
                        if outcome.get("source_sha256") != row["source_sha256"]:
                            raise ValueError("Completed source hash mismatch")
                        resumed[index] = outcome
                    else:
                        retry_number = len(existing)
                        if retry_number > 999:
                            raise ValueError("Source receipt retry limit reached")
                        receipt_names[index] = f"{prefix}-r{retry_number:03d}.json"
            else:
                with _directory(run_directory, create=True) as fd:
                    _publish_new(fd, "run.json", json.dumps(receipt, sort_keys=True).encode())
                    _publish_new(fd, "inventory.json", json.dumps({"sources": inventory["sources"],
                                 "reused": inventory.get("reused", [])}, sort_keys=True).encode())
        except (OSError, ValueError) as exc:
            obsidian_manager.release_lock(epoch)
            return jsonify({"error": str(exc)}), 409
        _cancel.clear()
        _job.clear()
        _job["index_run_id"] = obsidian_manager.get_progress_snapshot().get("run_id")
        _job.update({"state": "running", "scope": "vault_session",
                     "run_id": run_directory.name, "started_at": time.time(),
                     "last_activity_at": time.time(), "prepare_only": prepare_only,
                     "provider": engine.provider, "model": engine.model,
                     "vision_provider": vision_engine.provider,
                     "vision_model": vision_engine.model,
                     "run_index": run_index,
                     "full_rebuild": full_rebuild,
                     "attempt_generation": attempt_generation,
                     "index_settings": _index_settings(cfg),
                     "run_receipt": str(run_directory / "run.json") if run_directory else None,
                     "expected_sources": [row["source"] for row in inventory["sources"]
                                         if run_index or
                                         (row["eligible"] and not row["has_cached_text"])],
                     "source_count": inventory["source_count"],
                     "results": [resumed[index] for index in sorted(resumed)],
                     "extraction_manifest": {"approved": {
                         row["source"]: row["source_sha256"] for row in
                         inventory["sources"] + inventory.get("reused", [])}}})

    def worker():
        import json
        from core.cache_exports import _directory, _publish_new

        def heartbeat():
            obsidian_manager._heartbeat_or_stop(epoch)
            with _lock:
                _job["last_activity_at"] = time.time()

        deadline = time.monotonic() + (24 * 3600 if run_index else 3600)
        try:
            storage = Path(BASE_DIR) / "online_ocr_attempts"
            exports = Path(BASE_DIR) / "online_ocr_exports"
            from core.ocr_resources import MemoryGuard
            # One baseline for the whole batch: per-document/range resets hide
            # cumulative swap growth and repeatedly sample initial pressure.
            memory_guard = MemoryGuard(allow_initial_pressure=allow_initial_pressure)
            for source_index, row in enumerate(inventory["sources"]):
                if _cancel.is_set() or time.monotonic() >= deadline:
                    break
                if source_index in resumed:
                    continue
                if not run_index and (row["has_cached_text"] or row["eligible"] == 0):
                    continue
                rel = row["source"]
                source = vault / rel
                with _lock:
                    _job.update(current_source=rel, last_activity_at=time.time())
                if _digest(source) != row["source_sha256"]:
                    raise ValueError("Source changed after preview")
                if row.get("known_failure") and data.get("retry_failed") is not True:
                    outcome = {"source": rel, "source_sha256": row["source_sha256"],
                               "status": "deferred_known_failure", "evidence": row["known_failure"]}
                elif row.get("preview_error"):
                    outcome = {"source": rel, "source_sha256": row["source_sha256"],
                               "status": "source_failed", "error": row["preview_error"]}
                elif native_only and row.get("ocr_eligible", row["eligible"]):
                    outcome = {"source": rel, "source_sha256": row["source_sha256"],
                               "status": "deferred_no_external"}
                elif _digest(source) != row["source_sha256"]:
                    outcome = {"source": rel, "status": "source_changed",
                               "error_detail": "The source file changed after the preview.",
                               "next_step": "Review the changed file and prepare a new update."}
                else:
                    try:
                        if row["kind"] == "pdf":
                            ranges = (range(0, row["pages"], _ONLINE_INDEX_RANGE_PAGES)
                                      if run_index else (0,))
                            results = []
                            outputs = []
                            for first in ranges:
                                last = min(first + _ONLINE_INDEX_RANGE_PAGES, row["pages"])
                                result = extract_pages(
                                    source, vault, storage, engine,
                                    start_page=first,
                                    end_page=last if run_index else None,
                                    accept_mixed_native=True,
                                    visual_engine=vision_engine if run_index and not native_only else None,
                                    attempt_generation=attempt_generation,
                                    retry_pages=(frozenset(range(first + 1, last + 1))
                                                 if data.get("retry_failed") is True
                                                 else frozenset()),
                                    cancelled=lambda: _cancel.is_set() or time.monotonic() >= deadline,
                                    heartbeat=heartbeat,
                                    resource_guard=memory_guard)
                                results.append(result)
                                outputs.append(str(export_pages(result, exports, vault)))
                                if result["coverage"] != "processed_unverified":
                                    break
                            outcome = {"source": rel,
                                       "status": ("processed_unverified" if all(
                                           part["coverage"] == "processed_unverified" for part in results)
                                           and results[-1]["range"][1] == row["pages"] else "partial"),
                                       "export": outputs[-1],
                                       "snapshot_id": results[-1]["snapshot_id"],
                                       "snapshots": [part["snapshot_id"] for part in results],
                                       "recipe": results[0]["payload"]["recipe"],
                                       "source_sha256": row["source_sha256"],
                                       "page_statuses": sorted({page["status"] for part in results for page in part.get("pages", [])}),
                                       "warnings": sorted({part.get("warning", "Scientific accuracy is unverified.") for part in results}),
                                       "warning_pages": sum(bool(page.get("warning")) for part in results for page in part.get("pages", []))}
                            from core.ocr_resources import extraction_stop_diagnostic
                            stop = next((part.get("stop_reason") for part in results if part.get("stop_reason")), "")
                            outcome.update(extraction_stop_diagnostic(stop))
                            failures = [page for part in results for page in part.get("pages", [])
                                        if page.get("error_detail")]
                            if failures and (not stop or stop in {"engine_unavailable", "two_consecutive_ocr_failures"}):
                                failure = next((page for page in failures if page.get("failure_scope") == "global"), failures[0])
                                outcome.update({key: failure[key] for key in ERROR_FIELDS if key in failure})
                        else:
                            obsidian_manager._heartbeat_or_stop(epoch)
                            result = extract_image(
                                source, vault, storage,
                                vision_engine if run_index else engine,
                                reextract=run_index,
                                attempt_generation=attempt_generation,
                                retry=data.get("retry_failed") is True,
                                cancelled=lambda: _cancel.is_set() or time.monotonic() >= deadline)
                            output = export_image(result, source, vault, exports)
                            outcome = {"source": rel, "status": result["status"],
                                       "export": str(output),
                                       "recipe": result["binding"]["recipe"],
                                       "attempt": result["attempt"],
                                       "source_sha256": row["source_sha256"],
                                       **{key: result[key] for key in ERROR_FIELDS if key in result}}
                    except Exception as exc:
                        outcome = {"source": rel, "status": "failed",
                                   "source_sha256": row["source_sha256"],
                                   "error": type(exc).__name__,
                                   "error_detail": sanitise_error_msg(exc)[:2000],
                                   "next_step": "Review this error before preparing a new update; no search update was started."}
                from core.online_runs import source_exception
                if outcome.get("status") not in {"processed_unverified", "ocr", "deferred_known_failure", "deferred_no_external"} and not source_exception(outcome):
                    outcome["failure_scope"] = "global"
                if row.get("document_identity"):
                    outcome["document_identity"] = row["document_identity"]
                outcome["recorded_at_ns"] = time.time_ns()
                # A resumed run retains its immutable original consent record.
                # Bind this attempt's explicit retry to its new source receipt.
                outcome["retry_authorized"] = data.get("retry_failed") is True
                if run_directory is not None:
                    with _directory(run_directory) as fd:
                        _publish_new(fd, receipt_names.get(
                            source_index, f"source-{source_index:06d}.json"),
                                     json.dumps(outcome, sort_keys=True).encode())
                with _lock:
                    _job["results"].append(outcome)
                    _job["last_activity_at"] = time.time()
                if outcome.get("failure_scope") == "global":
                    with _lock:
                        _job["global_failure"] = True
                        _job["failure"] = {key: outcome[key] for key in ("source", "status", "error", *ERROR_FIELDS) if key in outcome}
                        _job["unfinished_sources"] = sorted(set(_job["expected_sources"]) - {
                            item["source"] for item in _job["results"]
                            if item.get("status") in ("processed_unverified", "ocr")})
                    break
            with _lock:
                _job["state"] = "cancelled" if _cancel.is_set() else (
                    "time_limit" if time.monotonic() >= deadline else
                    "extraction_incomplete" if _job.get("global_failure") else "done")
            if run_index and _job["state"] == "done":
                expected = set(_job["expected_sources"])
                completed = {row.get("source") for row in _job["results"]
                             if row.get("status") in ("processed_unverified", "ocr")}
                from core.online_runs import source_exception
                deferred = {row["source"] for row in _job["results"]
                            if row.get("status") in ("deferred_known_failure", "deferred_no_external")
                            or (routine and source_exception(row))}
                if completed | deferred != expected:
                    with _lock:
                        _job["state"] = "extraction_incomplete"
                        _job["unfinished_sources"] = sorted(expected - completed)
                else:
                    fresh_inventory = _session_inventory(vault, large_run=True, routine=routine,
                                                         paths=data.get("paths"))
                    if fresh_inventory["session_token"] != inventory["session_token"]:
                        raise ValueError("Vault inventory changed during online extraction; "
                                         "cache promotion and indexing refused")
                    selected = []
                    for row in _job["results"]:
                        if row["source"] in deferred:
                            continue
                        source = vault / row["source"]
                        selected.append((vault, source, row["source"], row,
                                         _promotable_text(source, vault, row)))
                    _job["extraction_manifest"]["deferred"] = sorted(deferred)
                    for rel in deferred:
                        _job["extraction_manifest"]["approved"].pop(rel, None)
                    with _lock:
                        _job["state"] = "promoting"
                    with _directory(run_directory) as fd:
                        _publish_new(fd, "promotion-started.json",
                                     json.dumps({"session_token": inventory["session_token"]},
                                                sort_keys=True).encode())
                    _promote_session_selected(selected, epoch, run_index=True,
                                              settings=_job["index_settings"],
                                              full_rebuild=full_rebuild, prepare_only=prepare_only)
        except Exception as exc:
            with _lock:
                detail = sanitise_error_msg(exc)[:2000] or type(exc).__name__
                _job.update({"state": "failed", "error": type(exc).__name__, "error_detail": detail,
                             "failure": {"source": _job.get("current_source"), "status": "failed",
                                         "error_detail": detail, "failure_scope": "global",
                                         "next_step": "Review the saved outcomes before retrying. An interrupted publication may need recovery."}})
        finally:
            with _lock:
                _job["finished_at"] = time.time()
                _job["last_activity_at"] = _job["finished_at"]
            try:
                if run_directory is not None:
                    from core.online_runs import checkpoint
                    checkpoint(run_directory, _job.get("state", "interrupted"), **_checkpoint_details())
            finally:
                obsidian_manager.release_lock(epoch)

    try:
        thread = threading.Thread(target=worker, daemon=True, name="online-ocr-session")
        obsidian_manager.register_index_thread(thread)
        thread.start()
    except Exception:
        obsidian_manager.release_lock(epoch)
        with _lock:
            _job.update({"state": "failed", "error": "Session worker failed to start"})
        raise
    return jsonify({"ok": True, "run_id": run_directory.name if run_directory else None})


@online_ocr_bp.post("/api/online-ocr/start")
def start():
    data = request.get_json(silent=True) or {}
    if data.get("confirm_external") is not True:
        return jsonify({"error": "Confirm external image transmission for this run"}), 400
    try:
        vault, source, rel = _source(data.get("path"))
        expected = data.get("source_sha256")
        if expected != _digest(source):
            raise ValueError("Source changed since preview")
        settings = _index_settings(load_config())
        _preflight_index_settings(settings)
        engine = OnlineOCR(str(data.get("provider", "")), str(data.get("model", "")))
        engine.identity()
        if source.suffix.lower() == ".pdf" and not isinstance(data.get("reextract"), bool):
            raise ValueError("PDF re-extraction choice is required")
        if source.suffix.lower() != ".pdf" and not isinstance(data.get("reextract"), bool):
            raise ValueError("Image re-extraction choice is required")
        retry_page = data.get("retry_page")
        if retry_page is not None and (type(retry_page) is not int or retry_page < 1):
            raise ValueError("Retry page must be a positive physical page number")
        preview_result = _preview(vault, source, rel, page_start=data.get("page_start", 1), page_end=data.get("page_end"))
        preview_result.update(provider=engine.provider, model=engine.model, reextract=data["reextract"])
        if data.get("preview_token") != _single_preview_token(preview_result):
            raise ValueError("Source, range or settings changed; preview again")
        if preview_result["has_cached_text"] and not data["reextract"]:
            raise ValueError("Existing cached text retained; select re-extraction explicitly")
        if not preview_result["eligible"] and not data["reextract"]:
            raise ValueError("No image requires OCR")
    except (OSError, ValueError, TypeError, fitz.FileDataError) as exc:
        return jsonify({"error": str(exc)}), 400
    with _lock:
        if _job.get("state") in ("running", "promoting", "indexing"):
            return jsonify({"error": "An online OCR job is running"}), 409
        if obsidian_manager.get_status() in ("running", "scanning", "embedding"):
            return jsonify({"error": "Indexing is active; retry when it finishes"}), 409
        epoch = obsidian_manager.try_acquire_lock(ttl=3600)
        if not epoch:
            return jsonify({"error": "Another vault operation is active"}), 409
        from uuid import uuid4
        previous_generation = (
            _job.get("attempt_generation") if (retry_page or data.get("retry")) and
            _job.get("source") == rel and _job.get("source_sha256") == expected and
            _job.get("provider") == engine.provider and _job.get("model") == engine.model
            else None)
        attempt_generation = previous_generation or (uuid4().hex if data["reextract"] else None)
        from core.online_runs import create_file_run
        record = {"schema": 2, "kind": "file", "vault_key": vault_key(vault),
                  "source": rel, "source_sha256": expected, "source_count": 1,
                  "page_range": preview_result.get("page_range"), "preview_token": data["preview_token"],
                  "reextract": data["reextract"], "index_settings": settings,
                  "ocr": engine.identity(), "attempt_generation": attempt_generation}
        try:
            file_directory, attempt_generation = create_file_run(BASE_DIR, record, data.get("resume_run_id"))
        except (OSError, ValueError, TypeError) as exc:
            obsidian_manager.release_lock(epoch)
            return jsonify({"error": str(exc)}), 409
        _cancel.clear()
        _job.clear()
        _job["index_run_id"] = obsidian_manager.get_progress_snapshot().get("run_id")
        _job.update({"state": "running", "run_id": file_directory.name, "source": rel, "provider": engine.provider,
                     "model": engine.model, "source_sha256": expected,
                     "attempt_generation": attempt_generation, "index_settings": settings,
                     "run_receipt": str(file_directory / "run.json")})

    def worker():
        try:
            storage = Path(BASE_DIR) / "online_ocr_attempts"
            if source.suffix.lower() == ".pdf":
                result = extract_pages(source, vault, storage, engine,
                                       start_page=preview_result["page_range"][0] - 1,
                                       end_page=preview_result["page_range"][1],
                                       cancelled=_cancel.is_set,
                                       accept_mixed_native=True,
                                       attempt_generation=attempt_generation,
                                       retry_pages=(frozenset({retry_page}) if retry_page else
                                                    frozenset(range(preview_result["page_range"][0], preview_result["page_range"][1] + 1))
                                                    if data.get("resume_run_id") else frozenset()))
                exported = export_pages(result, Path(BASE_DIR) / "online_ocr_exports", vault)
                summary = {"coverage": result["coverage"], "export": str(exported),
                           "snapshot_id": result["snapshot_id"],
                           "recipe": result["payload"]["recipe"],
                           "pages": [{"page": r["binding"]["page"],
                                      "status": r["status"]} for r in result["pages"]]}
                from core.ocr_resources import extraction_stop_diagnostic
                stop = result.get("stop_reason", "")
                summary.update(extraction_stop_diagnostic(stop))
                failures = [page for page in result["pages"] if page.get("error_detail")]
                if failures and (not stop or stop in {"engine_unavailable", "two_consecutive_ocr_failures"}):
                    failure = next((page for page in failures if page.get("failure_scope") == "global"), failures[0])
                    summary.update({key: failure[key] for key in ERROR_FIELDS if key in failure})
            else:
                result = extract_image(source, vault, storage, engine,
                                       retry=bool(data.get("retry") or data.get("resume_run_id")),
                                       attempt_generation=attempt_generation,
                                       reextract=data["reextract"], cancelled=_cancel.is_set)
                summary = {"coverage": result["coverage"], "status": result["status"]}
                if "binding" in result:
                    summary["recipe"] = result["binding"]["recipe"]
                    summary["attempt"] = result["attempt"]
                if "binding" in result:
                    exported = export_image(result, source, vault,
                                            Path(BASE_DIR) / "online_ocr_exports")
                    summary["export"] = str(exported)
            with _lock:
                _job.update({"state": "extraction_incomplete" if summary.get("failure_scope") == "global" else "done", **summary})
        except Exception as exc:
            with _lock:
                _job.update({"state": "failed", "error": type(exc).__name__,
                             "error_detail": sanitise_error_msg(exc)[:2000],
                             "next_step": "Inspect the error before explicitly retrying; no index update was started."})
        finally:
            from core.online_runs import save_file_outcome, checkpoint
            try:
                with _lock:
                    outcome = {k: v for k, v in _job.items() if k != "run_receipt"}
                    outcome["status"] = outcome.get("coverage", outcome.get("state"))
                save_file_outcome(file_directory, outcome)
                checkpoint(file_directory, outcome["state"])
            finally:
                obsidian_manager.release_lock(epoch)

    try:
        thread = threading.Thread(target=worker, daemon=True, name="online-ocr")
        obsidian_manager.register_index_thread(thread)
        thread.start()
    except Exception:
        with _lock:
            _job.update({"state": "failed", "error": "Worker failed to start"})
        obsidian_manager.release_lock(epoch)
        raise
    return jsonify({"ok": True, "run_id": file_directory.name})


@online_ocr_bp.get("/api/online-ocr/status")
def status():
    with _lock:
        snapshot = {key: _job[key] for key in (
            "run_id", "started_at", "finished_at", "last_activity_at", "current_source", "prepare_only",
            "state", "source", "provider", "model", "vision_provider", "vision_model",
            "scope", "run_index", "full_rebuild", "source_count", "coverage", "status",
            "export", "error", "stop_reason", "next_step", "failure_scope", "error_detail", "global_failure", "failure", "promotion_receipt", "deferred_count", "deferred_content"
        ) if key in _job}
        snapshot["promoted_count"] = len(_job.get("promoted_sources", []))
        snapshot["pages"] = _job.get("pages", [])[-50:]
        snapshot["page_count"] = len(_job.get("pages", []))
        if "results" in _job or snapshot.get("run_index"):
            rows = _job.get("results", [])
            snapshot["expected_count"] = len(_job.get("expected_sources", []))
            snapshot["result_count"] = len(rows) or int(bool(snapshot.get("coverage")))
            snapshot["recent_results"] = [{key: row[key] for key in ("source", "status", "error", *ERROR_FIELDS) if key in row}
                                          for row in rows[-5:]]
            from core.online_runs import source_exception
            snapshot["failed_count"] = sum(source_exception(row) or row.get("failure_scope") == "global" for row in rows)
            snapshot["not_attempted_count"] = max(0, snapshot["expected_count"] - len(rows))
            snapshot["deferred_count"] = snapshot.get("deferred_count", sum(
                row.get("status") in ("deferred_known_failure", "deferred_no_external") or source_exception(row) for row in rows))
            snapshot["source_errors"] = [{key: row[key] for key in ("source", "status", "error", *ERROR_FIELDS) if key in row}
                                         for row in rows if row.get("error_detail") or row.get("failure_scope") == "global"][-20:]
            snapshot["completed_count"] = sum(row.get("status") in ("processed_unverified", "ocr") for row in rows)
            unfinished = _job.get("unfinished_sources", [])
            snapshot["unfinished_count"] = len(unfinished)
        if snapshot.get("state") == "indexing":
            snapshot["index_progress"] = obsidian_manager.get_progress_snapshot()
        if snapshot.get("state") in ("indexed", "completed_with_omissions"):
            snapshot["bm25_status"] = obsidian_manager.get_postindex_bm25_status()
        # A standalone/local index can own the shared operation lock while
        # this subsystem retains an older terminal receipt.
        if snapshot.get("state") not in ("running", "promoting", "indexing"):
            progress = obsidian_manager.get_progress_snapshot()
            local_state = obsidian_manager.get_status()
            local_id = progress.get("run_id")
            if (local_state in ("running", "scanning", "embedding") or
                    local_id and local_id != _job.get("index_run_id")):
                state = {"done": "indexed", "error": "failed", "idle": "idle",
                         "paused": "index_incomplete", "paused_partial": "index_incomplete",
                         "paused_scan": "index_incomplete", "cancelled": "cancelled"}.get(local_state, "indexing")
                snapshot = {"state": state, "run_id": "local-" + (local_id or "index"),
                            "run_index": True, "index_progress": progress,
                            "error_detail": progress.get("error_detail", ""),
                            "bm25_status": obsidian_manager.get_postindex_bm25_status()}
        return jsonify(snapshot)


def _send_export(value):
    root = Path(BASE_DIR) / "online_ocr_exports"
    path = Path(value)
    if root.is_symlink() or path.is_symlink() or not path.resolve(strict=True).is_relative_to(root.resolve(strict=True)):
        raise ValueError("Export is outside the approved export directory")
    if not path.is_file():
        raise ValueError("Export is unavailable")
    return send_file(path, as_attachment=True, mimetype="text/plain")


@online_ocr_bp.get("/api/online-ocr/export/current")
def current_export():
    try:
        with _lock:
            value = _job.get("export")
        return _send_export(value)
    except (ValueError, OSError, TypeError):
        return jsonify({"error": "Export is unavailable"}), 404


@online_ocr_bp.get("/api/online-ocr/runs/<run_id>/export/<int:source_index>")
def run_export(run_id, source_index):
    from core.online_runs import read_record, run_directory
    try:
        directory = run_directory(BASE_DIR, run_id)
        run = read_record(directory / "run.json")
        vault = Path(load_config()["obsidian_vault_path"]).resolve(strict=True)
        if run.get("vault_key") != vault_key(vault) or not 0 <= source_index < run["source_count"]:
            raise ValueError("Run binding mismatch")
        prefix = f"source-{source_index:06d}"
        candidates = [directory / f"{prefix}.json"] + sorted(directory.glob(f"{prefix}-r[0-9][0-9][0-9].json"))
        existing = [path for path in candidates if path.exists()]
        return _send_export(read_record(existing[-1])["export"])
    except (ValueError, OSError, TypeError, KeyError, IndexError):
        return jsonify({"error": "Export is unavailable for this run"}), 404


@online_ocr_bp.get("/api/online-ocr/runs")
def runs():
    from core.online_runs import inspect_run
    try:
        offset = max(0, int(request.args.get("offset", 0)))
        limit = min(50, max(1, int(request.args.get("limit", 20))))
        vault = Path(load_config()["obsidian_vault_path"]).resolve(strict=True)
        root = Path(BASE_DIR) / "online_index_runs"
        if root.is_symlink():
            raise ValueError("Unsafe run root")
        ids = [name for _, name in sorted(((p.stat().st_mtime_ns, p.name) for p in root.iterdir()
               if re.fullmatch(r"[0-9a-f]{32}", p.name) and p.is_dir() and not p.is_symlink()), reverse=True)] if root.exists() else []
        result = []
        for run_id in ids[offset:offset + limit]:
            try:
                summary = inspect_run(BASE_DIR, run_id, vault_key(vault), limit=0)
                with _lock:
                    live_id = Path(_job["run_receipt"]).parent.name if _job.get("run_receipt") else None
                    summary["worker_state"] = _job.get("state") if live_id == run_id else None
                    if summary["worker_state"] in ("running", "promoting", "indexing"):
                        summary["retry_allowed"] = False
                result.append(summary)
            except (ValueError, OSError, TypeError):
                continue
        return jsonify({"runs": result, "next_offset": offset + limit if offset + limit < len(ids) else None})
    except (ValueError, OSError, KeyError):
        return jsonify({"error": "Run history unavailable for the configured vault"}), 409


@online_ocr_bp.get("/api/online-ocr/runs/<run_id>")
def run_detail(run_id):
    from core.online_runs import inspect_run
    try:
        vault = Path(load_config()["obsidian_vault_path"]).resolve(strict=True)
        return jsonify(inspect_run(BASE_DIR, run_id, vault_key(vault),
                       offset=max(0, int(request.args.get("offset", 0))),
                       limit=min(100, max(1, int(request.args.get("limit", 50))))))
    except (ValueError, OSError, KeyError, TypeError):
        return jsonify({"error": "Run evidence unavailable or belongs to another vault"}), 409


@online_ocr_bp.post("/api/online-ocr/session/continue-incomplete")
def continue_incomplete():
    """Explicit omissions are enforced at the loader, never just receipt labels."""
    from core.online_runs import read_record, run_directory, checkpoint
    from core.cache_exports import _directory, _publish_new
    import json

    data = request.get_json(silent=True) or {}
    if data.get("confirm_omissions") is not True:
        return jsonify({"error": "Confirm the exact omissions"}), 400
    try:
        directory = run_directory(BASE_DIR, data.get("run_id"))
        run = read_record(directory / "run.json")
        preparation = _receipt_prepare_only(run)
        explicit = str(run.get("session_token", "")).startswith("intent-v1:")
        if explicit and data.get("operation_mode") != run.get("operation_mode"):
            raise ValueError("Recovery operation mode must match the receipt")
        if explicit and preparation:
            if data.get("confirm_preparation") is not True or data.get("confirm_index") is True:
                raise ValueError("Confirm text preparation without indexing")
        elif data.get("confirm_index") is not True:
            raise ValueError("Confirm indexing and the exact omissions")
        cfg = load_config()
        vault = Path(cfg["obsidian_vault_path"]).resolve(strict=True)
        if run.get("vault_key") != vault_key(vault) or run.get("index_settings") != _index_settings(cfg):
            raise ValueError("Vault or index settings changed")
        if (directory / "promotion-started.json").exists():
            raise ValueError("Promotion barrier exists; inspect landed cache evidence before recovery")
        if run.get("full_rebuild") and data.get("confirm_full_rebuild") is not True:
            raise ValueError("Confirm rebuild: deferred sources will be omitted from the new index")
        inventory = _session_inventory(vault, large_run=True, routine=run.get("routine", False),
                                       paths=run.get("paths"))
        expected_token = _intent_token(inventory["session_token"], run.get("operation_mode")
                                       if run.get("initiating_action") != "legacy_session" else None,
                                       run.get("initiating_action"))
        if run.get("session_token") != expected_token or data.get("session_token") != expected_token:
            raise ValueError("Selection changed; obtain a fresh preview")
        _preflight_index_settings(run["index_settings"], force_fresh=run.get("full_rebuild", False))
        completed, omitted = [], []
        for index, source_row in enumerate(inventory["sources"]):
            prefix = f"source-{index:06d}"
            candidates = [directory / f"{prefix}.json"] + sorted(directory.glob(f"{prefix}-r[0-9][0-9][0-9].json"))
            existing = [path for path in candidates if path.exists()]
            outcome = read_record(existing[-1]) if existing else {}
            if outcome and outcome.get("source") != source_row["source"]:
                raise ValueError("Source receipt mismatch")
            if outcome.get("failure_scope") == "global":
                raise ValueError("Runtime or integrity failure cannot be omitted; inspect and retry explicitly")
            if outcome.get("status") in ("processed_unverified", "ocr"):
                if outcome.get("source_sha256") != source_row["source_sha256"]:
                    raise ValueError("Source bytes changed")
                source = vault / outcome["source"]
                completed.append((vault, source, outcome["source"], outcome,
                                  _promotable_text(source, vault, outcome)))
            else:
                omitted.append(source_row["source"])
        if not omitted or data.get("omit_sources") != sorted(omitted):
            raise ValueError("Exact sorted unfinished source list required")
        manifest = {"approved": {row["source"]: row["source_sha256"]
                    for row in inventory.get("reused", [])}, "deferred": sorted(omitted)}
        manifest["approved"].update({rel: row["source_sha256"] for _, _, rel, row, _ in completed})
        if not obsidian_manager.wait_for_indexing(timeout=0):
            raise ValueError("Indexing is active")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return jsonify({"error": str(exc)}), 409
    with _lock:
        if _job.get("state") in ("running", "promoting", "indexing"):
            return jsonify({"error": "Another online worker owns the run"}), 409
        epoch = obsidian_manager.try_acquire_lock(ttl=3600)
        if not epoch:
            return jsonify({"error": "Another vault operation is active"}), 409
        _cancel.clear()
        _job.clear()
        _job["index_run_id"] = obsidian_manager.get_progress_snapshot().get("run_id")
        _job.update({"state": "promoting", "scope": "vault_session", "run_index": True,
                     "run_id": directory.name, "prepare_only": run.get("prepare_only", False),
                     "run_receipt": str(directory / "run.json"), "omitted_sources": omitted,
                     "extraction_manifest": manifest, "index_settings": run["index_settings"]})

    def worker():
        try:
            with _directory(directory) as fd:
                _publish_new(fd, "accepted-omissions.json", json.dumps(manifest, sort_keys=True).encode())
                _publish_new(fd, "promotion-started.json", json.dumps({"session_token": run["session_token"]}).encode())
            _promote_session_selected(completed, epoch, run_index=True, settings=run["index_settings"],
                                      full_rebuild=run.get("full_rebuild", False), manifest=manifest,
                                      prepare_only=run.get("prepare_only", False))
            with _lock:
                if _job["state"] == "indexed":
                    _job["state"] = "completed_with_omissions"
        except Exception as exc:
            with _lock:
                _job.update(state="promotion_or_index_incomplete", error=type(exc).__name__)
        finally:
            try:
                checkpoint(directory, _job["state"], **_checkpoint_details())
            finally:
                obsidian_manager.release_lock(epoch)
    thread = threading.Thread(target=worker, daemon=True, name="online-explicit-omissions")
    try:
        obsidian_manager.register_index_thread(thread)
        thread.start()
    except Exception:
        obsidian_manager.release_lock(epoch)
        with _lock:
            _job["state"] = "failed"
        raise
    return jsonify({"ok": True, "run_id": directory.name, "omitted_count": len(omitted),
                    "prior_content": "omitted_from_new_index" if run.get("full_rebuild") else "retained_if_present"})


@online_ocr_bp.post("/api/online-ocr/session/continue-index")
def continue_index():
    """Reconcile landed bytes, then explicitly continue a compatible index only."""
    from core.online_runs import read_record, run_directory, checkpoint
    data = request.get_json(silent=True) or {}
    if data.get("confirm_index") is not True:
        return jsonify({"error": "Confirm the incremental index continuation"}), 400
    try:
        directory = run_directory(BASE_DIR, data.get("run_id"))
        run = read_record(directory / "run.json")
        if _receipt_prepare_only(run):
            raise ValueError("Preparation cannot continue into indexing; obtain a fresh search-update preview")
        cfg = load_config()
        vault = Path(cfg["obsidian_vault_path"]).resolve(strict=True)
        if run.get("vault_key") != vault_key(vault) or run.get("index_settings") != _index_settings(cfg):
            raise ValueError("Run vault or embedding settings changed")
        if not (directory / "promotion-started.json").is_file():
            raise ValueError("No promotion barrier to reconcile")
        evidence = read_record(directory / "index-input.json", max_bytes=16 * 1024 * 1024)
        _check_continuation_scope(evidence, cfg)
        promotion_id = evidence.get("promotion_id")
        if not isinstance(promotion_id, str) or not re.fullmatch(r"[0-9a-f]{32}", promotion_id):
            raise ValueError("Invalid promotion identity")
        root = Path(BASE_DIR) / "online_ocr_promotions"
        if root.is_symlink():
            raise ValueError("Unsafe promotion root")
        promotion = root / promotion_id
        receipt = read_record(promotion / "receipt.json", max_bytes=16 * 1024 * 1024)
        for index, row in enumerate(receipt["sources"]):
            _, source, rel = _source(row["source"])
            cache = _promotion_cache(vault, source, row["source_sha256"])
            landed = read_record(promotion / f"landed-{index:04d}.json")
            if (landed.get("source") != rel or landed.get("text_sha256") != row["text_sha256"] or
                    cache.is_symlink() or cache.parent.is_symlink() or
                    hashlib.sha256(cache.read_bytes()).hexdigest() != row["text_sha256"]):
                raise ValueError("Promotion is partial or cache bytes changed; inspect before-images")
        manifest = evidence["manifest"]
        for rel, digest in manifest["approved"].items():
            _, source, _ = _source(rel)
            if _digest(source) != digest:
                raise ValueError("Approved source changed; continuation refused")
        _preflight_index_settings(run["index_settings"])
        if not obsidian_manager.wait_for_indexing(timeout=0):
            raise ValueError("Indexing is active")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return jsonify({"error": str(exc)}), 409
    with _lock:
        if _job.get("state") in ("running", "promoting", "indexing"):
            return jsonify({"error": "Another online worker owns the run"}), 409
        epoch = obsidian_manager.try_acquire_lock(ttl=3600)
        if not epoch:
            return jsonify({"error": "Another vault operation is active"}), 409
        _cancel.clear()
        _job.clear()
        _job["index_run_id"] = obsidian_manager.get_progress_snapshot().get("run_id")
        _job.update(state="indexing", run_index=True, run_id=directory.name,
                    run_receipt=str(directory / "run.json"))
    def worker():
        try:
            settings = run["index_settings"]
            _check_continuation_scope(evidence, load_config())
            if settings != _index_settings(load_config()):
                raise ValueError("Settings changed before index continuation")
            with _lock:
                if _cancel.is_set() or not obsidian_manager.heartbeat(epoch):
                    _job.update(state="index_incomplete", error="Continuation cancelled or operation lock lost")
                    return
                obsidian_manager.begin_index_run(epoch)
                _job["index_run_id"] = obsidian_manager.get_progress_snapshot().get("run_id")
            obsidian_manager.index_vault(settings["llm"], settings["embed"],
                                         provider_name=settings["provider"], op_epoch=epoch,
                                         online_extraction=True, extraction_manifest=manifest)
            with _lock:
                _job.update(_index_outcome(manifest, full_rebuild=run.get("full_rebuild", False)))
        except Exception as exc:
            _job.update(state="index_incomplete", error=type(exc).__name__)
        finally:
            try:
                checkpoint(directory, _job["state"], **_checkpoint_details())
            finally:
                obsidian_manager.release_lock(epoch)
    try:
        thread = threading.Thread(target=worker, daemon=True, name="online-index-continuation")
        obsidian_manager.register_index_thread(thread)
        thread.start()
    except Exception:
        obsidian_manager.release_lock(epoch)
        _job["state"] = "index_incomplete"
        raise
    return jsonify({"ok": True})


@online_ocr_bp.post("/api/online-ocr/session/finish-incomplete")
def finish_incomplete_session():
    """Finish an existing full rebuild with exactly the recorded failed sources skipped.

    Only complete, source-bound receipts are promoted. The skipped-source manifest
    is published before the promotion barrier and remains with the original run.
    """
    import json
    from core.cache_exports import _directory, _publish_new

    data = request.get_json(silent=True) or {}
    if not (data.get("confirm_index") is True and
            data.get("confirm_full_rebuild") is True and
            data.get("confirm_skip_incomplete") is True):
        return jsonify({"error": "Confirm the full rebuild and exact incomplete-source exclusions"}), 400
    run_id = data.get("run_id")
    if not isinstance(run_id, str) or re.fullmatch(r"[0-9a-f]{32}", run_id) is None:
        return jsonify({"error": "Invalid online run ID"}), 400
    run_directory = Path(BASE_DIR) / "online_index_runs" / run_id
    try:
        cfg = load_config()
        vault = Path(cfg["obsidian_vault_path"]).resolve(strict=True)
        inventory = _session_inventory(vault, large_run=True)
        run_file = run_directory / "run.json"
        if (run_directory.is_symlink() or run_file.is_symlink() or
                not run_file.is_file() or run_file.stat().st_size > 64 * 1024 or
                (run_directory / "promotion-started.json").exists() or
                (run_directory / "accepted-incomplete.json").exists()):
            raise ValueError("Run receipt is missing or promotion already started")
        run = json.loads(run_file.read_text(encoding="utf-8"))
        if (not run.get("full_rebuild") or
                run.get("session_token") != inventory["session_token"] or
                data.get("session_token") != inventory["session_token"] or
                run.get("source_count") != inventory["source_count"] or
                run.get("eligible") != inventory["eligible"] or
                run.get("vault_key") != vault_key(vault) or
                run.get("index_settings") != _index_settings(cfg) or
                run.get("ocr") != OnlineOCR(
                    run["ocr"]["provider"], run["ocr"]["model"]).identity() or
                run.get("vision") != OnlineOCR(
                    run["vision"]["provider"], run["vision"]["model"],
                    task="vision").identity()):
            raise ValueError("Run identity, settings or inventory changed")
        _preflight_index_settings(run["index_settings"], force_fresh=True)
        completed = []
        skipped = []
        for index, source_row in enumerate(inventory["sources"]):
            prefix = f"source-{index:06d}"
            candidates = [run_directory / f"{prefix}.json"]
            candidates.extend(sorted(run_directory.glob(f"{prefix}-r[0-9][0-9][0-9].json")))
            existing = [path for path in candidates if path.exists()]
            if not existing:
                raise ValueError("Missing source receipt")
            latest = existing[-1]
            if latest.is_symlink() or latest.stat().st_size > 64 * 1024:
                raise ValueError("Unsafe source receipt")
            outcome = json.loads(latest.read_text(encoding="utf-8"))
            if outcome.get("source") != source_row["source"]:
                raise ValueError("Source receipt identity mismatch")
            if outcome.get("failure_scope") == "global":
                raise ValueError("Runtime or integrity failure cannot be omitted; inspect and retry explicitly")
            if outcome.get("status") in ("processed_unverified", "ocr"):
                if outcome.get("source_sha256") != source_row["source_sha256"]:
                    raise ValueError("Completed source hash mismatch")
                completed.append(outcome)
            else:
                skipped.append({"source": source_row["source"],
                                "source_sha256": source_row["source_sha256"],
                                "status": outcome.get("status"),
                                "receipt": latest.name})
        skip_paths = sorted(row["source"] for row in skipped)
        if (not completed or not skipped or
                data.get("skip_sources") != skip_paths):
            raise ValueError("Exact incomplete-source list is required")
        if not obsidian_manager.wait_for_indexing(timeout=0):
            raise ValueError("Indexing is active")
    except (OSError, ValueError, KeyError, TypeError, IndexError) as exc:
        return jsonify({"error": str(exc)}), 409

    with _lock:
        if _job.get("state") in ("running", "promoting", "indexing"):
            return jsonify({"error": "An OCR or index job is running"}), 409
        epoch = obsidian_manager.try_acquire_lock(ttl=3600)
        if not epoch:
            return jsonify({"error": "Another vault operation is active"}), 409
        _cancel.clear()
        _job.clear()
        _job["index_run_id"] = obsidian_manager.get_progress_snapshot().get("run_id")
        _job.update({"state": "running", "scope": "vault_session",
                     "run_id": run_directory.name, "started_at": time.time(),
                     "last_activity_at": time.time(), "prepare_only": bool(run.get("prepare_only")),
                     "run_index": True, "full_rebuild": True,
                     "run_receipt": str(run_file),
                     "attempt_generation": run["attempt_generation"],
                     "index_settings": run["index_settings"],
                     "expected_sources": [row["source"] for row in inventory["sources"]],
                     "source_count": inventory["source_count"],
                     "results": completed,
                     "skipped_sources": skip_paths,
                     "extraction_manifest": {"approved": {
                         row["source"]: row["source_sha256"] for row in completed},
                         "deferred": skip_paths}})

    def worker():
        try:
            selected = []
            for row in completed:
                source = vault / row["source"]
                selected.append((vault, source, row["source"], row,
                                 _promotable_text(source, vault, row)))
            manifest = {"schema": 1, "run_id": run_id,
                        "attempt_generation": run["attempt_generation"],
                        "session_token": inventory["session_token"],
                        "source_count": inventory["source_count"],
                        "completed_count": len(completed),
                        "skipped_count": len(skipped), "skipped": skipped}
            with _directory(run_directory) as fd:
                _publish_new(fd, "accepted-incomplete.json",
                             json.dumps(manifest, sort_keys=True).encode())
                _publish_new(fd, "promotion-started.json",
                             json.dumps({"session_token": inventory["session_token"],
                                         "accepted_incomplete": "accepted-incomplete.json"},
                                        sort_keys=True).encode())
            with _lock:
                _job["state"] = "promoting"
            _promote_session_selected(selected, epoch, run_index=True,
                                      settings=run["index_settings"], full_rebuild=True,
                                      manifest=_job["extraction_manifest"])
        except Exception as exc:
            with _lock:
                _job.update({"state": "failed", "error": type(exc).__name__})
        finally:
            try:
                from core.online_runs import checkpoint
                checkpoint(run_directory, _job.get("state", "interrupted"), **_checkpoint_details())
            finally:
                obsidian_manager.release_lock(epoch)

    try:
        thread = threading.Thread(target=worker, daemon=True,
                                  name="online-ocr-finish-incomplete")
        obsidian_manager.register_index_thread(thread)
        thread.start()
    except Exception:
        obsidian_manager.release_lock(epoch)
        with _lock:
            _job.update({"state": "failed", "error": "Finish worker failed to start"})
        raise
    return jsonify({"ok": True, "run_id": run_id,
                    "completed_count": len(completed), "skipped_count": len(skipped)})


@online_ocr_bp.post("/api/online-ocr/cancel")
def cancel():
    with _lock:
        _cancel.set()
        indexing = _job.get("run_index") is True and _job.get("state") == "indexing"
        if indexing:
            obsidian_manager.cancel_indexing()
        phase = "indexing" if indexing else ("promotion" if _job.get("state") == "promoting" else "extraction")
    return jsonify({"ok": True, "phase": phase})


@online_ocr_bp.post("/api/online-ocr/promote")
def promote():
    """Explicitly select completed text and run the unchanged incremental indexer."""
    data = request.get_json(silent=True) or {}
    if data.get("confirm_index") is not True:
        return jsonify({"error": "Confirm the cache and index update"}), 400
    try:
        vault, source, rel = _source(data.get("path"))
        if not obsidian_manager.wait_for_indexing(timeout=0):
            raise ValueError("Indexing is active")
        recovered = None
        if data.get("run_id"):
            from core.online_runs import read_record, run_directory, file_outcome
            directory = run_directory(BASE_DIR, data["run_id"])
            run = read_record(directory / "run.json")
            if (run.get("kind") != "file" or run.get("vault_key") != vault_key(vault) or
                    run.get("source") != rel or (directory / "promotion-started.json").exists()):
                raise ValueError("File run identity changed or promotion already started")
            recovered = {**file_outcome(directory), "index_settings": run["index_settings"],
                         "run_receipt": str(directory / "run.json")}
        with _lock:
            if _job.get("state") in ("running", "promoting", "indexing"):
                raise ValueError("Another online worker owns the run")
            if recovered is not None:
                _job.clear()
                _job["index_run_id"] = obsidian_manager.get_progress_snapshot().get("run_id")
                _job.update(recovered)
            if (_job.get("state") != "done" or _job.get("source") != rel or
                    _job.get("coverage") != "processed_unverified"):
                raise ValueError("No completed extraction for this source")
            job = dict(_job)
            settings = job.get("index_settings") or _index_settings(load_config())
            if settings != _index_settings(load_config()):
                raise ValueError("Embedding settings changed since extraction")
            _preflight_index_settings(settings)
            text = _promotable_text(source, vault, job)
            epoch = obsidian_manager.try_acquire_lock(ttl=3600)
            if not epoch:
                raise ValueError("Another vault operation is active")
            _cancel.clear()
            _job["state"] = "promoting"
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return jsonify({"error": str(exc)}), 409

    def worker():
        try:
            if job.get("run_receipt"):
                from core.cache_exports import _directory, _publish_new
                with _directory(Path(job["run_receipt"]).parent) as fd:
                    _publish_new(fd, "promotion-started.json", b'{"schema":1}')
            _promote_session_selected([(vault, source, rel, job, text)], epoch,
                                      run_index=True, settings=settings,
                                      manifest={"approved": {rel: job["source_sha256"]}})
        except Exception as exc:
            with _lock:
                _job.update({"state": "failed", "error": type(exc).__name__})
        finally:
            try:
                if job.get("run_receipt"):
                    from core.online_runs import checkpoint
                    checkpoint(Path(job["run_receipt"]).parent, _job.get("state", "interrupted"), **_checkpoint_details())
            finally:
                obsidian_manager.release_lock(epoch)

    try:
        thread = threading.Thread(target=worker, daemon=True, name="online-ocr-promote")
        obsidian_manager.register_index_thread(thread)
        thread.start()
    except Exception:
        obsidian_manager.release_lock(epoch)
        with _lock:
            _job.update({"state": "failed", "error": "Promotion worker failed to start"})
        raise
    return jsonify({"ok": True})


@online_ocr_bp.post("/api/online-ocr/session/promote")
def session_promote():
    """Select successful session results together; retain incomplete sources."""
    data = request.get_json(silent=True) or {}
    if data.get("confirm_index") is not True:
        return jsonify({"error": "Confirm the session cache and index update"}), 400
    try:
        recovered = None
        if data.get("run_id"):
            from core.online_runs import read_record, run_directory
            directory = run_directory(BASE_DIR, data["run_id"])
            run = read_record(directory / "run.json")
            cfg = load_config()
            vault = Path(cfg["obsidian_vault_path"]).resolve(strict=True)
            if (run.get("kind") != "extraction_session" or run.get("vault_key") != vault_key(vault) or
                    run.get("index_settings") != _index_settings(cfg) or
                    (directory / "promotion-started.json").exists()):
                raise ValueError("Session identity changed or promotion already started")
            inventory = read_record(directory / "inventory.json", max_bytes=16 * 1024 * 1024)
            outcomes = []
            for index, source in enumerate(inventory["sources"]):
                prefix = f"source-{index:06d}"
                paths = [directory / f"{prefix}.json"] + sorted(directory.glob(f"{prefix}-r[0-9][0-9][0-9].json"))
                paths = [path for path in paths if path.exists()]
                if not paths:
                    raise ValueError("Session extraction is unfinished")
                outcome = read_record(paths[-1])
                if outcome.get("source") != source["source"]:
                    raise ValueError("Source receipt mismatch")
                outcomes.append(outcome)
            recovered = {"state": "done", "scope": "vault_session", "results": outcomes,
                         "index_settings": run["index_settings"], "run_index": False,
                         "run_receipt": str(directory / "run.json"),
                         "expected_sources": [row["source"] for row in inventory["sources"]]}
        with _lock:
            if _job.get("state") in ("running", "promoting", "indexing"):
                raise ValueError("Another online worker owns the run")
            if recovered is not None:
                _job.clear()
                _job["index_run_id"] = obsidian_manager.get_progress_snapshot().get("run_id")
                _job.update(recovered)
            if _job.get("state") != "done" or _job.get("scope") != "vault_session":
                raise ValueError("No completed vault extraction session")
            expected = set(_job.get("expected_sources", ()))
            completed = {row.get("source") for row in _job["results"]
                         if row.get("status") in ("processed_unverified", "ocr")}
            if not expected or completed != expected:
                missing = sorted(expected - completed)
                raise ValueError(
                    "Online extraction is incomplete; index update refused"
                    + (f" ({len(missing)} source(s) unfinished)" if missing else ""))
            run_index = bool(_job.get("run_index"))
            settings = _job.get("index_settings") or _index_settings(load_config())
            if run_index and settings != _index_settings(load_config()):
                raise ValueError("Index or embedding settings changed since extraction started")
            candidates = [dict(row) for row in _job["results"]
                          if row["status"] in ("processed_unverified", "ocr")]
            if not candidates:
                raise ValueError("No completed source text to promote")
            selected = []
            for row in candidates:
                vault, source, rel = _source(row["source"])
                if rel != row["source"]:
                    raise ValueError("Session source changed")
                selected.append((vault, source, rel, row,
                                 _promotable_text(source, vault, row)))
            if not obsidian_manager.wait_for_indexing(timeout=0):
                raise ValueError("Indexing is active")
            epoch = obsidian_manager.try_acquire_lock(ttl=3600)
            if not epoch:
                raise ValueError("Another vault operation is active")
            run_receipt = _job.get("run_receipt")
            _cancel.clear()
            _job["state"] = "promoting"
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return jsonify({"error": str(exc)}), 409

    def worker():
        try:
            if run_receipt:
                from core.cache_exports import _directory, _publish_new
                with _directory(Path(run_receipt).parent) as fd:
                    _publish_new(fd, "promotion-started.json", b'{"schema":1}')
            _promote_session_selected(selected, epoch, run_index=run_index, settings=settings)
        except Exception as exc:
            with _lock:
                _job.update({"state": "failed", "error": type(exc).__name__})
        finally:
            try:
                if run_receipt:
                    from core.online_runs import checkpoint
                    checkpoint(Path(run_receipt).parent, _job.get("state", "interrupted"), **_checkpoint_details())
            finally:
                obsidian_manager.release_lock(epoch)

    try:
        thread = threading.Thread(target=worker, daemon=True, name="online-ocr-session-promote")
        obsidian_manager.register_index_thread(thread)
        thread.start()
    except Exception:
        obsidian_manager.release_lock(epoch)
        with _lock:
            _job.update({"state": "failed", "error": "Session promotion failed to start"})
        raise
    return jsonify({"ok": True})


def _promote_session_selected(selected: list, epoch: int, *, run_index: bool,
                              settings: dict | None,
                              full_rebuild: bool = False,
                              manifest: dict | None = None, prepare_only: bool = False) -> None:
    """Journal exact cache replacements before the existing indexer consumes them."""
    import json
    from uuid import uuid4
    from core.cache_exports import _directory, _publish_new

    run_id = _job.get("run_id", "")
    run_file = (Path(_job["run_receipt"]) if _job.get("run_receipt") else
                Path(BASE_DIR) / "online_index_runs" / run_id / "run.json")
    if _job.get("run_receipt") or (re.fullmatch(r"[0-9a-f]{32}", run_id) and run_file.exists()):
        from core.online_runs import read_record
        intent = read_record(run_file)
        if _receipt_prepare_only(intent):
            prepare_only = True
            if full_rebuild:
                raise ValueError("Preparation receipt cannot rebuild the index")

    def may_continue():
        # A cache replacement and its landed receipt form one durable unit.
        # Stop between units; keep partial publication evidence for recovery.
        owned = obsidian_manager.heartbeat(epoch)
        with _lock:
            if not owned or _cancel.is_set():
                _job.update(state=("promotion_or_index_incomplete" if _job.get("promotion_receipt")
                                   else "failed" if not owned else "cancelled"),
                            error="Operation lock lost" if not owned else "Cancellation requested",
                            error_detail="No further cache publication or indexing was started. Review saved promotion evidence before continuing.")
                return False
        return True

    if not may_continue():
        return
    cfg = load_config()
    if settings != _index_settings(cfg):
        raise ValueError("Index or embedding settings changed before promotion")
    if full_rebuild and not run_index:
        raise ValueError("Full rebuild requires online extraction")
    if settings is None:
        raise ValueError("Online index settings are missing")
    _preflight_index_settings(settings, force_fresh=full_rebuild)
    if manifest is None:
        manifest = _job.get("extraction_manifest") or {"approved": {
            rel: row["source_sha256"] for _, _, rel, row, _ in selected}}
    receipt_dir = Path(BASE_DIR) / "online_ocr_promotions" / uuid4().hex
    run_receipt = _job.get("run_receipt")
    if run_receipt:
        from core.online_runs import run_directory
        directory = run_directory(BASE_DIR, Path(run_receipt).parent.name)
        with _directory(directory) as fd:
            _publish_new(fd, "index-input.json", json.dumps({"schema": 1,
                         "promotion_id": receipt_dir.name, "manifest": manifest,
                         "index_settings": settings, "scope_binding": _continuation_scope(cfg)}, sort_keys=True).encode())
    writes = []
    with _directory(receipt_dir, create=True) as fd:
        for index, (vault, source, rel, row, text) in enumerate(selected):
            if not may_continue():
                return
            if _digest(source) != row["source_sha256"]:
                raise ValueError("Session source changed before promotion")
            cache = _promotion_cache(vault, source, row["source_sha256"])
            if cache.is_symlink():
                raise ValueError("Symlink cache path refused")
            previous = cache.read_bytes() if cache.is_file() else None
            if previous is not None:
                _publish_new(fd, f"before-{index:04d}.txt", previous)
            writes.append((cache, text, source, row["source_sha256"], rel, previous))
        _publish_new(fd, "receipt.json", json.dumps({"sources": [
            {"source": rel, "source_sha256": row["source_sha256"],
             "recipe": row["recipe"], "cache": str(writes[index][0]),
             "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
             "before_sha256": (hashlib.sha256(cast(bytes, writes[index][5])).hexdigest()
                               if writes[index][5] is not None else None)}
            for index, (_, _, rel, row, text) in enumerate(selected)]},
            sort_keys=True).encode())
    with _lock:
        _job["promotion_receipt"] = str(receipt_dir / "receipt.json")
        _job["promoted_sources"] = []
    for index, (cache, text, source, digest, rel, previous) in enumerate(writes):
        if cache.is_symlink() or _digest(source) != digest:
            raise ValueError("Session source or cache changed before write")
        _promotion_cache(vault, source, digest)
        if (cache.read_bytes() if cache.is_file() else None) != previous:
            raise ValueError("Cache changed after before-image capture")
        if not may_continue():
            return
        obsidian_manager._atomic_write_text(cache, text)
        with _directory(receipt_dir) as fd:
            _publish_new(fd, f"landed-{index:04d}.json", json.dumps({
                "source": rel, "cache": str(cache),
                "text_sha256": hashlib.sha256(text.encode()).hexdigest()},
                sort_keys=True).encode())
        with _lock:
            _job["promoted_sources"].append(rel)
    cfg = load_config()
    if settings != _index_settings(cfg):
        raise ValueError("Index or embedding settings changed before indexing")
    if not may_continue():
        return
    if prepare_only:
        with _lock:
            _job.update(state="text_prepared", deferred_count=len(manifest.get("deferred", [])),
                        deferred_content="index_unchanged")
        return
    provider = cfg.get("provider", "ollama")
    with _lock:
        # Serialize cancellation with begin_index_run, which clears the indexer's
        # stop event. A cancel after this handoff reaches cancel_indexing.
        if _cancel.is_set() or not obsidian_manager.heartbeat(epoch):
            _job.update(state="promotion_or_index_incomplete", error="Cancellation requested or operation lock lost")
            return
        obsidian_manager.begin_index_run(epoch)
        _job["index_run_id"] = obsidian_manager.get_progress_snapshot().get("run_id")
        _job["state"] = "indexing"
        _job["run_index"] = True
        _job.setdefault("source_count", len(selected))
    obsidian_manager.index_vault(_index_settings(cfg)["llm"],
                                 cfg.get("embed", DEFAULT_EMBED), provider_name=provider,
                                 op_epoch=epoch, online_extraction=True,
                                 force_fresh=full_rebuild, extraction_manifest=manifest)
    with _lock:
        _job.update(_index_outcome(manifest, full_rebuild=full_rebuild))


@online_ocr_bp.post("/api/online-ocr/report-bad")
def report_bad_extraction():
    """Explicit app-only decision, bound to the inspected source hash."""
    from accession.review import identity
    from core.online_runs import create_file_run, save_file_outcome, checkpoint
    from uuid import uuid4
    data = request.get_json(silent=True) or {}
    if data.get("confirm") is not True:
        return jsonify({"error": "Confirm deferring this document until an explicit retry"}), 400
    epoch = obsidian_manager.try_acquire_lock(ttl=60)
    if not epoch:
        return jsonify({"error": "Another vault operation is active"}), 409
    try:
        vault, source, rel = _source(data.get("path"))
        current = identity(source)
        if current["sha256"] != data.get("source_sha256"):
            raise ValueError("Source changed since inspection")
        reason = data.get("reason", "User reported an incorrect extraction")
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 1000:
            raise ValueError("Provide a reason of at most 1000 characters")
        directory, _ = create_file_run(BASE_DIR, {"schema": 2, "kind": "extraction_decision",
            "vault_key": vault_key(vault), "source": rel, "source_sha256": current["sha256"],
            "attempt_generation": uuid4().hex, "source_count": 1})
        save_file_outcome(directory, {"source": rel, "source_sha256": current["sha256"],
            "document_identity": {k: current[k] for k in ("ino", "birth")},
            "status": "reported_bad", "reason": reason.strip(), "recorded_at_ns": time.time_ns()})
        checkpoint(directory, "deferred_by_user")
        return jsonify({"ok": True, "run_id": directory.name})
    except (OSError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 409
    finally:
        obsidian_manager.release_lock(epoch)


@online_ocr_bp.post('/api/online-ocr/recovery/import')
def import_recovery():
    """Confirmed source-bound import; use the existing journal and indexer."""
    import json
    from uuid import uuid4
    from core.cache_exports import _directory, _publish_new
    from core.recovery_import import prepare
    from core.online_runs import checkpoint
    data = request.get_json(silent=True) or {}
    if data.get('confirm_import') is not True or data.get('confirm_index') is not True:
        return jsonify({'error': 'Confirm recovery publication and incremental indexing'}), 400
    epoch = None
    try:
        cfg = load_config()
        vault = Path(cfg['obsidian_vault_path']).resolve(strict=True)
        with _lock:
            if _job.get('state') in ('running', 'promoting', 'indexing'):
                raise ValueError('An online operation is active')
            if not obsidian_manager.wait_for_indexing(timeout=0):
                raise ValueError('Indexing is active')
            epoch = obsidian_manager.try_acquire_lock(ttl=3600)
            if not epoch:
                raise ValueError('Another vault operation is active')
        root, selected, excluded = prepare(Path(BASE_DIR), vault, data.get('recovery_id'),
                                           data.get('catalog_sha256'))
        if (root / 'import-started.json').exists():
            raise ValueError('Recovery import was already attempted; inspect retained evidence')
        settings = _index_settings(cfg)
        _preflight_index_settings(settings)
        inventory = _session_inventory(vault, routine=True, large_run=True, preview_pages=False)
        available = {r['source']: r['source_sha256'] for r in inventory['sources'] + inventory['reused']}
        if any(available.get(rel) != row['source_sha256'] for _, _, rel, row, _ in selected):
            raise ValueError('Recovery sources are excluded or changed in current index scope')
        if any(source.suffix.lower() == '.pdf' and
               obsidian_manager._selected_pdf_payload(source, vault) is not None
               for _, source, _, _, _ in selected):
            raise ValueError('An active PDF profile supersedes recovered cache text')
        # Identical attachments share one hash-keyed cache. Journal/write that
        # cache once while retaining every alias in admission and run evidence.
        unique_caches = {}
        for item in selected:
            key = (item[1].suffix.lower() == '.pdf', item[3]['source_sha256'])
            if key in unique_caches and unique_caches[key][4] != item[4]:
                raise ValueError('Conflicting text for the same cache identity')
            unique_caches.setdefault(key, item)
        publish_selected = list(unique_caches.values())
        accepted = {rel for _, _, rel, _, _ in selected}
        deferred = sorted(({r['source'] for r in inventory['sources']} - accepted) | set(excluded))
        manifest = {'approved': {r['source']: r['source_sha256'] for r in inventory['reused']},
                    'deferred': deferred}
        manifest['approved'].update({rel: row['source_sha256'] for _, _, rel, row, _ in selected})
        for rel in deferred:
            manifest['approved'].pop(rel, None)
        run_id = uuid4().hex
        directory = Path(BASE_DIR) / 'online_index_runs' / run_id
        with _directory(directory, create=True) as fd:
            _publish_new(fd, 'run.json', json.dumps({'schema': 2, 'kind': 'recovery_import',
                'vault_key': vault_key(vault), 'source_count': len(selected), 'run_index': True,
                'prepare_only': False, 'full_rebuild': False, 'index_settings': settings,
                'recovery_id': data['recovery_id'], 'catalog_sha256': data['catalog_sha256']}, sort_keys=True).encode())
            _publish_new(fd, 'inventory.json', json.dumps({'sources': [row for _, _, _, row, _ in selected]}, sort_keys=True).encode())
            for index, (_, _, _, row, _) in enumerate(selected):
                _publish_new(fd, f'source-{index:06d}.json', json.dumps(row, sort_keys=True).encode())
            _publish_new(fd, 'promotion-started.json', b'{"schema":1}')
        with _directory(root) as fd:
            _publish_new(fd, 'import-started.json', json.dumps({'run_id': run_id,
                'catalog_sha256': data['catalog_sha256'], 'deferred': deferred}, sort_keys=True).encode())
        with _lock:
            _cancel.clear()
            _job.clear()
            _job["index_run_id"] = obsidian_manager.get_progress_snapshot().get("run_id")
            _job.update(state='promoting', run_id=run_id, scope='vault_session', run_index=True,
                run_receipt=str(directory / 'run.json'), started_at=time.time(), last_activity_at=time.time(),
                source_count=len(selected), expected_sources=sorted(accepted),
                deferred_count=len(deferred), deferred_content='retained_if_present',
                results=[row for _, _, _, row, _ in selected], extraction_manifest=manifest)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        if epoch:
            obsidian_manager.release_lock(epoch)
        return jsonify({'error': sanitise_error_msg(str(exc))}), 409

    def worker():
        try:
            _promote_session_selected(publish_selected, epoch, run_index=True, settings=settings, manifest=manifest)
        except Exception as exc:
            with _lock:
                _job.update(state='promotion_or_index_incomplete' if _job.get('promotion_receipt') else 'failed',
                            error=sanitise_error_msg(str(exc)))
        finally:
            try:
                checkpoint(directory, _job.get('state', 'interrupted'), **_checkpoint_details())
            finally:
                obsidian_manager.release_lock(epoch)
    try:
        thread = threading.Thread(target=worker, daemon=True, name='recovery-import')
        obsidian_manager.register_index_thread(thread)
        thread.start()
    except Exception:
        obsidian_manager.release_lock(epoch)
        with _lock:
            _job.update(state='failed', error='Recovery worker failed to start; inspect import evidence')
        raise
    return jsonify({'ok': True, 'run_id': run_id, 'accepted': len(selected), 'cache_count': len(publish_selected), 'deferred': deferred})
