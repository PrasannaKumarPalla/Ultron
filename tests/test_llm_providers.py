"""Conformance suite: both ChatProvider implementations must satisfy the same
contract — streaming tokens, done meta with token counts, tool-call passthrough,
JSON-schema format handling, and classified ProviderError on failure."""

import json

import httpx
import pytest

from ultron.llm_providers import OllamaProvider, OmniRouteProvider, ProviderError

MESSAGES = [{"role": "user", "content": "hello"}]


def ollama_factory(handler):
    return lambda **kw: httpx.AsyncClient(transport=httpx.MockTransport(handler), **kw)


def omniroute_factory(handler):
    return lambda **kw: httpx.AsyncClient(transport=httpx.MockTransport(handler), **kw)


def openai_stream_lines():
    chunks = [
        {"choices": [{"delta": {"content": "hel"}}]},
        {"choices": [{"delta": {"content": "lo"}}]},
        {"choices": [{"delta": {}}], "usage": {"prompt_tokens": 3, "completion_tokens": 2}},
    ]
    body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n"
    return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})


def ollama_stream_lines():
    lines = [
        json.dumps({"message": {"content": "hel"}}),
        json.dumps({"message": {"content": "lo"}}),
        json.dumps({"message": {}, "done": True, "prompt_eval_count": 3, "eval_count": 2}),
    ]
    return httpx.Response(200, text="\n".join(lines) + "\n",
                          headers={"content-type": "application/x-ndjson"})


@pytest.fixture
def ollama_provider():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/api/tags"):
            return httpx.Response(200, json={"models": [{"name": "qwen3:30b"}]})
        return ollama_stream_lines()
    return OllamaProvider("http://mock-ollama", client_factory=ollama_factory(handler))


@pytest.fixture
def omniroute_provider():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/v1/models"):
            return httpx.Response(200, json={"data": [{"id": "auto", "owned_by": "omniroute"}]})
        return openai_stream_lines()
    return OmniRouteProvider("http://127.0.0.1:20128", client_factory=omniroute_factory(handler))


async def collect(events):
    return [event async for event in events]


# ── shared contract ────────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("provider_name", ["ollama", "omniroute"])
async def test_streams_tokens_and_done_meta(provider_name, ollama_provider, omniroute_provider):
    provider = ollama_provider if provider_name == "ollama" else omniroute_provider
    events = await collect(provider.chat(MESSAGES, stream=True))
    kinds = [event.kind for event in events]
    assert kinds[0] == "token"
    assert "done" in kinds
    tokens = "".join(event.text for event in events if event.kind == "token")
    assert tokens == "hello"
    done = next(event for event in events if event.kind == "done")
    assert done.meta.get("tokens_in") == 3
    assert done.meta.get("tokens_out") == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_name", ["ollama", "omniroute"])
async def test_health_reflects_upstream(provider_name, ollama_provider, omniroute_provider):
    provider = ollama_provider if provider_name == "ollama" else omniroute_provider
    assert await provider.health() is True


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_name", ["ollama", "omniroute"])
async def test_5xx_raises_server_error(provider_name, ollama_provider, omniroute_provider):
    handler = lambda request: httpx.Response(502, text="bad gateway")  # noqa: E731
    factory = ollama_factory(handler) if provider_name == "ollama" else omniroute_factory(handler)
    provider = OllamaProvider("http://x", client_factory=factory) \
        if provider_name == "ollama" \
        else OmniRouteProvider("http://127.0.0.1:20128", client_factory=factory)
    with pytest.raises(ProviderError) as excinfo:
        await collect(provider.chat(MESSAGES))
    assert excinfo.value.reason == "server_error"


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_name", ["ollama", "omniroute"])
async def test_connect_error_is_unreachable(provider_name, ollama_provider, omniroute_provider):
    def handler(request):
        raise httpx.ConnectError("refused")

    factory = ollama_factory(handler) if provider_name == "ollama" else omniroute_factory(handler)
    provider = OllamaProvider("http://x", client_factory=factory) \
        if provider_name == "ollama" \
        else OmniRouteProvider("http://127.0.0.1:20128", client_factory=factory)
    with pytest.raises(ProviderError) as excinfo:
        await collect(provider.chat(MESSAGES))
    assert excinfo.value.reason == "unreachable"


