"""Bounded, observational access to immutable online run evidence."""
import copy
import json
from pathlib import Path
import re
import hashlib
import threading
from collections import OrderedDict

from core.cache_exports import _directory, _publish_new

_ID = re.compile(r"[0-9a-f]{32}")
_COUNT_CACHE: OrderedDict = OrderedDict()
_RUN_FAILURE_CACHE: OrderedDict = OrderedDict()
_COUNT_LOCK = threading.Lock()


def _run_dir_signature(directory: Path, paths: list[Path]) -> tuple[str, str]:
    if directory.is_symlink():
        raise ValueError("Symlink run directory refused")
    digest = hashlib.sha256()
    for path in sorted(paths, key=lambda p: p.name):
        try:
            stat = path.lstat()
        except OSError as exc:
            raise ValueError("Missing, unsafe or oversized run evidence") from exc
        max_bytes = 16 * 1024 * 1024 if path.name == "inventory.json" else 65536
        if path.is_symlink() or not path.is_file() or stat.st_size > max_bytes:
            raise ValueError("Missing, unsafe or oversized run evidence")
        digest.update(repr((path.name, stat.st_dev, stat.st_ino, stat.st_size,
                            stat.st_mtime_ns, stat.st_ctime_ns)).encode())
    return str(directory.resolve()), digest.hexdigest()


def _count_signature(directory, keys, latest):
    """Cache counts only: each page still reads its own authoritative receipts.

    Include filesystem identity and ctime so replacement/edit/removal invalidates
    the count even when an old mtime is restored. Never cache parsed documents or
    write a sidecar from a history GET. New retries change the selected filename.
    """
    digest = hashlib.sha256()
    for key in keys:
        path = latest.get(key)
        if path is None:
            digest.update((key + ':missing\n').encode())
            continue
        stat = path.lstat()
        if path.is_symlink() or not path.is_file() or stat.st_size > 65536:
            raise ValueError("Missing, unsafe or oversized run evidence")
        digest.update(repr((path.name, stat.st_dev, stat.st_ino, stat.st_size,
                            stat.st_mtime_ns, stat.st_ctime_ns)).encode())
    return str(directory.resolve()), digest.hexdigest()


def read_record(path, *, max_bytes=65536):
    path = Path(path)
    if path.is_symlink() or path.parent.is_symlink() or not path.is_file() or path.stat().st_size > max_bytes:
        raise ValueError("Missing, unsafe or oversized run evidence")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Malformed run evidence")
    return value


def run_directory(base, run_id):
    if not isinstance(run_id, str) or not _ID.fullmatch(run_id):
        raise ValueError("Invalid run ID")
    root = Path(base) / "online_index_runs"
    directory = root / run_id
    if root.is_symlink() or directory.is_symlink():
        raise ValueError("Symlink run directory refused")
    return directory


def source_diagnostics(base, row):
    """Recover old stop diagnostics from hash-bound snapshots, without writes.

    Never follow the arbitrary export path in a receipt. Missing or altered
    snapshots cannot establish a resource cause. At most one prior receipt is
    followed for a deferred result, and only for the same source hash.
    """
    result = dict(row)
    if row.get("stop_reason") or row.get("failure_scope") == "global":
        return result
    original = row
    evidence = row.get("evidence")
    try:
        if row.get("status") == "deferred_known_failure" and isinstance(evidence, dict):
            name = evidence.get("receipt", "")
            if not isinstance(name, str) or not re.fullmatch(r"source-\d{6}(?:-r\d{3})?\.json", name):
                return result
            prior = read_record(run_directory(base, evidence.get("run_id")) / name)
            if not row.get("source_sha256") or prior.get("source_sha256") != row["source_sha256"]:
                return result
            original = prior
        if original.get("stop_reason"):
            from core.ocr_resources import extraction_stop_diagnostic
            result.update(extraction_stop_diagnostic(original["stop_reason"]))
            return result
        if original.get("status") != "partial":
            return result
        identity = original.get("snapshot_id", "")
        if not isinstance(identity, str) or not re.fullmatch(r"[0-9a-f]{64}", identity):
            return result
        bundle = read_record(Path(base) / "online_ocr_exports" / (identity + ".json"),
                             max_bytes=32 * 1024 * 1024)
        payload = bundle.get("payload", {})
        if (not isinstance(payload, dict) or bundle.get("snapshot_id") != identity
                or not original.get("source_sha256")
                or payload.get("source_sha256") != original["source_sha256"]
                or payload.get("recipe") != original.get("recipe")):
            return result
        raw = json.dumps({k: v for k, v in bundle.items() if k != "snapshot_id"},
                         sort_keys=True, ensure_ascii=False, allow_nan=False).encode()
        if hashlib.sha256(raw).hexdigest() != identity:
            return result
        from core.ocr_resources import extraction_stop_diagnostic
        reason = bundle.get("stop_reason", "")
        if isinstance(reason, str):
            result.update(extraction_stop_diagnostic(reason))
    except (ValueError, OSError, TypeError):
        pass  # An unavailable snapshot cannot justify inventing a diagnosis.
    return result


