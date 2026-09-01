from __future__ import annotations

import os
import logging
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path


APP_NAME = "Ultron"
OLLAMA_DOWNLOAD_URL = "https://ollama.com/download"


class OllamaMissing(RuntimeError):
    """Ollama is not installed on PATH; caller should offer the download link."""


def _msgbox(text: str, title: str, style: int) -> int:
    import ctypes

    return int(ctypes.windll.user32.MessageBoxW(0, text, title, style))


def _confirm_yesno(text: str, title: str, icon: int = 0x40) -> bool:
    # 0x04 = MB_YESNO; 0x40 = MB_ICONINFORMATION; IDYES = 6.
    return _msgbox(text, title, 0x04 | icon) == 6


def _info(text: str, title: str) -> None:
    _msgbox(text, title, 0x40)  # MB_ICONINFORMATION


def _warn(text: str, title: str) -> None:
    _msgbox(text, title, 0x30)  # MB_ICONWARNING


def _require(report, key: str):
    return next((r for r in report.requirements if r.key == key), None)


def bootstrap_ollama_and_models() -> None:
    """Interactive first-run bootstrap, driven by the preflight report.

    Called before the main server starts. Detects the machine, then walks the
    prerequisite report: warns (overridably) on low RAM / disk, installs Ollama
    if missing, and offers to pull the hardware-matched recommended model. All
    dialogs are optional; declining falls back to the legacy error path.
    """
    from ultron import bootstrap, preflight

    report = preflight.resolve(preflight.detect_machine())

    for key in ("ram", "disk"):
        req = _require(report, key)
        if req and req.blocking and req.status == "insufficient":
            if not _confirm_yesno(
                f"{req.label}: {req.detail}\n\n"
                "Ultron may not run well on this machine. Continue anyway?",
                "Ultron: system check", icon=0x30,
            ):
                raise SystemExit(1)

    if (_require(report, "ollama") or None) and _require(report, "ollama").status != "ok":
        prompt = (
            "Ollama is required to run Ultron and was not found.\n\n"
            "Download and install it now? (Resumable, one-click install.)\n\n"
            "Choose No to open the download page in your browser instead."
        )
        if _confirm_yesno(prompt, "Ultron: install Ollama?", icon=0x30):
            try:
                installer = bootstrap.download_ollama_installer(user_data_dir() / "downloads")
                _info(
                    "Installer downloaded. Windows may prompt for permission "
                    "on the next step; accept it.",
                    "Ultron: installing Ollama",
                )
                bootstrap.install_ollama_silently(installer)
                bootstrap.wait_for_ollama_ready()
                _info("Ollama installed and ready.", "Ultron")
            except bootstrap.BootstrapError as exc:
                _warn(
                    f"Automatic install failed: {exc}\n\n"
                    "Opening the download page instead.",
                    "Ultron",
                )
                import webbrowser

                webbrowser.open(OLLAMA_DOWNLOAD_URL)
                raise SystemExit(1) from exc
        else:
            import webbrowser

            webbrowser.open(OLLAMA_DOWNLOAD_URL)
            raise SystemExit(1)
        report = preflight.resolve(preflight.detect_machine())  # Ollama now present

    model_req = _require(report, "model")
    if model_req and model_req.status == "missing" and report.recommended_model:
        tag = report.recommended_model
        size_gb = (model_req.download_mb or 0) / 1024
        note = "  (CPU — expect slow responses)" if report.degraded else ""
        prompt = (
            f"No local chat model is installed.\n\n"
            f"Pull {tag}{note} now?  (~{size_gb:.0f} GB)\n"
            f"Runs in a separate window; you can keep using Ultron while it downloads.\n\n"
            f"{report.model_reason}"
        )
        if _confirm_yesno(prompt, "Ultron: pull a model?"):
            subprocess.Popen(  # noqa: S603, S607 - user-consented model pull
                ["cmd", "/c", f"ollama pull {tag} & pause"],
                creationflags=subprocess.CREATE_NEW_CONSOLE,  # type: ignore[attr-defined]
            )
    elif model_req and model_req.status == "insufficient":
        _warn(model_req.detail, "Ultron: cannot recommend a model")


def app_icon() -> str | None:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
    for candidate in (base / "assets" / "ultron.ico", base / "assets" / "ultron-icon-dark.png"):
        if candidate.exists():
            return str(candidate)
    return None


def configure_logging() -> Path:
    log_path = user_data_dir() / "ultron.log"
    logging.basicConfig(
        filename=log_path,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        force=True,
    )
    return log_path


