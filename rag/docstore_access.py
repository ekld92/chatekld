"""Localized read-only counts for LlamaIndex SimpleDocumentStore.

Callers hold the manager mutation lock when consistency is required. The
disk-backed variant uses its maintained collection count. The
private mapping seam is contract-tested on core 0.14.22; older SimpleKVStore
uses _data. Unknown backends deliberately fall back to the public docs API
(O(N) deserialization), never an assumed or cached count.
"""
from llama_index.core.storage.docstore import SimpleDocumentStore
from llama_index.core.storage.kvstore import SimpleKVStore

from rag.disk_docstore import DiskKVStore


def node_count(docstore) -> int:
    if isinstance(docstore, SimpleDocumentStore) and isinstance(docstore._kvstore, DiskKVStore):
        return docstore._kvstore.count(docstore._node_collection)
    if isinstance(docstore, SimpleDocumentStore) and isinstance(docstore._kvstore, SimpleKVStore):
        kv = docstore._kvstore
        mappings = getattr(kv, "_collections_mappings", None)
        if mappings is None:
            mappings = getattr(kv, "_data", None)
        if isinstance(mappings, dict):
            collection = mappings.get(docstore._node_collection, {})
            if isinstance(collection, dict):
                return len(collection)
    return len(docstore.docs)
