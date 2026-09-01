"""OmniRoute sidecar lifecycle: Ultron owns start/stop/health/restart.

Prefers the Docker image when Docker is available; falls back to the npm
package via a Node runtime. The sidecar is Ultron's only outbound bridge to
hosted models — OpenAI-compatible at http://localhost:20128/v1.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import yaml

logger = logging.getLogger(__name__)

HEALTH_PATHS = ("/healthz", "/api/health", "/health", "/v1/models")
RESTART_CAP = 3


def default_config_path() -> Path:
    appdata = os.environ.get("APPDATA")
    root = Path(appdata) / "Ultron" if appdata else Path.home() / ".ultron"
    return root / "omniroute.yaml"


@dataclass
class SidecarConfig:
    enabled: bool = True
    install_preference: str = "auto"  # auto | docker | npm
    port: int = 20128
    providers: list[str] = field(default_factory=list)
    combos: list[str] = field(default_factory=list)
    quota_share_policy: str = "fair"
    compression: bool = True

    @classmethod
    def load(cls, path: Path) -> "SidecarConfig":
        if not path.exists():
            return cls()
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return cls(
            enabled=bool(raw.get("enabled", True)),
            install_preference=str(raw.get("install_preference", "auto")),
            port=int(raw.get("port", 20128)),
            providers=list(raw.get("providers", [])),
            combos=list(raw.get("combos", [])),
            quota_share_policy=str(raw.get("quota_share_policy", "fair")),
            compression=bool(raw.get("compression", True)),
        )

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "enabled": self.enabled,
            "install_preference": self.install_preference,
            "port": self.port,
            "providers": self.providers,
            "combos": self.combos,
            "quota_share_policy": self.quota_share_policy,
            "compression": self.compression,
        }
        path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def _docker_bin() -> str:
    return shutil.which("docker") or "docker"


def docker_daemon_up(docker_path: str | None) -> bool:
    """True only when the Docker CLI exists AND the daemon answers."""
    if not docker_path:
        return False
    try:
        result = subprocess.run([docker_path, "info", "--format", "{{.ServerVersion}}"],
                                capture_output=True, text=True, timeout=8)
    except (subprocess.TimeoutExpired, OSError):
        return False
    return result.returncode == 0 and result.stdout.strip() != ""


def detect_runtime() -> dict:
    """Detect Docker vs Node availability for the first-run wizard."""
    docker = shutil.which("docker")
    node = shutil.which("node") or shutil.which("node.exe")
    return {"docker": bool(docker), "docker_path": docker,
            "docker_daemon": docker_daemon_up(docker),
            "node": bool(node), "node_path": node}


class OmniRouteSidecar:
    """Manages the local OmniRoute process. Crash => auto-restart, capped."""

    def __init__(self, base_url: str, config_path: Path | None = None,
                 image: str = "diegosouzapw/omniroute", package: str = "omniroute",
                 api_key: str | None = None):
        self.base_url = base_url.rstrip("/")
        self.config_path = config_path or default_config_path()
        self.image = image
        self.package = package
        self.api_key = api_key
        self.config = SidecarConfig.load(self.config_path)
        self.process: subprocess.Popen | None = None
        self.container_name = "ultron-omniroute"
        self.restart_count = 0
        self.container_started = False
        self.watcher_task: asyncio.Task | None = None
        self.last_error: str | None = None
        self.install_method: str | None = None
        self.install_progress: dict = {"stage": "idle", "detail": ""}

    def _auth(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}

    async def healthy(self, timeout_s: float = 2.0) -> bool:
        for path in HEALTH_PATHS:
            try:
                async with httpx.AsyncClient(timeout=timeout_s) as client:
                    response = await client.get(f"{self.base_url}{path}", headers=self._auth())
                if response.is_success:
                    return True
            except httpx.HTTPError:
                continue
        return False

    async def install(self) -> dict:
        method = self.pick_install_method()
        self.install_method = method
        self.install_progress = {"stage": "pulling", "detail": method}
        try:
            if method == "docker":
                await asyncio.to_thread(
                    subprocess.run,
                    [_docker_bin(), "pull", self.image], check=True,
                    capture_output=True, text=True, timeout=600)
            else:
                await asyncio.to_thread(
                    subprocess.run,
                    ["npm", "view", self.package, "version"], check=True,
                    capture_output=True, text=True, timeout=120)
        except (subprocess.TimeoutExpired, subprocess.CalledProcessError, FileNotFoundError) as exc:
            self.install_progress = {"stage": "failed", "detail": str(exc)}
            raise RuntimeError(f"OmniRoute install failed via {method}: {exc}") from exc
        self.install_progress = {"stage": "installed", "detail": method}
        return {"method": method}

    async def start(self) -> bool:
        """Start the sidecar (idempotent) and block until healthy.

        Tries the Docker image first when the daemon is up; on any Docker
        failure it falls through to the npm package when Node is present.
        """
        if not self.config.enabled:
            return False
        if await self.healthy():
            return True
        try:
            method = self.pick_install_method()
        except RuntimeError as exc:
            self.last_error = str(exc)
            logger.warning("OmniRoute sidecar cannot start: %s", exc)
            return False

        if method == "docker":
            self.install_method = "docker"
            try:
                self.process = None
                await asyncio.to_thread(
                    subprocess.run,
                    [_docker_bin(), "run", "-d", "--rm", "--name", self.container_name,
                     "-p", f"{self.config.port}:20128", self.image],
                    check=True, capture_output=True, text=True, timeout=120)
                self.container_started = True
                if await self.wait_healthy():
                    return True
                self.last_error = "docker container did not become healthy"
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
                self.last_error = f"docker start failed: {exc}"
                logger.warning("OmniRoute docker start failed (%s)", exc)
            if not detect_runtime()["node"]:
                return False
            logger.info("OmniRoute falling back to the npm package")
            method = "npm"

        self.install_method = "npm"
        npx = self._resolve_npx()
        if not npx:
            self.last_error = "npx not found on PATH"
            logger.warning("OmniRoute npm fallback unavailable: npx not found")
            return False
        self.process = subprocess.Popen(
            [npx, "-y", self.package, "serve", "--port", str(self.config.port), "--no-open"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        started = await self.wait_healthy()
        if not started:
            self.last_error = "sidecar did not become healthy after start"
            logger.warning("OmniRoute sidecar failed health check after start")
        return started

    @staticmethod
    def _resolve_npx() -> str | None:
        if os.name == "nt":
            # CreateProcess cannot run the extensionless shim; require .cmd/.exe.
            for name in ("npx.cmd", "npx.exe"):
                found = shutil.which(name)
                if found:
                    return found
            node_dir = shutil.which("node")
            if node_dir:
                candidate = Path(node_dir).with_name("npx.cmd")
                if candidate.exists():
                    return str(candidate)
            return None
        return shutil.which("npx")

    async def restart_now(self) -> bool:
        """Operator-triggered restart: clears the cap and the last error."""
        self.stop()
        self.restart_count = 0
        self.last_error = None
        return await self.start()

    def _container_running(self) -> bool:
        if not self.container_started:
            return False
        try:
            result = subprocess.run(
                [_docker_bin(), "inspect", "-f", "{{.State.Running}}", self.container_name],
                capture_output=True, text=True, timeout=8)
        except (subprocess.TimeoutExpired, OSError):
            return False
        return result.returncode == 0 and result.stdout.strip() == "true"

    def stop(self) -> None:
        if self.watcher_task:
            self.watcher_task.cancel()
            self.watcher_task = None
        if self.process is not None and self.process.poll() is None:
            if os.name == "nt":
                # `omniroute serve` forks worker processes; kill the whole tree.
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(self.process.pid)],
                               capture_output=True, text=True, timeout=15)
            else:
                self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
        self.process = None
        if self.container_started and detect_runtime()["docker"]:
            try:
                subprocess.run([_docker_bin(), "rm", "-f", self.container_name],
                               capture_output=True, text=True, timeout=30)
            except (subprocess.TimeoutExpired, FileNotFoundError):
                pass
        self.container_started = False

    async def supervise(self, poll_s: float = 5.0) -> None:
        """Auto-restart loop with a cap; runs as a background task."""
        while True:
            await asyncio.sleep(poll_s)
            if await self.healthy():
                continue
            process_alive = self.process is not None and self.process.poll() is None
            if process_alive or self._container_running():
                continue  # up but not answering yet — give it another poll
            if self.restart_count >= RESTART_CAP:
                self.last_error = f"restart cap ({RESTART_CAP}) reached"
                logger.error("OmniRoute sidecar restart cap reached; giving up")
                return
            self.restart_count += 1
            logger.warning("OmniRoute sidecar down; restarting (%d/%d)",
                           self.restart_count, RESTART_CAP)
            try:
                await self.start()
            except Exception as exc:
                self.last_error = str(exc)

    def status(self) -> dict:
        return {
            "base_url": self.base_url,
            "running": self.process is not None and self.process.poll() is None,
            "restart_count": self.restart_count,
            "last_error": self.last_error,
            "install_method": self.install_method,
            "install_progress": self.install_progress,
            "config_path": str(self.config_path),
            "runtime": detect_runtime(),
        }

    async def free_tiers(self) -> dict | None:
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                response = await client.get(f"{self.base_url}/dashboard/free-tiers", headers=self._auth())
            if response.is_success:
                return response.json()
        except (httpx.HTTPError, json.JSONDecodeError, ValueError):
            pass
        return None

    async def models(self) -> list[dict]:
        async with httpx.AsyncClient(timeout=5) as client:
            response = await client.get(f"{self.base_url}/v1/models", headers=self._auth())
            response.raise_for_status()
        return response.json().get("data", [])

    async def wait_healthy(self, attempts: int = 30, backoff_s: float = 0.5,
                           max_backoff_s: float = 4.0) -> bool:
        delay = backoff_s
        for _ in range(attempts):
            if await self.healthy():
                return True
            await asyncio.sleep(delay)
            delay = min(delay * 1.5, max_backoff_s)
        return False

    def pick_install_method(self) -> str:
        runtime = detect_runtime()
        preference = self.config.install_preference
        if preference == "docker":
            if not runtime["docker_daemon"]:
                raise RuntimeError("docker requested but the Docker daemon is not running")
            return "docker"
        if preference == "npm":
            if not runtime["node"]:
                raise RuntimeError("npm install requested but no Node runtime is available")
            return "npm"
        if runtime["docker_daemon"]:
            return "docker"
        if runtime["node"]:
            return "npm"
        raise RuntimeError("OmniRoute needs either a running Docker daemon or a Node runtime")

