"""Embedded Bujji Control Core sidecar.

Serves Bujji's rich "Control Core" UI (Central Core, Memory/Brain, Model
Router, Voice, Tools, Research, etc.) as a full-screen view inside Ultron.

We reuse Bujji's own ``create_app`` factory in-process (same approach as
``bujji/cli/serve.py`` and used by Bujji's own test suite), hosting it with an
embedded uvicorn server on a dedicated loopback port. Running in-process means
the same code path works in the development venv AND inside the frozen
``dist/Ultron.exe`` (no separate ``python.exe`` is needed for a subprocess).

The sidecar is started by Ultron's FastAPI ``lifespan`` on boot and shut down
on exit, so both the browser and desktop modes get the Control Core tab.
"""

from __future__ import annotations

import logging
import socket
import threading
import time

logger = logging.getLogger(__name__)

BUJJI_UI_HOST = "127.0.0.1"
BUJJI_UI_PORT = 8321  # legacy name only; the actual port is picked at runtime

_url: str | None = None
_server: "uvicorn.Server | None" = None
_server_thread: threading.Thread | None = None


def _port_free(host: str, port: int) -> bool:
    """Return True if the port is not currently bound (loopback)."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        probe.close()


def _reserve_free_port(host: str) -> int:
    """Ask the OS for a free loopback port (ephemeral-bind-then-release)."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind((host, 0))
        return probe.getsockname()[1]
    finally:
        probe.close()


def is_running() -> bool:
    """True only while the uvicorn server is both started AND its thread lives."""
    return (
        _server is not None
        and bool(getattr(_server, "started", False))
        and _server_thread is not None
        and _server_thread.is_alive()
    )


def ui_url() -> str | None:
    """Reachable base URL of the embedded Control Core, or None."""
    return _url if is_running() else None


def status() -> dict:
    """What ``GET /api/bujji-core`` reports to the dashboard iframe loader."""
    url = ui_url()
    return {"ok": bool(url), "url": url}


def wait_until_reachable(host: str, port: int, timeout_s: float = 20.0) -> bool:
    """Block until the loopback port accepts TCP connections (or timeout)."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.settimeout(0.4)
        try:
            if probe.connect_ex((host, port)) == 0:
                return True
        except OSError:
            pass
        finally:
            probe.close()
        if _server_thread is not None and not _server_thread.is_alive():
            return False  # thread died; don't spin for the full timeout
        time.sleep(0.15)
    return False


def _reachable_model(engine: object) -> str:
    """Pick the engine's first reachable model, else fall back to a default."""
    try:
        models = list(engine.list_models())
    except Exception as exc:
        logger.warning("Bujji engine model list failed: %s", exc)
        models = []
    if models:
        return models[0]
    return "qwen3.6:27b"


class _FrameFriendly:
    """Strip ``X-Frame-Options`` so the Ultron dashboard may embed us.

    Bujji's server hardens responses with ``X-Frame-Options: DENY`` — great for
    a public site, fatal for an embedded sidecar (browsers refuse to render the
    iframe, showing a white/blocked frame). This sidecar only ever serves
    Ultron's own UI on loopback, so dropping that single header is safe; every
    other hardening header (nosniff, HSTS, CSP) is left untouched.
    """

    _STRIP = {b"x-frame-options"}

    def __init__(self, inner):  # noqa: ANN001
        self.inner = inner

    async def __call__(self, scope, receive, send):  # noqa: ANN001
        if scope["type"] != "http":
            await self.inner(scope, receive, send)
            return

        async def send_wrapped(message):
            if message["type"] == "http.response.start":
                headers = [(k, v) for k, v in message.get("headers", [])
                           if k.lower() not in self._STRIP]
                message = dict(message)
                message["headers"] = headers
            await send(message)

        await self.inner(scope, receive, send_wrapped)


_hosted_cfg: dict | None = None


