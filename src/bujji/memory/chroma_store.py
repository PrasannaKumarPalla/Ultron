"""ChromaDB vector store for long-term memory.

Called from memory/service.py and agents/orchestrator.py.
No existing file. Schema: collection of text docs with metadata.
User instruction: do all remaining ones.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

try:
    import chromadb
    from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction

    _CHROMA_AVAILABLE = True
except ImportError:
    _CHROMA_AVAILABLE = False
    logger.warning(
        "chromadb not installed — ChromaStore will be non-functional. "
        "Install with: pip install chromadb sentence-transformers"
    )


class ChromaStore:
    """ChromaDB-backed vector store for long-term semantic memory.

    Parameters
    ----------
    path:
        Directory path for persistent ChromaDB storage.
    collection:
        Name of the ChromaDB collection.
    embedding_model:
        SentenceTransformer model name for embeddings.
    """

    def __init__(
        self,
        path: str,
        collection: str = "bujji_facts",
        embedding_model: str = "all-MiniLM-L6-v2",
    ) -> None:
        self._path = path
        self._collection_name = collection
        self._embedding_model = embedding_model
        self._client: Any = None
        self._collection: Any = None

        if not _CHROMA_AVAILABLE:
            return

        try:
            self._client = chromadb.PersistentClient(path=path)
            ef = SentenceTransformerEmbeddingFunction(model_name=embedding_model)
            self._collection = self._client.get_or_create_collection(
                name=collection,
                embedding_function=ef,
            )
        except Exception as exc:
            logger.warning("ChromaStore init failed: %s", exc)
            self._client = None
            self._collection = None

    def add(self, text: str, metadata: Optional[Dict[str, Any]] = None) -> str:
        """Store *text* with optional *metadata*. Returns the generated document ID."""
        doc_id = uuid.uuid4().hex
        if self._collection is None:
            logger.debug("ChromaStore.add skipped — backend unavailable")
            return doc_id
        try:
            self._collection.add(
                documents=[text],
                metadatas=[metadata or {}],
                ids=[doc_id],
            )
        except Exception as exc:
            logger.warning("ChromaStore.add error: %s", exc)
        return doc_id

    def search(self, query: str, top_k: int = 5) -> List[Dict[str, Any]]:
        """Semantic search returning up to *top_k* results.

        Returns
        -------
        list of dicts with keys: text, score, metadata.
        """
        if self._collection is None:
            return []
        try:
            results = self._collection.query(
                query_texts=[query],
                n_results=min(top_k, max(1, self.count())),
                include=["documents", "distances", "metadatas"],
            )
            docs = results.get("documents", [[]])[0]
            distances = results.get("distances", [[]])[0]
            metadatas = results.get("metadatas", [[]])[0]
            out: List[Dict[str, Any]] = []
            for text, dist, meta in zip(docs, distances, metadatas):
                # Convert L2 distance to a similarity-like score (0-1)
                score = 1.0 / (1.0 + float(dist))
                out.append({"text": text, "score": score, "metadata": meta or {}})
            return out
        except Exception as exc:
            logger.warning("ChromaStore.search error: %s", exc)
            return []

    def delete(self, ids: List[str]) -> int:
        """Delete documents by ID list. Returns count of IDs submitted."""
        if self._collection is None or not ids:
            return 0
        try:
            self._collection.delete(ids=ids)
        except Exception as exc:
            logger.warning("ChromaStore.delete error: %s", exc)
        return len(ids)

    def count(self) -> int:
        """Return total number of stored documents."""
        if self._collection is None:
            return 0
        try:
            return self._collection.count()
        except Exception as exc:
            logger.warning("ChromaStore.count error: %s", exc)
            return 0

    def health(self) -> bool:
        """Return True if the store is operational."""
        if not _CHROMA_AVAILABLE or self._collection is None:
            return False
        try:
            self._collection.count()
            return True
        except Exception:
            return False


__all__ = ["ChromaStore"]