def user_data_dir() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    path = base / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def configure_desktop_environment(port: int) -> None:
    data_dir = user_data_dir()
    projects_dir = Path.home() / "Documents" / "UltronProjects"
    projects_dir.mkdir(parents=True, exist_ok=True)
    defaults = {
        "ULTRON_DESKTOP_MODE": "1",
        "ULTRON_ENV": "desktop",
        "ULTRON_HOST": "127.0.0.1",
        "ULTRON_PORT": str(port),
        "ULTRON_DATABASE_PATH": str(data_dir / "ultron.db"),
        "ULTRON_CHECKPOINT_PATH": str(data_dir / "checkpoints.db"),
        "ULTRON_PROJECTS_ROOT": str(projects_dir),
        "ULTRON_EXECUTION_PROVIDER": "local",
    }
    for name, value in defaults.items():
        os.environ.setdefault(name, value)


def available_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def ensure_ollama() -> None:
    try:
        # Ollama's fixed loopback endpoint is not user-controlled and does not provide TLS.
        with urllib.request.urlopen("http://127.0.0.1:11434/api/version", timeout=2):  # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected,python.lang.security.audit.insecure-transport.urllib.insecure-urlopen.insecure-urlopen
            return
    except OSError:
        pass
    executable = shutil.which("ollama")
    if not executable:
        raise OllamaMissing(
            "Ollama is required and was not found on PATH.\n\n"
            "Download it from https://ollama.com/download and restart Ultron.\n\n"
            "After installing, pull a chat model (for example: `ollama pull qwen3.6:27b`)."
        )
    subprocess.Popen(
        [executable, "serve"],
        creationflags=subprocess.CREATE_NO_WINDOW,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            # Ollama's fixed loopback endpoint is not user-controlled and does not provide TLS.
            with urllib.request.urlopen("http://127.0.0.1:11434/api/version", timeout=2):  # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected,python.lang.security.audit.insecure-transport.urllib.insecure-urlopen.insecure-urlopen
                return
        except OSError:
            time.sleep(0.4)
    raise RuntimeError("Ollama did not start within 20 seconds.")


def wait_for_server(url: str, timeout: int = 30) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            # url is assembled from a fixed loopback host and an OS-assigned local port.
            with urllib.request.urlopen(f"{url}health", timeout=2) as response:  # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
                if response.status == 200:
                    return
        except OSError:
            time.sleep(0.25)
    raise RuntimeError("Ultron did not become ready in time.")


def main() -> None:
    configure_logging()
    logging.info("Ultron desktop startup began")
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Ultron.Desktop")
        except Exception:
            pass
    port = available_port()
    configure_desktop_environment(port)
    bootstrap_ollama_and_models()
    ensure_ollama()
    logging.info("Ollama is ready; starting internal service on port %s", port)

    import uvicorn
    import webview

    from ultron.api import app

    url = f"http://127.0.0.1:{port}/"
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", access_log=False)
    )
    server.install_signal_handlers = lambda: None
    server_thread = threading.Thread(target=server.run, name="ultron-server", daemon=True)
    server_thread.start()
    try:
        wait_for_server(url)
        logging.info("Internal service is ready; opening native window")
        webview.create_window(
            "Ultron — Local AI Studio",
            url,
            width=1440,
            height=940,
            min_size=(980, 700),
            resizable=True,
            text_select=True,
        )
        icon = app_icon()
        if icon is not None:
            webview.start(private_mode=False, icon=icon)
        else:
            webview.start(private_mode=False)
    finally:
        logging.info("Ultron desktop shutdown began")
        server.should_exit = True
        server_thread.join(timeout=10)


if __name__ == "__main__":
    try:
        main()
    except OllamaMissing as exc:
        import ctypes

        logging.error("Ollama missing at startup")
        MB_YESNO = 0x04
        MB_ICONWARNING = 0x30
        IDYES = 6
        prompt = f"{exc}\n\nOpen the Ollama download page now?"
        result = ctypes.windll.user32.MessageBoxW(
            0, prompt, "Ultron needs Ollama", MB_YESNO | MB_ICONWARNING
        )
        if result == IDYES:
            import webbrowser

            webbrowser.open(OLLAMA_DOWNLOAD_URL)
        raise SystemExit(1)
    except Exception as exc:
        import ctypes

        logging.exception("Ultron startup failed")
        ctypes.windll.user32.MessageBoxW(0, str(exc), "Ultron could not start", 0x10)
        raise
