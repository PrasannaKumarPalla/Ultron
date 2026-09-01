"""First-run bootstrap for Ultron: install Ollama on demand + pull a model.

The desktop shell wires these into ctypes-backed dialogs. The functions
here are pure IO — no UI. They raise a small typed error hierarchy so
callers can react without matching on message strings.
"""

from __future__ import annotations

import logging
import os
import shutil
import socket
import subprocess
import time
import urllib.request
from pathlib import Path

OLLAMA_INSTALLER_URL = "https://ollama.com/download/OllamaSetup.exe"
DEFAULT_MODEL = "qwen3.6:27b"
FALLBACK_MODEL = "qwen3:8b"


class BootstrapError(RuntimeError):
    """Base type for bootstrap failures."""


class DownloadFailed(BootstrapError):
    pass


class InstallFailed(BootstrapError):
    pass


class PullFailed(BootstrapError):
    pass


def ollama_available() -> bool:
    return shutil.which("ollama") is not None


def ollama_responds(timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", 11434), timeout=timeout):
            return True
    except OSError:
        return False


def installed_models() -> list[str]:
    if not ollama_responds():
        return []
    try:
        # Ollama's fixed loopback endpoint is not user-controlled and does not provide TLS.
        with urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=4) as response:  # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected,python.lang.security.audit.insecure-transport.urllib.insecure-urlopen.insecure-urlopen
            import json

            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError):
        return []
    return [entry["name"] for entry in payload.get("models", []) if entry.get("name")]


def download_ollama_installer(dest_dir: Path, on_progress=None) -> Path:
    """Fetch OllamaSetup.exe. Streamed, resumable, retried (see ultron.downloader).

    Ollama does not publish a stable per-release checksum URL, so integrity here
    rests on HTTPS plus a minimum-size guard; `on_progress` receives
    `ultron.downloader.Progress` updates for a UI.
    """
    from ultron import downloader

    dest = dest_dir / "OllamaSetup.exe"
    logging.info("Downloading Ollama installer to %s", dest)
    try:
        return downloader.download(
            OLLAMA_INSTALLER_URL, dest,
            min_bytes=5_000_000, on_progress=on_progress,
        )
    except downloader.DownloadError as exc:
        raise DownloadFailed(f"Could not download Ollama installer: {exc}") from exc


def install_ollama_silently(installer: Path) -> None:
    logging.info("Running Ollama silent install: %s", installer)
    try:
        completed = subprocess.run(  # nosec B603
            [str(installer), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"],
            capture_output=True,
            text=True,
            timeout=600,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise InstallFailed(f"Ollama installer did not complete: {exc}") from exc
    if completed.returncode != 0:
        raise InstallFailed(
            f"Ollama installer exited with code {completed.returncode}: "
            f"{completed.stderr.strip() or completed.stdout.strip()}"
        )
    # Fresh install adds Ollama to PATH via setx; the current process still
    # has the old PATH. Poke the well-known install dir so `which` finds it.
    local_app = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    candidate = local_app / "Programs" / "Ollama" / "ollama.exe"
    if candidate.exists() and str(candidate.parent) not in os.environ["PATH"].split(os.pathsep):
        os.environ["PATH"] = f"{candidate.parent}{os.pathsep}{os.environ['PATH']}"


def wait_for_ollama_ready(timeout_s: float = 60.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if ollama_responds():
            return
        time.sleep(1)
    raise InstallFailed("Ollama did not become reachable after install.")


def pull_model(name: str, show_console: bool = True) -> None:
    """Run `ollama pull <name>` synchronously. Optionally in a visible console."""
    if not ollama_available():
        raise PullFailed("Ollama executable not found on PATH.")
    if show_console:
        # Open a new console window so the user sees pull progress; keep it
        # open after completion so they can read any final error.
        creationflags = subprocess.CREATE_NEW_CONSOLE  # type: ignore[attr-defined]
        try:
            completed = subprocess.run(  # nosec B603 B607
                ["cmd", "/c", f"ollama pull {name} & pause"],
                creationflags=creationflags,
                timeout=None,
            )
        except OSError as exc:
            raise PullFailed(f"Could not launch model pull: {exc}") from exc
    else:
        try:
            completed = subprocess.run(  # nosec B603 B607
                ["ollama", "pull", name],
                capture_output=True,
                text=True,
                timeout=None,
            )
        except OSError as exc:
            raise PullFailed(f"Could not launch model pull: {exc}") from exc
    if completed.returncode != 0:
        raise PullFailed(f"`ollama pull {name}` exited with code {completed.returncode}.")
