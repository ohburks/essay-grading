"""ChromaDB-backed retrieval over cached source documents.

One shared collection, metadata-filtered by source_key, rather than one
collection per source: Chroma's per-collection overhead (each persists its
own HNSW segment) makes a collection-per-source wasteful once dozens/
hundreds of sources accumulate across a class, and a single collection with
a `where={"source_key": ...}` filter gives every assessment instant query
access to any source already indexed by another -- the same global-dedup
spirit as the cited_sources cache table.

Uses Chroma's bundled default local embedding function (no API key needed)
so this feature works without configuring an embeddings provider.
"""

import os
import re
import threading
from pathlib import Path

from ...db import database as db

CHROMA_DIR = Path(os.environ.get("CITATIONS_CHROMA_DIR", db.DATA_DIR / "chroma"))
COLLECTION_NAME = "cited_sources"
CHUNK_CHARS = 1200
CHUNK_OVERLAP = 200
TOP_K = 5

_lock = threading.Lock()
_collection_singleton = None


def is_available() -> bool:
    """True iff `import chromadb` succeeds. Does not guarantee the embedding
    model will download successfully -- callers still handle a None collection."""
    try:
        import chromadb  # noqa: F401
        return True
    except ImportError:
        return False


def _collection():
    global _collection_singleton
    if _collection_singleton is not None:
        return _collection_singleton
    with _lock:
        if _collection_singleton is not None:
            return _collection_singleton
        try:
            import chromadb
            from chromadb.config import Settings
            from chromadb.utils import embedding_functions
        except ImportError:
            return None
        try:
            CHROMA_DIR.mkdir(parents=True, exist_ok=True)
            client = chromadb.PersistentClient(
                path=str(CHROMA_DIR), settings=Settings(anonymized_telemetry=False))
            _collection_singleton = client.get_or_create_collection(
                COLLECTION_NAME, embedding_function=embedding_functions.DefaultEmbeddingFunction())
        except Exception:
            return None
        return _collection_singleton


def chunk_text(text: str) -> list[str]:
    text = re.sub(r"\s+", " ", text or "").strip()
    if not text:
        return []
    chunks = []
    step = CHUNK_CHARS - CHUNK_OVERLAP
    start, n = 0, len(text)
    while start < n:
        end = min(start + CHUNK_CHARS, n)
        chunks.append(text[start:end])
        if end >= n:
            break
        start += step
    return chunks


def index_source(source_key: str, text: str) -> int:
    """Chunks and upserts (idempotent -- re-indexing the same source_key
    replaces its chunks rather than duplicating them). Returns chunk count;
    0 if Chroma is unavailable or there's nothing to index."""
    col = _collection()
    if col is None:
        return 0
    chunks = chunk_text(text)
    if not chunks:
        return 0
    ids = [f"{source_key}:{i}" for i in range(len(chunks))]
    metadatas = [{"source_key": source_key, "chunk_index": i} for i in range(len(chunks))]
    col.upsert(ids=ids, documents=chunks, metadatas=metadatas)
    return len(chunks)


def query_source(source_key: str, query_text: str, k: int = TOP_K) -> list[dict]:
    """Returns [{"chunk_id", "text"}] for the top-k chunks of `source_key`
    most relevant to `query_text`. Empty list (never raises) if unindexed."""
    col = _collection()
    if col is None or not (query_text or "").strip():
        return []
    try:
        res = col.query(query_texts=[query_text], n_results=k,
                        where={"source_key": source_key})
    except Exception:
        return []
    ids = (res.get("ids") or [[]])[0]
    docs = (res.get("documents") or [[]])[0]
    return [{"chunk_id": cid, "text": doc} for cid, doc in zip(ids, docs)]
