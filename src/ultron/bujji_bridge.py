from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, AsyncIterator

if TYPE_CHECKING:
    from .config import Settings

logger = logging.getLogger(__name__)


class BujjiBridge:
    def __init__(self, settings: Settings, sdk: Any | None = None) -> None:
        self._settings = settings
        self._sdk = sdk
        self._lock = asyncio.Lock()

    async def _ensure_sdk(self) -> Any:
        if self._sdk is not None:
            return self._sdk
        async with self._lock:
            if self._sdk is None:
                from bujji import Bujji

                model = self._settings.default_model
                try:
                    probe = Bujji(engine_key="ollama", model=model)
                    installed = await asyncio.to_thread(probe.list_models)
                    if installed and model not in installed:
                        logger.warning(
                            "Bujji model %r is not installed in Ollama; "
                            "falling back to %r", model, installed[0])
                        model = installed[0]
                    self._sdk = Bujji(engine_key="ollama", model=model)
                except Exception:
                    self._sdk = Bujji(engine_key="ollama", model=self._settings.default_model)
        return self._sdk

    async def status(self) -> dict[str, Any]:
        try:
            sdk = await self._ensure_sdk()
        except Exception as exc:
            return {"available": False, "version": "", "engines": [], "models": [], "detail": str(exc)}
        engines = await asyncio.to_thread(sdk.list_engines)
        try:
            models = await asyncio.to_thread(sdk.list_models)
        except Exception as exc:
            logger.warning("Bujji model listing failed: %s", exc)
            return {"available": False, "version": sdk.version, "engines": engines, "models": [], "detail": str(exc)}
        return {"available": True, "version": sdk.version, "engines": engines, "models": models, "detail": ""}

    async def ask_full(self, query: str, model: str | None = None) -> dict[str, Any]:
        sdk = await self._ensure_sdk()
        return await asyncio.to_thread(sdk.ask_full, query, model=model)

    async def stream(self, query: str, model: str | None = None) -> AsyncIterator[dict[str, Any]]:
        sdk = await self._ensure_sdk()
        async for token in sdk.ask_stream(query, model=model):
            yield token


_bridge: BujjiBridge | None = None


def get_bujji_bridge(settings: Settings) -> BujjiBridge:
    global _bridge
    if _bridge is None:
        _bridge = BujjiBridge(settings)
    return _bridge