"""Chat provider abstraction: OllamaProvider and OmniRouteProvider.

Contract: chat(messages, model, tools, format, stream) -> AsyncIterator[ChatEvent].
ProviderError carries a machine-readable failover reason; the router turns it
into an observable provider_switched decision, never a silent retry.
"""

from __future__ import annotations

import json
import os
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path

import httpx

AUTO_MODEL = "auto"


class ProviderError(Exception):
    """Failover-relevant provider failure."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(detail or reason)
        self.reason = reason  # unreachable | server_error | quota | timeout | bad_response


@dataclass
class ChatEvent:
    kind: str  # token | tool_call | meta | done
    text: str | None = None
    tool_calls: list[dict] | None = None
    meta: dict = field(default_factory=dict)


def load_api_key(secrets_dir: Path | None) -> str | None:
    """Keys live under the secrets dir or OMNIROUTE_API_KEY, never logged,
    never off-localhost."""
    if secrets_dir is not None:
        for name in ("api_key.txt", "omniroute.key"):
            path = secrets_dir / name
            try:
                if path.exists():
                    key = path.read_text(encoding="utf-8").strip()
                    if key:
                        return key
            except OSError:
                continue
    return os.environ.get("OMNIROUTE_API_KEY", "").strip() or None


class ChatProvider(ABC):
    name: str = "abstract"

    @abstractmethod
    def chat(self, messages: list[dict], model: str | None = None,
             tools: list[dict] | None = None, format: dict | None = None,
             stream: bool = True) -> AsyncIterator[ChatEvent]: ...

    @abstractmethod
    async def health(self) -> bool: ...


def _classify_http_error(exc: httpx.HTTPError) -> ProviderError:
    if isinstance(exc, httpx.TimeoutException):
        return ProviderError("timeout", str(exc))
    if isinstance(exc, httpx.ConnectError):
        return ProviderError("unreachable", str(exc))
    response = getattr(exc, "response", None)
    if response is not None and response.status_code == 429:
        return ProviderError("quota", str(exc))
    if response is not None and response.status_code >= 500:
        return ProviderError("server_error", str(exc))
    return ProviderError("unreachable", str(exc))


class OllamaProvider(ChatProvider):
    """Existing native /api/chat behaviour, refactored behind the interface."""

    def __init__(self, base_url: str, client_factory=None):
        self.base_url = base_url.rstrip("/")
        self.name = "ollama"
        self.client_factory = client_factory or (lambda **kw: httpx.AsyncClient(**kw))

    async def health(self) -> bool:
        try:
            async with self.client_factory(timeout=3) as client:
                response = await client.get(f"{self.base_url}/api/tags")
            return response.is_success
        except httpx.HTTPError:
            return False

    async def installed_models(self) -> set[str]:
        try:
            async with self.client_factory(timeout=5) as client:
                response = await client.get(f"{self.base_url}/api/tags")
                response.raise_for_status()
            return {item.get("name", "") for item in response.json().get("models", [])}
        except (httpx.HTTPError, ValueError):
            return set()

    async def chat(self, messages: list[dict], model: str | None = None,
                   tools: list[dict] | None = None, format: dict | None = None,
                   stream: bool = True) -> AsyncIterator[ChatEvent]:
        payload: dict = {"model": model or AUTO_MODEL, "messages": messages, "stream": stream}
        if tools:
            payload["tools"] = tools
        if format:
            payload["format"] = format
        try:
            async with self.client_factory(
                    timeout=httpx.Timeout(600.0, connect=10.0)) as client:
                response = await client.post(f"{self.base_url}/api/chat", json=payload)
                if response.status_code == 429:
                    raise ProviderError("quota", "ollama rate limited")
                if response.status_code >= 500:
                    raise ProviderError("server_error", f"ollama {response.status_code}")
                response.raise_for_status()
                if not stream:
                    for event in _ollama_message_events(response.json()):
                        yield event
                    return
                async for line in response.aiter_lines():
                    if not line.strip():
                        continue
                    chunk = json.loads(line)
                    for event in _ollama_message_events(chunk):
                        yield event
        except httpx.HTTPError as exc:
            raise _classify_http_error(exc) from exc
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise ProviderError("bad_response", str(exc)) from exc


def _ollama_message_events(chunk: dict) -> list[ChatEvent]:
    events = []
    message = chunk.get("message") or {}
    piece = message.get("content") or ""
    if piece:
        events.append(ChatEvent("token", text=piece))
    if message.get("tool_calls"):
        events.append(ChatEvent("tool_call", tool_calls=message["tool_calls"]))
    if chunk.get("done"):
        events.append(ChatEvent("done", meta={
            "tokens_in": chunk.get("prompt_eval_count"),
            "tokens_out": chunk.get("eval_count"),
        }))
    return events


class OmniRouteProvider(ChatProvider):
    """OpenAI-compatible client for the local OmniRoute sidecar.

    The sidecar is the ONLY outbound bridge to hosted models: keys never leave
    this process except to localhost:20128. RTK+Caveman stacked compression is
    on by default (quota savings matter only when a hosted pool pays for them).
    """

    def __init__(self, base_url: str, secrets_dir: Path | None = None,
                 redactor=None, compression: bool = True,
                 default_upstream_timeout_s: float = 90.0, client_factory=None):
        self.base_url = base_url.rstrip("/")
        if not self.base_url.endswith("/v1"):
            self.base_url += "/v1"
        self.name = "omniroute"
        self.api_key = load_api_key(secrets_dir)
        self.redactor = redactor
        self.compression = compression
        self.default_upstream_timeout_s = default_upstream_timeout_s
        self.client_factory = client_factory or (lambda **kw: httpx.AsyncClient(**kw))

    async def health(self) -> bool:
        """The OmniRoute inference plane (/v1/chat/completions) is keyless even
        when the management plane (/v1/models, /health) is gated by a
        MANAGEMENT_TOKEN. Probe the unauthenticated /healthz; treat any HTTP
        response < 500 as "sidecar is up" since a 401 still proves liveness."""
        root = self.base_url[:-3] if self.base_url.endswith("/v1") else self.base_url
        for url in (f"{root}/healthz", f"{root}/api/health", f"{self.base_url}/models"):
            try:
                async with self.client_factory(timeout=3) as client:
                    response = await client.get(url, headers=self.auth_headers())
                if response.status_code < 500:
                    return True
            except httpx.HTTPError:
                continue
        return False

    def auth_headers(self) -> dict:
        """Bearer header for the loopback sidecar. base_url is fixed to
        127.0.0.1:20128, so no off-host credential check is needed here."""
        return {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}

    def _headers(self, request_host: str) -> dict:
        if not self.api_key:
            return {}
        if request_host not in {"localhost", "127.0.0.1", "::1"}:
            raise ProviderError("unreachable", "refusing to send credentials off localhost")
        return {"Authorization": f"Bearer {self.api_key}"}

    def _build_payload(self, messages: list[dict], model: str | None, tools: list[dict] | None,
                       format: dict | None, stream: bool) -> tuple[dict, list[str]]:
        redaction_names: list[str] = []
        sent_messages = []
        if self.redactor is not None:
            for message in messages:
                content = message.get("content")
                if isinstance(content, str):
                    clean, names = self.redactor.redact(content)
                    redaction_names.extend(names)
                    sent_messages.append({**message, "content": clean})
                else:
                    sent_messages.append(message)
        else:
            sent_messages = list(messages)
        payload: dict = {"model": model or AUTO_MODEL, "messages": sent_messages, "stream": stream}
        if tools:
            payload["tools"] = tools
        if format is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "ultron_response", "schema": format},
            }
        if self.compression:
            payload["x_ultron_compression"] = {"stack": ["rtk", "caveman"]}
        return payload, redaction_names

    async def chat(self, messages: list[dict], model: str | None = None,
                   tools: list[dict] | None = None, format: dict | None = None,
                   stream: bool = True) -> AsyncIterator[ChatEvent]:
        payload, _names = self._build_payload(messages, model, tools, format, stream)
        host = httpx.URL(self.base_url).host or "localhost"
        headers = self._headers(host)
        timeout = httpx.Timeout(self.default_upstream_timeout_s, connect=5.0)
        try:
            async with self.client_factory(timeout=timeout) as client:
                response = await client.post(f"{self.base_url}/chat/completions",
                                             json=payload, headers=headers)
                if response.status_code == 429:
                    raise ProviderError("quota", "upstream rate limited")
                if response.status_code >= 500:
                    raise ProviderError("server_error", f"sidecar {response.status_code}")
                response.raise_for_status()
                if stream and response.headers.get("content-type", "").startswith("text/event-stream"):
                    async for event in _sse_events(response):
                        yield event
                else:
                    for event in _openai_message_events(response.json()):
                        yield event
        except httpx.HTTPError as exc:
            raise _classify_http_error(exc) from exc
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise ProviderError("bad_response", str(exc)) from exc


async def _sse_events(response: httpx.Response) -> AsyncIterator[ChatEvent]:
    usage: dict | None = None
    compressed_tokens: int | None = None
    async for line in response.aiter_lines():
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            break
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError:
            continue
        choices = chunk.get("choices") or [{}]
        delta = choices[0].get("delta") or {}
        if delta.get("content"):
            yield ChatEvent("token", text=delta["content"])
        if delta.get("tool_calls"):
            yield ChatEvent("tool_call", tool_calls=delta["tool_calls"])
        if chunk.get("usage"):
            usage = chunk["usage"]
        if chunk.get("x_ultron_compressed_tokens") is not None:
            compressed_tokens = chunk["x_ultron_compressed_tokens"]
    meta: dict = {}
    if usage:
        meta = {"tokens_in": usage.get("prompt_tokens"), "tokens_out": usage.get("completion_tokens")}
    if compressed_tokens is not None:
        meta["compressed_tokens"] = compressed_tokens
    yield ChatEvent("done", meta=meta)


def _openai_message_events(body: dict) -> list[ChatEvent]:
    events = []
    choice = (body.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    if message.get("content"):
        events.append(ChatEvent("token", text=message["content"]))
    if message.get("tool_calls"):
        events.append(ChatEvent("tool_call", tool_calls=message["tool_calls"]))
    usage = body.get("usage") or {}
    meta: dict = {"tokens_in": usage.get("prompt_tokens"), "tokens_out": usage.get("completion_tokens")}
    if body.get("x_ultron_compressed_tokens") is not None:
        meta["compressed_tokens"] = body["x_ultron_compressed_tokens"]
    events.append(ChatEvent("done", meta=meta))
    return events