def inspect_run(base, run_id, vault_key, *, offset=0, limit=50):
    directory = run_directory(base, run_id)
    record = read_record(directory / "run.json")
    if record.get("vault_key") != vault_key:
        raise ValueError("Run belongs to another vault")
    latest = {}
    for path in directory.glob("source-*.json") if limit else ():
        if re.fullmatch(r"source-\d{6}(?:-r\d{3})?\.json", path.name):
            key = path.name[:13]
            if key not in latest or ("-r" in path.name, path.name) > ("-r" in latest[key].name, latest[key].name):
                latest[key] = path
    expected = []
    if limit and (directory / "inventory.json").exists():
        expected = read_record(directory / "inventory.json", max_bytes=16 * 1024 * 1024).get("sources", [])
    complete = 0
    rows = []
    keys = [f"source-{index:06d}" for index in range(len(expected))] if expected else sorted(latest)
    signature = _count_signature(directory, keys, latest) if limit else None
    with _COUNT_LOCK:
        cached_count = _COUNT_CACHE.get(signature)
    for index, key in enumerate(keys):
        on_page = offset <= index < offset + limit
        if cached_count is not None and not on_page:
            continue
        row = read_record(latest[key]) if key in latest else {"source": expected[index]["source"], "status": "not_attempted"}
        complete += row.get("status") in ("processed_unverified", "ocr")
        if on_page:
            row = source_diagnostics(base, row)
            projected = {k: row[k] for k in ("source", "source_sha256", "status", "error", "reason", "stop_reason", "failure_scope", "attempt", "warnings", "warning_pages", "error_detail", "http_status", "provider_code", "provider", "model", "next_step") if k in row}
            if row.get("export"):
                projected["export_url"] = f"/api/online-ocr/runs/{run_id}/export/{index}"
            rows.append(projected)
    if cached_count is not None:
        complete = cached_count
    elif signature is not None:
        # Do not publish a mixed snapshot if a receipt changed while reading.
        if signature == _count_signature(directory, keys, latest):
            with _COUNT_LOCK:
                _COUNT_CACHE[signature] = complete
                _COUNT_CACHE.move_to_end(signature)
                while len(_COUNT_CACHE) > 16:
                    _COUNT_CACHE.popitem(last=False)
    checkpoints = sorted(directory.glob("state-*.json"))
    latest_state = read_record(checkpoints[-1]) if checkpoints else {}
    state = latest_state.get("state", "status_unrecorded")
    barrier = (directory / "promotion-started.json").exists()
    if state in ("running", "promoting", "indexing"):
        state = "interrupted"  # Only the in-process worker can assert liveness.
    promotion_evidence = []
    promotion_next_offset = None
    if limit and (directory / "index-input.json").exists():
        binding = read_record(directory / "index-input.json", max_bytes=16 * 1024 * 1024)
        promotion_id = binding.get("promotion_id")
        if not isinstance(promotion_id, str) or not _ID.fullmatch(promotion_id):
            raise ValueError("Invalid promotion identity")
        root = Path(base) / "online_ocr_promotions"
        if root.is_symlink():
            raise ValueError("Unsafe promotion root")
        journal = root / promotion_id
        if (journal / "receipt.json").exists():
            receipt = read_record(journal / "receipt.json", max_bytes=16 * 1024 * 1024)
            entries = receipt.get("sources", [])
            for index in range(offset, min(offset + limit, len(entries))):
                entry = entries[index]
                landed_path = journal / f"landed-{index:04d}.json"
                landed = read_record(landed_path) if landed_path.exists() else {}
                promotion_evidence.append({"source": entry.get("source"),
                    "before_sha256": entry.get("before_sha256"), "text_sha256": entry.get("text_sha256"),
                    "landed_recorded": bool(landed),
                    "landed_hash_matches_receipt": bool(landed) and landed.get("text_sha256") == entry.get("text_sha256"),
                    "current_cache_verified": False})
            promotion_next_offset = offset + limit if offset + limit < len(entries) else None
    return {"run_id": run_id, "schema": record.get("schema"), "state": state,
            "recorded_at": (directory / "run.json").stat().st_mtime,
            "failure": latest_state.get("failure"),
            "status_note": ("This older run has no saved final status. This does not mean the current "
                            "search index is interrupted. Check the vault readiness separately."
                            if state == "status_unrecorded" else None),
            "deferred_count": latest_state.get("deferred_count"),
            "deferred_content": latest_state.get("deferred_content"),
            "source_count": record.get("source_count", 0), "completed_count": complete if limit else None,
            "unfinished_count": max(0, record.get("source_count", 0) - complete) if limit else None,
            "promotion_barrier": barrier, "retry_allowed": not barrier,
            "index_continuation_available": barrier and not record.get("prepare_only")
                and record.get("operation_mode") != "prepare_text" and (directory / "index-input.json").is_file(),
            "index_settings": record.get("index_settings"),
            "session_token": record.get("session_token"), "routine": record.get("routine", False),
            "full_rebuild": record.get("full_rebuild", False),
            "no_external": record.get("no_external", False),
            "prepare_only": record.get("prepare_only", False),
            "operation_mode": record.get("operation_mode"),
            "initiating_action": record.get("initiating_action"),
            "application_version": record.get("application_version"),
            "reused_count": record.get("reused_count", 0),
            "kind": record.get("kind", "session"), "source": record.get("source"),
            "source_sha256": record.get("source_sha256"), "page_range": record.get("page_range"),
            "preview_token": record.get("preview_token"), "reextract": record.get("reextract"),
            "paths": record.get("paths"), "ocr": record.get("ocr"), "vision": record.get("vision"),
            "usage": "Unknown calls may have been billed; retry retains existing attempt bounds.",
            "promotion_evidence": promotion_evidence, "promotion_next_offset": promotion_next_offset,
            "sources": rows, "next_offset": offset + limit if offset + limit < len(keys) else None}


