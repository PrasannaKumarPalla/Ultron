"""Content-addressed blob store: append-only, local, zero dependencies.

Blobs live under <root>/<aa>/<sha256>. Writes are atomic (tmp + rename) and
idempotent — putting the same bytes twice yields the same address and touches
nothing. Payload references travel as {"blob": <sha>} and are inlined with
resolve().
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

BLOB_SPILL_THRESHOLD = 64 * 1024

_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


class BlobStore:
    def __init__(self, root: Path):
        self.root = Path(root)

    def put(self, data: bytes) -> str:
        digest = hashlib.sha256(data).hexdigest()
        path = self._path(digest)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(f"{digest}.tmp-{os.getpid()}")
            tmp.write_bytes(data)
            os.replace(tmp, path)
        return digest

    def put_text(self, text: str) -> str:
        return self.put(text.encode("utf-8"))

    def get(self, digest: str) -> bytes | None:
        if not _DIGEST_RE.match(digest or ""):
            return None
        try:
            return self._path(digest).read_bytes()
        except OSError:
            return None

    def has(self, digest: str) -> bool:
        return _DIGEST_RE.match(digest or "") is not None and self._path(digest).exists()

    def resolve(self, payload: dict) -> dict:
        """Return the payload with any {"blob": <sha>} reference inlined."""
        ref = payload.get("blob")
        if isinstance(ref, str):
            data = self.get(ref)
            if data is not None:
                return json.loads(data.decode("utf-8"))
            return {"blob_missing": ref}
        return payload

    def _path(self, digest: str) -> Path:
        return self.root / digest[:2] / digest
