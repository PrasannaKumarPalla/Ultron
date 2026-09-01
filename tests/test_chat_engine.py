import json

import httpx
import pytest

from ultron.chat_engine import ChatEngine, MAX_TOOL_ITERATIONS


class FakeToolRegistry:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def schemas(self) -> list[dict]:
        return [{"type": "function", "function": {"name": "web_search", "parameters": {}}}]

    async def call(self, name: str, arguments: dict) -> dict:
        self.calls.append((name, arguments))
        return {"ok": True, "result": f"result for {arguments.get('query')}"}


def _ollama_response(message: dict) -> httpx.Response:
    return httpx.Response(200, json={"message": message})


@pytest.mark.asyncio
async def test_turn_returns_plain_answer_when_no_tool_call():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return _ollama_response({"role": "assistant", "content": "Hello! How can I help?"})

    tools = FakeToolRegistry()
    engine = ChatEngine("http://fake-ollama", "qwen3:30b", tools)
    engine._client_kwargs = {"transport": httpx.MockTransport(handler)}

    produced = [message async for message in engine.turn([], "Hi there")]

    assert len(produced) == 1
    assert produced[0] == {"role": "assistant", "content": "Hello! How can I help?"}
    assert tools.calls == []
    assert requests[0]["options"]["num_ctx"] == 16384
    assert requests[0]["options"]["num_predict"] == 2048


@pytest.mark.asyncio
async def test_turn_executes_tool_call_then_returns_final_answer():
    responses = [
        {"role": "assistant", "content": "", "tool_calls": [
            {"function": {"name": "web_search", "arguments": {"query": "ultron ai"}}}
        ]},
        {"role": "assistant", "content": "Here is what I found."},
    ]
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        response = _ollama_response(responses[call_count["n"]])
        call_count["n"] += 1
        return response

    tools = FakeToolRegistry()
    engine = ChatEngine("http://fake-ollama", "qwen3:30b", tools)
    engine._client_kwargs = {"transport": httpx.MockTransport(handler)}

    produced = [message async for message in engine.turn([], "Search for ultron ai")]

    assert tools.calls == [("web_search", {"query": "ultron ai"})]
    roles = [m["role"] for m in produced]
    assert roles == ["assistant", "tool", "assistant"]
    assert produced[-1]["content"] == "Here is what I found."
    assert json.loads(produced[1]["content"]) == {"ok": True, "result": "result for ultron ai"}
    assert produced[0]["tool_calls"] == [
        {"function": {"name": "web_search", "arguments": {"query": "ultron ai"}}}
    ]


@pytest.mark.asyncio
async def test_turn_handles_string_tool_arguments():
    # Regression: Ollama may return tool-call arguments as a JSON string.
    responses = [
        {"role": "assistant", "content": "", "tool_calls": [
            {"function": {"name": "web_search", "arguments": "{\"query\": \"ultron\"}"}}
        ]},
        {"role": "assistant", "content": "Here is what I found."},
    ]
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        response = _ollama_response(responses[call_count["n"]])
        call_count["n"] += 1
        return response

    tools = FakeToolRegistry()
    engine = ChatEngine("http://fake-ollama", "qwen3:30b", tools)
    engine._client_kwargs = {"transport": httpx.MockTransport(handler)}

    produced = [message async for message in engine.turn([], "Search for ultron")]

    assert tools.calls == [("web_search", {"query": "ultron"})]
    assert produced[-1]["content"] == "Here is what I found."


@pytest.mark.asyncio
async def test_turn_stops_after_max_iterations():
    def handler(request: httpx.Request) -> httpx.Response:
        return _ollama_response({"role": "assistant", "content": "", "tool_calls": [
            {"function": {"name": "web_search", "arguments": {"query": "loop"}}}
        ]})

    tools = FakeToolRegistry()
    engine = ChatEngine("http://fake-ollama", "qwen3:30b", tools)
    engine._client_kwargs = {"transport": httpx.MockTransport(handler)}

    produced = [message async for message in engine.turn([], "loop forever")]

    assert len(tools.calls) == MAX_TOOL_ITERATIONS
    assert produced[-1]["role"] == "assistant"
    assert "tool-call limit" in produced[-1]["content"]
