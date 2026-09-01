"""Bootstrap unit tests: pure functions, no network, no subprocess."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from ultron import bootstrap


def test_default_model_is_a_valid_ollama_tag():
    name = bootstrap.DEFAULT_MODEL
    assert ":" in name
    left, right = name.split(":", 1)
    assert left and right


def test_ollama_available_is_boolean(monkeypatch):
    monkeypatch.setattr(bootstrap.shutil, "which", lambda _: None)
    assert bootstrap.ollama_available() is False
    monkeypatch.setattr(bootstrap.shutil, "which", lambda _: r"C:\ollama.exe")
    assert bootstrap.ollama_available() is True


def test_ollama_responds_returns_false_when_port_closed(monkeypatch):
    def _refuse(*_args, **_kwargs):
        raise OSError("connection refused")

    monkeypatch.setattr(bootstrap.socket, "create_connection", _refuse)
    assert bootstrap.ollama_responds() is False


def test_installed_models_empty_when_ollama_down(monkeypatch):
    monkeypatch.setattr(bootstrap, "ollama_responds", lambda timeout=2.0: False)
    assert bootstrap.installed_models() == []


def test_installed_models_parses_ollama_payload(monkeypatch):
    monkeypatch.setattr(bootstrap, "ollama_responds", lambda timeout=2.0: True)

    class _Fake:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self):
            return b'{"models":[{"name":"a:1"},{"name":"b:2"},{}]}'

    monkeypatch.setattr(bootstrap.urllib.request, "urlopen", lambda *a, **k: _Fake())
    assert bootstrap.installed_models() == ["a:1", "b:2"]


class _FakeBody:
    """Minimal stand-in for the downloader's HTTP response object."""

    def __init__(self, body: bytes):
        self._buf = memoryview(body)
        self._pos = 0
        self.status = 200
        self.headers = {"Content-Length": str(len(body))}

    def read(self, n: int) -> bytes:
        chunk = bytes(self._buf[self._pos : self._pos + n])
        self._pos += len(chunk)
        return chunk

    def close(self) -> None:
        pass


def test_installer_download_writes_and_returns_path(tmp_path, monkeypatch):
    from ultron import downloader

    payload = b"\x00" * 6_000_000
    monkeypatch.setattr(downloader, "_open", lambda *_a, **_k: _FakeBody(payload))
    dest = bootstrap.download_ollama_installer(tmp_path / "d")
    assert dest.exists() and dest.stat().st_size == len(payload)


def test_installer_download_rejects_truncated(tmp_path, monkeypatch):
    from ultron import downloader

    monkeypatch.setattr(downloader, "_open", lambda *_a, **_k: _FakeBody(b"tiny"))
    try:
        bootstrap.download_ollama_installer(tmp_path / "d")
    except bootstrap.DownloadFailed:
        return
    raise AssertionError("expected DownloadFailed")


def test_install_ollama_silently_wraps_nonzero_exit(tmp_path, monkeypatch):
    installer = tmp_path / "OllamaSetup.exe"
    installer.write_bytes(b"stub")

    fake = MagicMock(returncode=42, stderr="boom", stdout="")
    with patch.object(bootstrap.subprocess, "run", return_value=fake):
        try:
            bootstrap.install_ollama_silently(installer)
        except bootstrap.InstallFailed as exc:
            assert "42" in str(exc)
            return
    raise AssertionError("expected InstallFailed")
