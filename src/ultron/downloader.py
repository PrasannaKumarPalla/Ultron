"""Resumable, checksum-verified file download.

Layer 2 of the prerequisite installer (decision 0004). Used for the Ollama
installer today; model weights are pulled by ``ollama pull`` which does its own
content-addressed verification, so the sha256 path here is for any future direct
download.

- Streams in chunks (never loads the whole body into memory).
- Resumes an interrupted transfer via an HTTP ``Range`` request against a
  ``<dest>.part`` sidecar file.
- Retries transient network errors with backoff, resuming each attempt.
- Verifies ``sha256`` and/or a minimum byte count before promoting ``.part`` to
  the final path with an atomic ``os.replace``.
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

_CHUNK = 1 << 20  # 1 MiB


class DownloadError(RuntimeError):
    """Download failed after exhausting retries, or verification failed."""


@dataclass(frozen=True)
class Progress:
    downloaded: int
    total: int | None

    @property
    def fraction(self) -> float | None:
        if not self.total:
            return None
        return min(1.0, self.downloaded / self.total)


ProgressCb = Callable[[Progress], None]


def _open(request: urllib.request.Request, timeout: float):
    # Isolated so tests can substitute a fake transport. The URL is caller-supplied
    # and expected to be https:// (the one real caller passes a module constant).
    return urllib.request.urlopen(request, timeout=timeout)  # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


def download(
    url: str,
    dest: str | os.PathLike[str],
    *,
    sha256: str | None = None,
    min_bytes: int | None = None,
    retries: int = 3,
    timeout: float = 60.0,
    on_progress: ProgressCb | None = None,
    opener: Callable[[urllib.request.Request, float], object] | None = None,
) -> Path:
    """Download ``url`` to ``dest``. Returns the final path.

    Raises :class:`DownloadError` on repeated failure or a checksum / size
    mismatch. A failed verification leaves the ``.part`` file in place for
    inspection; a promoted file is always complete and verified.
    """
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    _get = opener or _open

    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        have = part.stat().st_size if part.exists() else 0
        headers = {"Range": f"bytes={have}-"} if have else {}
        try:
            response = _get(urllib.request.Request(url, headers=headers), timeout)
            status = getattr(response, "status", 200)
            resp_headers = getattr(response, "headers", {}) or {}

            if have and status != 206:
                # Server ignored the range — start over.
                logger.info("range not honoured (HTTP %s); restarting download", status)
                part.unlink(missing_ok=True)
                have = 0

            declared = resp_headers.get("Content-Length")
            total = (int(declared) + have) if declared is not None else None

            mode = "ab" if have else "wb"
            with part.open(mode) as sink:
                downloaded = have
                while True:
                    block = response.read(_CHUNK)
                    if not block:
                        break
                    sink.write(block)
                    downloaded += len(block)
                    if on_progress:
                        on_progress(Progress(downloaded, total))
            getattr(response, "close", lambda: None)()

            if min_bytes is not None and part.stat().st_size < min_bytes:
                raise DownloadError(
                    f"{dest.name} is {part.stat().st_size} bytes, expected at least {min_bytes}"
                )
            if sha256 is not None:
                actual = _sha256(part)
                if actual.lower() != sha256.lower():
                    raise DownloadError(
                        f"{dest.name} sha256 mismatch: got {actual}, expected {sha256}"
                    )

            os.replace(part, dest)
            return dest

        except DownloadError:
            raise  # verification failures are terminal, not retried
        except (urllib.error.URLError, OSError, ValueError) as exc:
            last_error = exc
            logger.warning("download attempt %d/%d failed: %s", attempt, retries, exc)
            if attempt < retries:
                time.sleep(min(2 ** attempt, 10))

    raise DownloadError(f"could not download {url} after {retries} attempts: {last_error}")


__all__ = ["download", "Progress", "DownloadError"]
