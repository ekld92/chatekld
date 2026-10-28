"""Read-only query identity resolution. No formatting, model probes or migration."""
from dataclasses import dataclass

from core.config import is_local_provider, resolve_embed_provider


@dataclass(frozen=True)
class QueryEmbeddingIdentity:
    provider: str
    model: str
    warnings: tuple[str, ...]


def resolve_query_identity(meta: dict | None, cfg: dict, chat_provider: str,
                           requested_model: str) -> QueryEmbeddingIdentity:
    meta = meta or {}
    configured = resolve_embed_provider(cfg, chat_provider)
    recorded = meta.get("embed_provider")
    if recorded and not isinstance(recorded, str):
        raise RuntimeError("Saved embedding backend identity is malformed; review required.")
    if recorded and not is_local_provider(recorded):
        raise RuntimeError("Saved embedding backend is unsupported; index identity requires review.")
    backend = recorded or (meta.get("provider") if is_local_provider(str(meta.get("provider") or "")) else None)
    model = meta.get("embed") or requested_model
    if not isinstance(model, str):
        raise RuntimeError("Saved embedding model identity is malformed; review required.")
    warnings = []
    if not backend or not meta.get("embed"):
        warnings.append("Legacy index embedding identity is incomplete; compatibility is unknown. "
                        "Using the established legacy selection without changing saved metadata.")
    if backend and backend != configured:
        warnings.append(f"Index uses embedding backend '{backend}'; retaining it despite chat/settings "
                        f"selection '{configured}'. No rebuild was started.")
    if meta.get("embed") and model != requested_model:
        warnings.append(f"Index was built with embedding model '{model}'; retrieving with it "
                        f"(re-index to switch to '{requested_model}').")
    return QueryEmbeddingIdentity(backend or configured, model, tuple(warnings))