def configure_hosted(base_url: str | None, api_key: str | None) -> None:
    """Point the Control Core at the OmniRoute sidecar. Pass base_url=None to
    revert to local Ollama on the next (re)start."""
    global _hosted_cfg
    _hosted_cfg = {"base_url": base_url, "api_key": api_key} if base_url else None


_engine_name: str = ""


def current_engine() -> str:
    """Engine the running Control Core was last built with ('ollama' | 'omniroute')."""
    return _engine_name if is_running() else ""


def reload() -> None:
    """Restart the Control Core so an engine change (local <-> OmniRoute) takes
    effect. Called when the operator flips the provider selector."""
    stop()
    start()


def _build_engine():
    """(engine, model, engine_name) — OmniRoute when configured and reachable,
    else local Ollama."""
    if _hosted_cfg:
        try:
            import httpx

            from bujji.engine.openai_compat_engines import (
                OpenAICompatEngine, normalize_openai_base_url)

            base = normalize_openai_base_url(_hosted_cfg["base_url"])
            # /healthz is keyless; /v1/models may be MANAGEMENT_TOKEN-gated while
            # the inference plane still routes. <500 means the sidecar is up.
            ok = False
            for _ in range(3):
                try:
                    if httpx.get(f"{base}/healthz", timeout=3).status_code < 500:
                        ok = True
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(1.5)
            if not ok:
                raise RuntimeError("sidecar /healthz not reachable")
            engine = OpenAICompatEngine(host=base, api_key=_hosted_cfg.get("api_key"))
            return engine, "auto", "omniroute"
        except Exception as exc:
            logger.warning("Bujji Core: OmniRoute not usable (%s); staying local", exc)

    from bujji.engine.ollama import OllamaEngine

    engine = OllamaEngine()
    return engine, _reachable_model(engine), "ollama"


def start() -> None:
    """Start the embedded Bujji Control Core server (idempotent)."""
    global _server, _server_thread, _url
    if is_running():
        return

    global _engine_name
    try:
        from bujji.server.app import create_app

        engine, model, engine_name = _build_engine()
        app = _FrameFriendly(create_app(engine, model, engine_name=engine_name))
        _engine_name = engine_name
        logger.info("Bujji Control Core engine: %s (%s)", engine_name, model)
    except Exception as exc:
        logger.exception("Failed to build Bujji Control Core app: %s", exc)
        return

    import uvicorn

    # Prefer the well-known port, but never fight over it: fall back to an
    # OS-assigned free loopback port if anything else already owns 8321.
    port = (BUJJI_UI_PORT if _port_free(BUJJI_UI_HOST, BUJJI_UI_PORT)
            else _reserve_free_port(BUJJI_UI_HOST))
    expected = f"http://{BUJJI_UI_HOST}:{port}/"

    _server = uvicorn.Server(
        uvicorn.Config(app, host=BUJJI_UI_HOST, port=port,
                       log_level="warning", access_log=False)
    )
    _server.install_signal_handlers = lambda: None

    def _run() -> None:
        global _url
        try:
            _server.run()
        except Exception:  # crash-path visibility in ultron.log
            logger.exception("Bujji Control Core server crashed")
        finally:
            _url = None

    _server_thread = threading.Thread(target=_run, name="bujji-core", daemon=True)
    _server_thread.start()

    if wait_until_reachable(BUJJI_UI_HOST, port):
        _url = expected
        logger.info("Bujji Control Core embedding started on %s", expected)
    elif _server_thread is not None and _server_thread.is_alive():
        logger.error("Bujji Control Core not reachable at %s within timeout", expected)
    else:
        logger.error("Bujji Control Core thread exited before serving at %s", expected)


def stop() -> None:
    """Stop the embedded Bujji Control Core server (idempotent)."""
    global _server, _server_thread, _url
    server = _server
    if server is not None:
        try:
            server.should_exit = True
        except Exception:
            pass
    if _server_thread is not None:
        _server_thread.join(timeout=5)
    _server = None
    _server_thread = None
    _url = None
    logger.info("Bujji Control Core embedding stopped")