def checkpoint(directory, state, *, deferred_count=None, deferred_content=None, failure=None):
    from time import time_ns
    with _directory(Path(directory)) as fd:
        _publish_new(fd, f"state-{time_ns()}.json", json.dumps({"schema": 1, "state": state,
                     "deferred_count": deferred_count, "deferred_content": deferred_content, "failure": failure}).encode())


def create_file_run(base, record, resume_id=None):
    """Freeze one-file settings/range; explicit retries retain the generation."""
    from uuid import uuid4
    directory = run_directory(base, resume_id or uuid4().hex)
    if resume_id:
        old = read_record(directory / "run.json")
        if (directory / "promotion-started.json").exists():
            raise ValueError("Promotion barrier exists; extraction cannot be replayed")
        expected = {**record, "attempt_generation": old.get("attempt_generation")}
        if old != expected:
            raise ValueError("Saved file identity, range or settings changed")
        record = old
    else:
        with _directory(directory, create=True) as fd:
            _publish_new(fd, "run.json", json.dumps(record, sort_keys=True).encode())
            _publish_new(fd, "inventory.json", json.dumps({"sources": [{"source": record["source"]}]}).encode())
    return directory, record["attempt_generation"]


def save_file_outcome(directory, outcome):
    names = list(Path(directory).glob("source-000000*.json"))
    number = len(names)
    if number > 999:
        raise ValueError("Source receipt retry limit reached")
    name = "source-000000.json" if not names else f"source-000000-r{number:03d}.json"
    with _directory(Path(directory)) as fd:
        _publish_new(fd, name, json.dumps(outcome, sort_keys=True).encode())


def file_outcome(directory):
    directory = Path(directory)
    names = [directory / "source-000000.json"] + sorted(directory.glob("source-000000-r[0-9][0-9][0-9].json"))
    existing = [path for path in names if path.exists()]
    if not existing:
        raise ValueError("No completed file receipt")
    return read_record(existing[-1])


