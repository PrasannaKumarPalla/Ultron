"""ProviderRouter: auto/hosted/local resolution, observable switches,
mid-stream kill failover with no lost tokens, quota cool-down + fallback."""

from collections.abc import AsyncIterator

import pytest

from ultron.llm_providers import ChatEvent, ChatProvider, ProviderError
from ultron.provider_router import ProviderRouter, QuotaCooldowns

MESSAGES = [{"role": "user", "content": "hi"}]


class FakeProvider(ChatProvider):
    def __init__(self, name: str, healthy: bool = True, events=None,
                 error_after: int | None = None, reason: str = "unreachable"):
        self.name = name
        self._healthy = healthy
        self._events = events or [ChatEvent("token", text="a"), ChatEvent("token", text="b"),
                                  ChatEvent("done", meta={"tokens_out": 2})]
        self.error_after = error_after
        self.reason = reason
        self.calls = 0

    async def health(self) -> bool:
        return self._healthy

    async def chat(self, messages, model=None, tools=None, format=None,
                   stream=True) -> AsyncIterator[ChatEvent]:
        self.calls += 1
        emitted = 0
        for event in self._events:
            if self.error_after is not None and emitted >= self.error_after:
                raise ProviderError(self.reason, "boom")
            emitted += 1
            yield event


def make_router(hosted_healthy=True, hosted_error_after=None, hosted_reason="unreachable",
                pinned_local=False, privacy=False):
    ollama = FakeProvider("ollama")
    omniroute = FakeProvider("omniroute", healthy=hosted_healthy,
                             error_after=hosted_error_after, reason=hosted_reason)
    switches = []
    router = ProviderRouter(
        ollama, omniroute,
        cooldowns=QuotaCooldowns(),
        pinned_local=lambda: pinned_local,
        privacy_mode=lambda: privacy,
        on_switch=lambda decision: switches.append(decision))
    return router, ollama, omniroute, switches


async def collect(stream):
    return [(event.kind, event.text) async for event in stream]


@pytest.mark.asyncio
async def test_auto_prefers_hosted_when_sidecar_healthy():
    router, _, omniroute, _ = make_router()
    provider, reason = await router.resolve()
    assert provider is not None and provider.name == "omniroute"
    assert reason == "hosted_preferred"


@pytest.mark.asyncio
async def test_auto_falls_back_when_sidecar_unhealthy():
    router, ollama, omniroute, switches = make_router(hosted_healthy=False)
    provider, reason = await router.resolve()
    assert provider.name == "ollama"
    assert reason == "sidecar_unhealthy"
    assert switches == []  # first call records baseline; no switch yet


@pytest.mark.asyncio
async def test_switch_emits_provider_switched_event():
    router, _, _, switches = make_router(hosted_healthy=False)
    # simulate a prior hosted call, then the sidecar goes down
    router.last_provider = "omniroute"
    provider, reason = await router.resolve()
    assert provider.name == "ollama"
    assert len(switches) == 1
    payload = switches[0].as_event_payload()
    assert payload == {"from": "omniroute", "to": "ollama", "reason": "sidecar_unhealthy",
                       "detail": ""}


@pytest.mark.asyncio
async def test_pinned_local_stays_local():
    router, ollama, omniroute, switches = make_router(pinned_local=True)
    provider, reason = await router.resolve()
    assert provider.name == "ollama"
    assert reason == "pinned_local"


@pytest.mark.asyncio
async def test_privacy_mode_forces_local():
    router, _, _, _ = make_router(privacy=True)
    provider, reason = await router.resolve("hosted")
    assert provider.name == "ollama"
    assert reason == "privacy_mode"


@pytest.mark.asyncio
async def test_midstream_kill_fails_over_without_lost_tokens():
    """Sidecar dies after emitting 'a': user still receives the full local answer."""
    router, ollama, omniroute, switches = make_router(hosted_error_after=1)
    await router.resolve()  # baseline: hosted preferred
    tokens = []
    async for event in router.chat(MESSAGES):
        if event.kind == "token":
            tokens.append(event.text)
    assert "".join(tokens) != ""           # something reached the user
    assert ollama.calls == 1               # failover happened
    reasons = [decision.reason for decision in switches]
    assert "server_error" in reasons or "sidecar_unhealthy" in reasons


@pytest.mark.asyncio
async def test_quota_marks_cooldown_then_falls_back():
    router, ollama, omniroute, _ = make_router(hosted_error_after=0, hosted_reason="quota")
    tokens = []
    async for event in router.chat(MESSAGES):
        if event.kind == "token":
            tokens.append(event.text)
    assert ollama.calls >= 1
    assert router.cooldowns.active("omniroute")
    # next auto call must go straight to Ollama without probing a cooled-down pool
    provider, reason = await router.resolve()
    assert reason == "quota_exhausted"


@pytest.mark.asyncio
async def test_cooldown_expiry_restores_hosted():
    router, _, _, _ = make_router()
    router.cooldowns._until["omniroute"] = -1.0  # expired
    provider, reason = await router.resolve()
    assert reason == "hosted_preferred"


@pytest.mark.asyncio
async def test_local_never_fails_over_to_hosted():
    router, ollama, omniroute, _ = make_router()

    class ExplodingLocal(FakeProvider):
        async def chat(self, messages, model=None, tools=None, format=None, stream=True):
            raise ProviderError("server_error", "ollama down")
            yield  # pragma: no cover

    router.providers["ollama"] = ExplodingLocal("ollama")
    with pytest.raises(ProviderError):
        await collect(router.chat(MESSAGES, mode="local"))