# ── OmniRoute specifics ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_omniroute_429_raises_quota():
    handler = lambda request: httpx.Response(429, json={"error": "rate limited"})  # noqa: E731
    provider = OmniRouteProvider("http://127.0.0.1:20128", client_factory=omniroute_factory(handler))
    with pytest.raises(ProviderError) as excinfo:
        await collect(provider.chat(MESSAGES))
    assert excinfo.value.reason == "quota"


@pytest.mark.asyncio
async def test_omniroute_defaults_to_auto_model_and_compression():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return openai_stream_lines()

    provider = OmniRouteProvider("http://127.0.0.1:20128", client_factory=omniroute_factory(handler))
    await collect(provider.chat(MESSAGES))
    assert captured["model"] == "auto"
    assert captured["x_ultron_compression"]["stack"] == ["rtk", "caveman"]


@pytest.mark.asyncio
async def test_omniroute_redacts_secrets_before_send():
    seen = {}

    class FakeRedactor:
        def redact(self, text):
            return text.replace("s3cret", "[REDACTED:x]"), ["generic_hex_secret"]

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return openai_stream_lines()

    provider = OmniRouteProvider("http://127.0.0.1:20128",
                                 redactor=FakeRedactor(), client_factory=omniroute_factory(handler))
    await collect(provider.chat([{"role": "user", "content": "key is s3cret ok"}]))
    sent_content = seen["body"]["messages"][0]["content"]
    assert "s3cret" not in sent_content
    assert "[REDACTED:" in sent_content


def test_api_key_only_sent_to_localhost(tmp_path):
    (tmp_path / "api_key.txt").write_text("sk-test-123\n", encoding="utf-8")
    provider = OmniRouteProvider("http://127.0.0.1:20128", secrets_dir=tmp_path)
    assert provider.api_key == "sk-test-123"
    assert "Authorization" in provider._headers("127.0.0.1")
    with pytest.raises(ProviderError):
        provider._headers("api.groq.com")


@pytest.mark.asyncio
async def test_omniroute_json_schema_maps_to_response_format():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return openai_stream_lines()

    provider = OmniRouteProvider("http://127.0.0.1:20128", client_factory=omniroute_factory(handler))
    schema = {"type": "object", "properties": {"summary": {"type": "string"}}}
    await collect(provider.chat(MESSAGES, format=schema))
    assert seen["body"]["response_format"]["json_schema"]["schema"] == schema


@pytest.mark.asyncio
async def test_ollama_format_passthrough():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return ollama_stream_lines()

    provider = OllamaProvider("http://mock", client_factory=ollama_factory(handler))
    schema = {"type": "object"}
    await collect(provider.chat(MESSAGES, format=schema))
    assert seen["body"]["format"] == schema


@pytest.mark.asyncio
async def test_compression_disabled_for_local_only_flag():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return openai_stream_lines()

    provider = OmniRouteProvider("http://127.0.0.1:20128", compression=False,
                                 client_factory=omniroute_factory(handler))
    await collect(provider.chat(MESSAGES))
    assert "x_ultron_compression" not in captured


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_name", ["ollama", "omniroute"])
async def test_5xx_raises_server_error(provider_name, ollama_provider, omniroute_provider):
    handler = lambda request: httpx.Response(502, text="bad gateway")  # noqa: E731
    factory = ollama_factory(handler) if provider_name == "ollama" else omniroute_factory(handler)
    provider = OllamaProvider("http://x", client_factory=factory) \
        if provider_name == "ollama" else OmniRouteProvider("http://127.0.0.1:20128", client_factory=factory)
    with pytest.raises(ProviderError) as excinfo:
        await collect(provider.chat(MESSAGES))
    assert excinfo.value.reason == "server_error"


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_name", ["ollama", "omniroute"])
async def test_connect_error_is_unreachable(provider_name, ollama_provider, omniroute_provider):
    def handler(request):
        raise httpx.ConnectError("refused")

    factory = ollama_factory(handler) if provider_name == "ollama" else omniroute_factory(handler)
    provider = OllamaProvider("http://x", client_factory=factory) \
        if provider_name == "ollama" else OmniRouteProvider("http://127.0.0.1:20128", client_factory=factory)
    with pytest.raises(ProviderError) as excinfo:
        await collect(provider.chat(MESSAGES))
    assert excinfo.value.reason == "unreachable"