def known_failed_content(base, vault_key, *, all_evidence=False):
    """Read hash-bound failure evidence across generations, without modifying it.

    A rename or model change cannot authorize a new attempt. Old failures lacking
    a hash are admitted only when their run inventory supplies that identity.
    At the inventory caller complete caches take precedence over ordinary failures,
    but not a manual report or uncertain document continuity.
    """
    failures = {}
    evidence = []
    successes = []
    root = Path(base) / "online_index_runs"
    if root.is_symlink():
        raise ValueError("Unsafe run evidence root")
    for directory in sorted(root.glob("*")):
        if not _ID.fullmatch(directory.name):
            continue
        if directory.is_symlink():
            raise ValueError("Symlink run directory refused")

        run_file = directory / "run.json"
        paths = [run_file]
        inv = directory / "inventory.json"
        if inv.exists() or inv.is_symlink():
            paths.append(inv)
        paths.extend(directory.glob("state-*.json"))
        paths.extend(directory.glob("source-*.json"))

        signature = _run_dir_signature(directory, paths)

        with _COUNT_LOCK:
            cached = _RUN_FAILURE_CACHE.get(signature)

        if cached is not None:
            cached_vk, cached_ev, cached_succ = cached
            if cached_vk != vault_key:
                continue
            evidence.extend(copy.deepcopy(cached_ev))
            successes.extend(copy.deepcopy(cached_succ))
            continue

        record = read_record(run_file)
        run_vk = record.get("vault_key")
        if run_vk != vault_key:
            continue

        run_evidence = []
        run_successes = []

        checkpoints = sorted(directory.glob("state-*.json"))
        final = read_record(checkpoints[-1]).get("state") if checkpoints else None
        promoted = final in {
            "text_prepared", "indexed", "completed_with_omissions"}
        inventory = {}
        if (directory / "inventory.json").exists():
            inventory = {r["source"]: r.get("source_sha256") for r in
                         read_record(directory / "inventory.json", max_bytes=16 * 1024 * 1024).get("sources", [])}
        for path in sorted(directory.glob("source-*.json")):
            row = source_diagnostics(base, read_record(path))
            if row.get("failure_scope") == "global":
                continue
            digest = row.get("source_sha256") or inventory.get(row.get("source"))
            if (promoted and (record.get("retry_failed") is True or row.get("retry_authorized") is True)
                    and row.get("status") in {"processed_unverified", "ocr"}):
                run_successes.append({**row, "recorded_at_ns": row.get("recorded_at_ns", path.stat().st_mtime_ns)})
            if row.get("status") not in {"failed", "partial", "reported_bad"} and not source_exception(row):
                continue
            digest = row.get("source_sha256") or inventory.get(row.get("source"))
            if isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest):
                rec_failure = {"reason": row["status"], "run_id": directory.name,
                               "receipt": path.name, "source": row.get("source"),
                               "source_sha256": digest, "document_identity": row.get("document_identity"),
                               "detail": row.get("reason") or row.get("error"), "recorded_at_ns": row.get("recorded_at_ns", path.stat().st_mtime_ns)}
                run_evidence.append(rec_failure)

        # Post-read signature re-check to avoid torn reads during concurrent writes
        post_paths = [run_file]
        if inv.exists() or inv.is_symlink():
            post_paths.append(inv)
        post_paths.extend(directory.glob("state-*.json"))
        post_paths.extend(directory.glob("source-*.json"))
        if signature == _run_dir_signature(directory, post_paths):
            with _COUNT_LOCK:
                _RUN_FAILURE_CACHE[signature] = (
                    run_vk,
                    copy.deepcopy(run_evidence),
                    copy.deepcopy(run_successes),
                )
                _RUN_FAILURE_CACHE.move_to_end(signature)
                while len(_RUN_FAILURE_CACHE) > 64:
                    _RUN_FAILURE_CACHE.popitem(last=False)

        evidence.extend(run_evidence)
        successes.extend(run_successes)
    from accession.review import same_identity
    evidence = [item for item in evidence if not any(
        success["recorded_at_ns"] > item["recorded_at_ns"] and (
            success.get("source_sha256") == item["source_sha256"] or
            success.get("source") == item.get("source") or
            (bool(item.get("document_identity")) and same_identity(
                success.get("document_identity"), item["document_identity"])))
        for success in successes)]
    failures = {item["source_sha256"]: item for item in evidence}
    return evidence if all_evidence else failures


def source_exception(row):
    """Only known source-local outcomes may pass the daily promotion barrier."""
    if row.get("failure_scope") == "global":
        return False
    if row.get("status") in {"image_rejected", "source_failed", "ocr_failed", "conversion_failed", "unsupported_format",
                             "output_limit", "incomplete_response", "timeout", "empty_unverified", "render_limit"}:
        return True
    if row.get("status") == "partial":
        states = set(row.get("page_statuses", []))
        return bool(states) and states <= {
            "native", "ocr", "ocr_failed", "ocr_partial", "native_partial",
            "mixed_unverified", "empty_unverified", "render_limit", "deferred",
            "output_limit", "incomplete_response", "timeout", "conversion_failed", "image_rejected",
        }
    return False


def failure_for(evidence, source, digest, document_identity):
    """Content equality and document continuity are separate observations.

    Same path after replacement is uncertain, never an implicit retry consent.
    """
    from accession.review import same_identity
    matches = []
    for item in evidence:
        same_document = bool(item.get("document_identity")) and same_identity(
            item["document_identity"], document_identity)
        if item["source_sha256"] == digest:
            matches.append(item)
        elif item.get("source") == source or same_document:
            matches.append({**item, "reason": "content_continuity_uncertain"})
    return next((m for m in matches if m["reason"] in {"reported_bad", "content_continuity_uncertain"}),
                matches[-1] if matches else None)
