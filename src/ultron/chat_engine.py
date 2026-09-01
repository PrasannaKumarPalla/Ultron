from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator

import httpx

from .chat_tools import ToolRegistry

logger = logging.getLogger(__name__)

MAX_TOOL_ITERATIONS = 8

SYSTEM_PROMPT = (
    "You are Ultron, a local-first AI engineering assistant. You have tools for web search, "
    "reading/writing files in the active project workspace, running shell commands, and "
    "inspecting/creating/pausing missions. Use tools when they would improve your answer. "
    "Be direct and concise."
)

GENERAL_SYSTEM_PROMPT = (
    "You are Ultron, a local-first AI assistant. Answer directly and concisely. "
    "This is a general conversation without an attached workspace, so file, shell, mission, and project tools are unavailable."
)


def normalize_tool_arguments(raw: Any) -> dict[str, Any]:
    # Ollama's /api/chat may return tool-call arguments either as a parsed
    # object or as a JSON string depending on model/format; accept both.
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return raw if isinstance(raw, dict) else {}


class ChatEngine:
    def __init__(self, base_url: str, model: str, tools: ToolRegistry):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.tools = tools
        self._client_kwargs: dict[str, Any] = {}

    async def turn(self, history: list[dict[str, Any]], user_message: str) -> AsyncIterator[dict[str, Any]]:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT if self.tools.schemas() else GENERAL_SYSTEM_PROMPT},
            *history,
            {"role": "user", "content": user_message},
        ]

        async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=10.0), **self._client_kwargs) as client:
            for _ in range(MAX_TOOL_ITERATIONS):
                reply = await self._call_model(messages, client)
                tool_calls = reply.get("tool_calls") or []
                if not tool_calls:
                    final_message = {"role": "assistant", "content": reply.get("content", "")}
                    yield final_message
                    return

                assistant_message = {
                    "role": "assistant",
                    "content": reply.get("content", ""),
                    "tool_calls": tool_calls,
                }
                yield assistant_message
                messages.append(dict(assistant_message))

                for call in tool_calls:
                    name = call["function"]["name"]
                    arguments = normalize_tool_arguments(call["function"].get("arguments"))
                    logger.debug("chat tool call: %s", name)
                    outcome = await self.tools.call(name, arguments)
                    content = json.dumps(outcome)
                    tool_message = {"role": "tool", "tool_name": name, "content": content}
                    yield tool_message
                    messages.append({"role": "tool", "tool_name": name, "content": content})

            fallback = {"role": "assistant",
                        "content": "I wasn't able to finish within the tool-call limit. Here is what I found so far."}
            yield fallback

    async def _call_model(self, messages: list[dict[str, Any]], client: httpx.AsyncClient) -> dict[str, Any]:
        response = await client.post(f"{self.base_url}/api/chat", json={
            "model": self.model,
            "messages": messages,
            "tools": self.tools.schemas(),
            "stream": False,
            "keep_alive": "5m",
            "options": {"temperature": 0.3, "num_ctx": 16384, "num_predict": 2048},
        })
        response.raise_for_status()
        return response.json()["message"]
