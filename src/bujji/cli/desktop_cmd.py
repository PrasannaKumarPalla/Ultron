"""Native desktop launcher for the assistant control core."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from contextlib import closing
from pathlib import Path

import click
import httpx

from bujji.brand import get_branding

_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 8000


def _is_port_open(host: str, port: int) -> bool:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        sock.settimeout(0.2)
        return sock.connect_ex((host, port)) == 0


def _available_port(host: str, preferred_port: int) -> int:
    """Return the preferred port when free, otherwise an OS-assigned port.

    The desktop must never kill an unrelated process merely because that
    process owns B.U.J.J.I's preferred port.
    """
    if not _is_port_open(host, preferred_port):
        return preferred_port
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def _wait_for_server(url: str, timeout_s: float = 30.0) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            with httpx.Client(timeout=1.0) as client:
                response = client.get(f"{url}/health")
                if response.is_success:
                    return True
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    return False


def _build_server_command(host: str, port: int) -> list[str]:
    command_name = get_branding().command_name
    if getattr(sys, "frozen", False):
        sibling_cli = Path(sys.executable).with_name(f"{command_name}.exe")
        if sibling_cli.exists() and sibling_cli.resolve() != Path(sys.executable).resolve():
            return [
                str(sibling_cli),
                "serve",
                "--host",
                host,
                "--port",
                str(port),
            ]

    return [
        sys.executable,
        "-m",
        "bujji.cli",
        "serve",
        "--host",
        host,
        "--port",
        str(port),
    ]


def _kill_port_listeners(port: int) -> None:
    """Terminate whatever is listening on *port* (stale assistant instances)."""
    import time as _time

    pids: set[int] = set()
    try:
        out = subprocess.run(
            ["netstat", "-ano", "-p", "tcp"],
            capture_output=True, text=True, timeout=15,
        ).stdout
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[3].upper() == "LISTENING" and parts[1].endswith(f":{port}"):
                try:
                    pid = int(parts[4])
                    if pid > 0 and pid != os.getpid():
                        pids.add(pid)
                except ValueError:
                    pass
        for pid in pids:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/F", "/T"],
                capture_output=True, timeout=15,
            )
        if pids:
            _time.sleep(2)  # let the OS release the socket
    except Exception:
        pass


def _spawn_server(host: str, port: int) -> subprocess.Popen[str]:
    branding = get_branding()
    creationflags = 0
    if sys.platform == "win32":
        creationflags = subprocess.CREATE_NO_WINDOW

    if getattr(sys, "frozen", False):
        cwd = str(Path(sys.executable).resolve().parent)
    else:
        cwd = str(Path(__file__).resolve().parents[3])

    env = {
        **os.environ,
        "BUJJI_PRODUCT_NAME": branding.product_name,
        "BUJJI_COMMAND_NAME": branding.command_name,
        "BUJJI_INSTALL_DIR": branding.install_dir_name,
        "BUJJI_DISPLAY_NAME": branding.display_name,
        "BUJJI_WAKE_WORD": branding.wake_word,
    }

    # Server output goes to ~/.bujji/server.log — DEVNULL made every backend
    # failure (voice, TTS, briefing) invisible and undebuggable.
    log_path = Path.home() / ".bujji" / "server.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = open(log_path, "a", encoding="utf-8", errors="replace")

    return subprocess.Popen(
        _build_server_command(host, port),
        cwd=cwd,
        creationflags=creationflags,
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        text=True,
    )


def _ensure_ollama() -> None:
    """Start Ollama in the background (hidden) if it isn't already running."""
    import shutil

    candidates = [
        os.path.expandvars(r"%LOCALAPPDATA%\AMD\AI_Bundle\Ollama\ollama.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\Ollama\ollama.exe"),
        os.path.expandvars(r"%ProgramFiles%\Ollama\ollama.exe"),
    ]
    ollama_exe = next((p for p in candidates if os.path.exists(p)), None)
    if ollama_exe is None:
        ollama_exe = shutil.which("ollama")
    if ollama_exe is None:
        return

    try:
        import httpx
        with httpx.Client(timeout=1.0) as c:
            c.get("http://localhost:11434/api/tags")
        return  # already running
    except Exception:
        pass

    creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    subprocess.Popen(
        [ollama_exe, "serve"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creationflags,
    )
    import time
    time.sleep(4)


def _apply_win32_icon(ico_path: str, window_title: str) -> None:
    """Find the webview HWND by title and stamp our icon onto it + taskbar."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        # Make Windows treat this as a standalone app, not python.exe
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "Bujji.Assistant.Desktop"
        )

        WM_SETICON = 0x0080
        IMAGE_ICON = 1
        LR_LOADFROMFILE = 0x10
        LR_DEFAULTSIZE = 0x40

        # Load at concrete small sizes — LoadImageW frequently fails to decode a
        # 256x256 PNG-compressed .ico entry, which would leave the taskbar
        # (ICON_BIG) slot empty and Windows falls back to a default icon.
        def _load(px: int):
            h = ctypes.windll.user32.LoadImageW(
                None, ico_path, IMAGE_ICON, px, px, LR_LOADFROMFILE
            )
            if not h:
                h = ctypes.windll.user32.LoadImageW(
                    None, ico_path, IMAGE_ICON, 0, 0, LR_LOADFROMFILE | LR_DEFAULTSIZE
                )
            return h

        hicon_big = _load(48)
        hicon_small = _load(16)

        hwnd = ctypes.windll.user32.FindWindowW(None, window_title)
        if hwnd:
            if hicon_big:
                ctypes.windll.user32.SendMessageW(hwnd, WM_SETICON, 1, hicon_big)   # ICON_BIG
            if hicon_small:
                ctypes.windll.user32.SendMessageW(hwnd, WM_SETICON, 0, hicon_small) # ICON_SMALL
    except Exception:
        pass


def launch_desktop(host: str = _DEFAULT_HOST, port: int = _DEFAULT_PORT) -> None:
    """Launch the desktop shell and local API server if needed."""
    import webview  # noqa: PLC0415
    branding = get_branding()

    _ensure_ollama()

    started_server = False
    process: subprocess.Popen[str] | None = None
    app_url = f"http://{host}:{port}"

    # A previous/stale instance on the port would serve OLD code while this
    # launch silently attaches to it — always reclaim and start fresh.
    port = _available_port(host, port)
    app_url = f"http://{host}:{port}"
    process = _spawn_server(host, port)
    started_server = True

    if not _wait_for_server(app_url):
        if process is not None:
            process.terminate()
        raise click.ClickException(
            f"Could not start {branding.product_name} server at {app_url}"
        )

    icon_path = str(Path(__file__).resolve().parents[1] / "server" / "static" / "bujji.ico")
    if not os.path.exists(icon_path):
        icon_path = None

    # Set AppUserModelID early so taskbar groups correctly
    if sys.platform == "win32" and icon_path:
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "Bujji.Assistant.Desktop"
            )
        except Exception:
            pass

    def _on_started():
        if icon_path:
            # Give EdgeChromium a moment to create its HWND
            time.sleep(0.5)
            _apply_win32_icon(icon_path, branding.display_name)

    try:
        webview.create_window(
            branding.display_name,
            app_url,
            width=1680,
            height=980,
            min_size=(1280, 760),
            background_color="#04070c",
            text_select=False,
            frameless=False,
            easy_drag=False,
        )
        # Use Edge/WebView2 on Windows for best mic + audio support
        webview.start(func=_on_started, debug=False)
    finally:
        if started_server and process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()


@click.command("desktop")
@click.option("--host", default=_DEFAULT_HOST, show_default=True, help="Local bind host.")
@click.option("--port", default=_DEFAULT_PORT, show_default=True, type=int, help="Local bind port.")
def desktop(host: str, port: int) -> None:
    """Launch the control core in a native desktop window."""
    try:
        import webview  # noqa: F401
    except ImportError:
        raise click.ClickException(
            "Desktop dependencies are missing. Run: uv sync --extra desktop"
        ) from None
    launch_desktop(host=host, port=port)
