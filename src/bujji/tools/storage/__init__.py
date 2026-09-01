"""Storage primitive â€” persistent searchable storage."""

from __future__ import annotations

# Always-available backend
import bujji.tools.storage.sqlite  # noqa: F401

# Optional backends â€” import to trigger registration
try:
    import bujji.tools.storage.bm25  # noqa: F401
except ImportError:
    pass

try:
    import bujji.tools.storage.faiss_backend  # noqa: F401
except ImportError:
    pass

try:
    import bujji.tools.storage.colbert_backend  # noqa: F401
except ImportError:
    pass

try:
    import bujji.tools.storage.hybrid  # noqa: F401
except ImportError:
    pass

try:
    import bujji.tools.storage.dense  # noqa: F401
except ImportError:
    pass

from bujji.tools.storage._stubs import MemoryBackend, RetrievalResult
from bujji.tools.storage.chunking import Chunk, ChunkConfig, chunk_text
from bujji.tools.storage.context import ContextConfig, inject_context
from bujji.tools.storage.ingest import ingest_path, read_document

__all__ = [
    "Chunk",
    "ChunkConfig",
    "ContextConfig",
    "MemoryBackend",
    "RetrievalResult",
    "chunk_text",
    "inject_context",
    "ingest_path",
    "read_document",
]
