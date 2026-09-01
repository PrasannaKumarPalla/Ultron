"""Process-wide OmniRoute runtime: sidecar + providers + router + catalog.

Privacy mode is authoritative: when set, hosted calls are refused outright,
the sidecar is stopped and the catalog is disabled — zero outbound.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from .catalog import ModelCatalog
from .config import Settings, get_settings
from .db import Repository
from .llm_providers import OllamaProvider, OmniRouteProvider, load_api_key
from .provider_router import ProviderRouter, QuotaCooldowns
from .redaction import Redactor
from .sidecar import OmniRouteSidecar

logger = logging.getLogger(__name__)

PRIVACY_KEY = "privacy_mode"
PINNED_LOCAL_KEY = "pinned_local"
HOSTED_PAUSED_KEY = "hosted_paused"
ROUTER_MODE_KEY = "router_mode"


def _bench_ranking(bench_root: Path) -> dict[str, float]:
    path = bench_root / "omniroute-vs-local.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    ranking = {}
    for result in data.get("results", []):
        score = result.get("score")
        key = f"{result.get('provider')}:{result.get('model')}"
        if isinstance(score, (int, float)) and score is not None:
            ranking[key] = float(score)
    return ranking


class OmniRouteRuntime:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.repo = Repository(settings.database_path)
        self.repo.initialize()
        self.sidecar = OmniRouteSidecar(
            settings.omniroute_url, config_path=settings.omniroute_config_path,
            image=settings.omniroute_sidecar_image,
            package=settings.omniroute_sidecar_package,
            api_key=load_api_key(settings.omniroute_secrets_dir))
        self.redactor = Redactor(settings.omniroute_secrets_dir)
        self.omniroute = OmniRouteProvider(
            settings.omniroute_url, secrets_dir=settings.omniroute_secrets_dir,
            redactor=self.redactor,
            compression=self.sidecar.config.compression,
            default_upstream_timeout_s=settings.hosted_call_timeout_s)
        self.ollama = OllamaProvider(settings.ollama_url)
        self.cooldowns = QuotaCooldowns()
        self.router = ProviderRouter(
            self.ollama, self.omniroute,
            default_mode=self._setting(ROUTER_MODE_KEY, settings.router_mode),
            timeout_s=settings.hosted_call_timeout_s,
            cooldowns=self.cooldowns,
            pinned_local=lambda: self._setting(PINNED_LOCAL_KEY) == "1",
            privacy_mode=lambda: self.privacy_enabled(),
            on_switch=self.record_switch)
        self.catalog = ModelCatalog(
            self.repo, self.omniroute, self.ollama, cooldowns=self.cooldowns,
            bench_ranking=_bench_ranking(Path("./bench")))
        self.background_tasks: list[asyncio.Task] = []

    # ── settings-backed toggles ───────────────────────────────────────
    def _setting(self, key: str, default: str | None = None) -> str | None:
        return self.repo.get_setting(key, default)

    def _set(self, key: str, value: str) -> None:
        self.repo.set_setting(key, value)

    def privacy_enabled(self) -> bool:
        return self._setting(PRIVACY_KEY) == "1"

    def set_privacy_mode(self, enabled: bool) -> dict:
        self._set(PRIVACY_KEY, "1" if enabled else "0")
        if enabled:
            self.sidecar.stop()
        return {"privacy_mode": enabled}

    def set_router_mode(self, mode: str) -> dict:
        if mode not in {"local", "hosted", "auto"}:
            raise ValueError(f"unknown router mode: {mode}")
        self._set(ROUTER_MODE_KEY, mode)
        self._set(PINNED_LOCAL_KEY, "1" if mode == "local" else "")
        self.router.default_mode = mode
        return {"mode": mode}

    def hosted_paused(self) -> bool:
        return self._setting(HOSTED_PAUSED_KEY) == "1"

    def pause_hosted_for_cost(self) -> None:
        self._set(HOSTED_PAUSED_KEY, "1")

    def acknowledge_costs(self) -> None:
        self._set(HOSTED_PAUSED_KEY, "")

    # ── per-repo consent ──────────────────────────────────────────────
    def consent_key(self, repo_path: str) -> str:
        return f"hosted_consent:{repo_path.strip().lower()}"

    def consent(self, repo_path: str) -> bool:
        return self._setting(self.consent_key(repo_path)) == "accepted"

    def record_consent(self, repo_path: str, accept: bool) -> dict:
        self._set(self.consent_key(repo_path), "accepted" if accept else "declined")
        return {"repo_path": repo_path, "consent": accept}

    # ── switch observability ──────────────────────────────────────────
    def record_switch(self, decision) -> None:
        payload = decision.as_event_payload()
        self.repo.append_run_event("app", "provider.switched", "provider-router", payload)
        logger.info("provider switched: %s -> %s (%s)",
                    payload["from"], payload["to"], payload["reason"])

    # ── lifecycle ─────────────────────────────────────────────────────
    async def start(self) -> None:
        self.repo.initialize()
        if self.privacy_enabled():
            logger.info("privacy mode is on; OmniRoute sidecar stays stopped")
            return
        if not self.sidecar.config.enabled or not self.settings.omniroute_enabled:
            logger.info("OmniRoute disabled by config; running Ollama-only")
            return
        self.background_tasks.append(asyncio.create_task(self._bring_up_sidecar()))
        self.background_tasks.append(asyncio.create_task(
            self.catalog.refresh_loop(self.settings.catalog_refresh_s)))
        self.sidecar.watcher_task = asyncio.create_task(self.sidecar.supervise())

    async def _bring_up_sidecar(self) -> None:
        """Start the sidecar off the critical path — npm bring-up can take
        tens of seconds and must not hold up the control-plane boot."""
        try:
            if await self.sidecar.start():
                await self.catalog.refresh()
        except Exception as exc:
            logger.warning("OmniRoute sidecar unavailable (%s); Ollama stays primary", exc)

    async def stop(self) -> None:
        for task in self.background_tasks:
            task.cancel()
        self.sidecar.stop()


_RUNTIME: OmniRouteRuntime | None = None


def get_runtime(settings: Settings | None = None) -> OmniRouteRuntime:
    global _RUNTIME
    if _RUNTIME is None:
        _RUNTIME = OmniRouteRuntime(settings or get_settings())
    return _RUNTIME


def reset_runtime() -> None:
    """Test hook: drop the cached singleton."""
    global _RUNTIME
    _RUNTIME = None

