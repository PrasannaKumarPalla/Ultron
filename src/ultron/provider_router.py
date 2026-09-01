"""ProviderRouter: local | hosted | auto, failover that is never silent.

auto prefers hosted (OmniRoute) when the sidecar is healthy and the user has
not pinned local mode; falls back to Ollama on sidecar unhealthy, upstream
5xx, quota exhaustion, or timeout > N. Every switch emits a callback carrying
a `provider_switched` event payload for observability.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from .llm_providers import ChatEvent, ChatProvider, ProviderError


@dataclass
class SwitchDecision:
    from_provider: str
    to_provider: str
    reason: str  # sidecar_unhealthy | pinned_local | privacy_mode | server_error | quota_exhausted | timeout | hosted_preferred | mode_forced
    detail: str = ""

    def as_event_payload(self) -> dict:
        return {"from": self.from_provider, "to": self.to_provider,
                "reason": self.reason, "detail": self.detail}


class QuotaCooldowns:
    """Marks upstreams/providers cooling down after a 429 storm."""

    def __init__(self):
        self._until: dict[str, float] = {}

    def mark(self, key: str, seconds: float) -> None:
        self._until[key] = max(self._until.get(key, 0), time.monotonic() + seconds)

    def active(self, key: str) -> bool:
        until = self._until.get(key)
        return bool(until and time.monotonic() < until)

    def clear(self, key: str) -> None:
        self._until.pop(key, None)

    def snapshot(self) -> dict[str, float]:
        now = time.monotonic()
        return {key: round(until - now, 1) for key, until in self._until.items() if until > now}


class ProviderRouter:
    def __init__(self, ollama: ChatProvider, omniroute: ChatProvider,
                 *, default_mode: str = "auto", timeout_s: float = 90.0,
                 cooldowns: QuotaCooldowns | None = None,
                 pinned_local: Callable[[], bool] | None = None,
                 privacy_mode: Callable[[], bool] | None = None,
                 on_switch: Callable[[SwitchDecision], None] | None = None):
        self.providers = {"ollama": ollama, "omniroute": omniroute}
        self.default_mode = default_mode
        self.timeout_s = timeout_s
        self.cooldowns = cooldowns or QuotaCooldowns()
        self.pinned_local = pinned_local or (lambda: False)
        self.privacy_mode = privacy_mode or (lambda: False)
        self.on_switch = on_switch
        self.last_provider: str | None = None

    # ── resolution ────────────────────────────────────────────────────
    async def resolve(self, requested_mode: str | None = None) -> tuple[ChatProvider, str]:
        """Return (provider, reason). Emits a switch decision when it differs
        from the previous call's provider."""
        mode = requested_mode or self.default_mode
        hosted = self.providers["omniroute"]
        local = self.providers["ollama"]

        if mode == "local" or self.privacy_mode():
            reason = "privacy_mode" if self.privacy_mode() else "mode_forced"
            return self._decide(local, reason)

        if mode == "hosted":
            return await self._try_hosted(hosted, local, forced=True)

        # auto: prefer hosted unless pinned local / privacy / unhealthy / cooling down
        if self.pinned_local():
            return self._decide(local, "pinned_local")
        if self.cooldowns.active("omniroute"):
            return self._decide(local, "quota_exhausted")
        if not await hosted.health():
            return self._decide(local, "sidecar_unhealthy")
        return self._decide(hosted, "hosted_preferred")

    async def _try_hosted(self, hosted: ChatProvider, local: ChatProvider,
                          forced: bool) -> tuple[ChatProvider, str]:
        if not await hosted.health():
            if forced:
                raise ProviderError("unreachable", "OmniRoute sidecar is not healthy")
            return self._decide(local, "sidecar_unhealthy")
        return self._decide(hosted, "mode_forced")

    def _decide(self, provider: ChatProvider, reason: str) -> tuple[ChatProvider, str]:
        if self.on_switch is not None and self.last_provider is not None \
                and self.last_provider != provider.name:
            self.on_switch(SwitchDecision(self.last_provider, provider.name, reason))
        self.last_provider = provider.name
        return provider, reason

    # ── chat with automatic failover ──────────────────────────────────
    async def chat(self, messages: list[dict], model: str | None = None,
                   tools: list[dict] | None = None, format: dict | None = None,
                   stream: bool = True, mode: str | None = None):
        """AsyncIterator[ChatEvent] that fails over to Ollama when hosted dies
        mid-call. Failures are surfaced via switch decisions, never swallowed."""
        provider, reason = await self.resolve(mode)
        try:
            async for event in provider.chat(messages, model, tools, format, stream):
                yield event
            return
        except ProviderError as error:
            if provider.name != "omniroute":
                raise
            fallback_reason = {
                "quota": "quota_exhausted", "timeout": "timeout",
                "server_error": "server_error",
            }.get(error.reason, "sidecar_unhealthy")
            if error.reason == "quota":
                self.cooldowns.mark("omniroute", 300)
        local = self.providers["ollama"]
        self._decide(local, fallback_reason)
        async for event in local.chat(messages, model, tools, format, stream):
            yield event
