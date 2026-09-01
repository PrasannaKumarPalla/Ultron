"""Downloader tests: fake transport, no real network."""

from __future__ import annotations

import hashlib

import pytest

from ultron import downloader
from ultron.downloader import DownloadError, Progress


class _FakeResponse:
    def __init__(self, body: bytes, *, status: int = 200, content_length: bool = True):
        self._buf = memoryview(body)
        self._pos = 0
        self.status = status
        self.headers = {"Content-Length": str(len(body))} if content_length else {}

    def read(self, n: int) -> bytes:
        chunk = bytes(self._buf[self._pos : self._pos + n])
        self._pos += len(chunk)
        return chunk

    def close(self) -> None:  # pragma: no cover - trivial
        pass


def _opener_for(*responses):
    """Return an opener callable that yields the given responses in order."""
    seq = list(responses)

    def _open(request, timeout):  # noqa: ARG001
        if not seq:
            raise AssertionError("opener called more times than expected")
        item = seq.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    return _open


def test_fresh_download_writes_final_file(tmp_path):
    body = b"hello world" * 1000
    dest = tmp_path / "f.bin"
    out = downloader.download("https://x/f", dest, opener=_opener_for(_FakeResponse(body)))
    assert out == dest
    assert dest.read_bytes() == body
    assert not dest.with_name("f.bin.part").exists()


def test_progress_callback_reports_total_and_monotonic_downloaded(tmp_path):
    body = b"z" * (3 << 20)  # 3 MiB -> multiple chunks
    seen: list[Progress] = []
    downloader.download(
        "https://x/f", tmp_path / "f", opener=_opener_for(_FakeResponse(body)),
        on_progress=seen.append,
    )
    assert seen[-1].downloaded == len(body)
    assert seen[-1].total == len(body)
    assert seen[-1].fraction == 1.0
    assert [p.downloaded for p in seen] == sorted(p.downloaded for p in seen)


def test_resume_appends_to_existing_part(tmp_path):
    full = b"abcdefghij" * 500
    dest = tmp_path / "f.bin"
    part = dest.with_name("f.bin.part")
    part.write_bytes(full[:1200])  # simulate an interrupted transfer

    resumed = _FakeResponse(full[1200:], status=206)
    out = downloader.download("https://x/f", dest, opener=_opener_for(resumed))
    assert out.read_bytes() == full


def test_server_ignoring_range_restarts_from_zero(tmp_path):
    full = b"0123456789" * 400
    dest = tmp_path / "f.bin"
    dest.with_name("f.bin.part").write_bytes(b"garbage-partial")

    # status 200 despite the Range header -> downloader must discard the partial
    out = downloader.download(
        "https://x/f", dest, opener=_opener_for(_FakeResponse(full, status=200))
    )
    assert out.read_bytes() == full


def test_sha256_mismatch_raises_and_keeps_part(tmp_path):
    body = b"payload"
    dest = tmp_path / "f.bin"
    with pytest.raises(DownloadError, match="sha256 mismatch"):
        downloader.download(
            "https://x/f", dest, sha256="deadbeef" * 8,
            opener=_opener_for(_FakeResponse(body)),
        )
    assert dest.with_name("f.bin.part").exists()
    assert not dest.exists()


def test_sha256_match_promotes_file(tmp_path):
    body = b"verify me"
    digest = hashlib.sha256(body).hexdigest()
    dest = tmp_path / "f.bin"
    out = downloader.download(
        "https://x/f", dest, sha256=digest, opener=_opener_for(_FakeResponse(body))
    )
    assert out.read_bytes() == body


def test_min_bytes_guard_rejects_truncated_body(tmp_path):
    with pytest.raises(DownloadError, match="at least"):
        downloader.download(
            "https://x/f", tmp_path / "f", min_bytes=1_000_000,
            opener=_opener_for(_FakeResponse(b"tiny")),
        )


def test_transient_error_is_retried_then_succeeds(tmp_path):
    body = b"eventually" * 100
    dest = tmp_path / "f.bin"
    opener = _opener_for(OSError("connection reset"), _FakeResponse(body))
    out = downloader.download("https://x/f", dest, retries=3, opener=opener)
    assert out.read_bytes() == body


def test_all_retries_failing_raises_downloaderror(tmp_path, monkeypatch):
    monkeypatch.setattr(downloader.time, "sleep", lambda _s: None)
    opener = _opener_for(OSError("a"), OSError("b"), OSError("c"))
    with pytest.raises(DownloadError, match="after 3 attempts"):
        downloader.download("https://x/f", tmp_path / "f", retries=3, opener=opener)
