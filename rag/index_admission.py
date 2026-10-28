"""Read-only admission shared by extraction promotion and index publication."""
from pathlib import Path

from core.constants import OBSIDIAN_INDEX_VERSION


def index_admission(manager, index_dir, vault, embed, provider, *, force_fresh=False):
    """Never infer rebuild consent from changed settings or damaged storage."""
    meta = manager._read_index_meta()
    if meta and meta.get("vector_backend") == "lancedb":
        from rag.lancedb_store import lancedb_available
        if not lancedb_available():
            return {"ready": False, "mode": "refused", "reason": "lancedb_unavailable"}
    has_data = manager._index_dir_has_vector_data(str(index_dir))
    if force_fresh:
        return {"ready": True, "mode": "rebuild", "reason": None}
    has_store_files = any((Path(index_dir) / name).exists() for name in
                          ("docstore.json", "index_store.json", "promotion_state.json"))
    recorded_data = bool(meta and meta.get("has_vector_data") is True)
    if not has_data and not has_store_files and not recorded_data:
        return {"ready": True, "mode": "initial", "reason": None}
    reason = None
    if not meta:
        reason = "missing_metadata"
    elif not meta.get("embed") or not manager._prev_embed_provider(meta):
        reason = "missing_embedding_identity"
    elif meta["embed"] != embed:
        reason = "embed_model_mismatch"
    elif manager._prev_embed_provider(meta) != provider:
        reason = "embed_backend_mismatch"
    elif meta.get("version") != OBSIDIAN_INDEX_VERSION:
        reason = "index_version_mismatch"
    elif not meta.get("vault_path") or Path(meta["vault_path"]).resolve() != Path(vault).resolve():
        reason = "vault_identity_mismatch"
    elif not manager._incremental_store_is_intact(meta):
        reason = "interrupted_store"
    return {"ready": reason is None, "mode": "incremental" if reason is None else "refused",
            "reason": reason}


def require_index_admission(manager, index_dir, vault, settings, *, force_fresh=False):
    result = index_admission(manager, index_dir, vault, settings["embed"],
                             settings["embed_provider"], force_fresh=force_fresh)
    if not result["ready"]:
        raise ValueError(f"Index update refused: {result['reason']}. Restore compatible settings "
                         "or explicitly confirm a rebuild; existing index is unchanged.")
    return result


def validate_extraction_manifest(manifest):
    """Internal manifests must fail before any store can be archived or opened."""
    import re
    if not isinstance(manifest, dict) or not isinstance(manifest.get("approved"), dict):
        raise ValueError("Invalid extraction manifest")
    for rel, digest in manifest["approved"].items():
        if (not isinstance(rel, str) or not rel or Path(rel).is_absolute() or
                ".." in Path(rel).parts or not isinstance(digest, str) or
                re.fullmatch(r"[0-9a-f]{64}", digest) is None):
            raise ValueError("Invalid approved source identity")
    deferred = manifest.get("deferred", [])
    if not isinstance(deferred, list) or any(not isinstance(rel, str) or not rel or
            Path(rel).is_absolute() or ".." in Path(rel).parts for rel in deferred):
        raise ValueError("Invalid deferred source list")
    if "allow_local_extraction" in manifest and type(manifest["allow_local_extraction"]) is not bool:
        raise ValueError("Invalid local extraction policy")
    if set(deferred) & manifest["approved"].keys():
        raise ValueError("Source cannot be both approved and deferred")
