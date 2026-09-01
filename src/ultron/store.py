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

    def iter_digests(self):
        """Yield every stored blob digest."""
        if not self.root.exists():
            return
        for shard in self.root.iterdir():
            if not shard.is_dir():
                continue
            for blob in shard.iterdir():
                if _DIGEST_RE.match(blob.name):
                    yield blob.name

    def gc(self, referenced: set[str]) -> dict:
        """Mark-and-sweep: delete every blob whose digest is not in `referenced`.

        Content-addressed and append-only, so a blob that nothing points at can
        never be reached again. Returns {"deleted", "kept", "freed_bytes"}.
        """
        deleted = kept = freed = 0
        for digest in list(self.iter_digests()):
            if digest in referenced:
                kept += 1
                continue
            path = self._path(digest)
            try:
                freed += path.stat().st_size
                path.unlink()
                deleted += 1
            except OSError:
                pass
        return {"deleted": deleted, "kept": kept, "freed_bytes": freed}

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
