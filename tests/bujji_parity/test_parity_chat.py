"""Parity: local chat through the unified /api/bujji surface."""

import json
import asyncio

from fastapi.testclient import TestClient

import ultron.api as api_module
from ultron.api import app
from ultron.bujji_bridge import BujjiBridge
from ultron.config import get_settings


def test_status_reports_engines_and_models(settings, fake_sdk):
    bridge = BujjiBridge(settings, sdk=fake_sdk)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[api_module.bujji_bridge] = lambda: bridge
    try:
        with TestClient(app) as client:
            body = client.get("/api/bujji/status").json()
            assert body["available"] is True
            assert body["engines"] == ["ollama"]
            assert "qwen2.5:7b" in body["models"]
            assert body["version"] == fake_sdk.version
    finally:
        app.dependency_overrides.pop(get_settings, None)
        app.dependency_overrides.pop(api_module.bujji_bridge, None)


def test_chat_streams_tokens_and_done_event(settings, fake_sdk):
    bridge = BujjiBridge(settings, sdk=fake_sdk)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[api_module.bujji_bridge] = lambda: bridge
    try:
        with TestClient(app) as client:
            response = client.post("/api/bujji/chat", json={"query": "hello studio"})
            assert response.status_code == 200
            events: dict[str, list[dict]] = {}
            for block in response.text.split("\n\n"):
                lines = block.strip().splitlines()
                name = next((line[len("event: "):] for line in lines if line.startswith("event: ")), None)
                data = next((line[len("data: "):] for line in lines if line.startswith("data: ")), None)
                if name and data:
                    events.setdefault(name, []).append(json.loads(data))
            tokens = "".join(item["token"] for item in events["bujji-token"])
            assert tokens == "echo:hello studio"
            assert events["bujji-done"][0]["content"] == "echo:hello studio"
    finally:
        app.dependency_overrides.pop(get_settings, None)
        app.dependency_overrides.pop(api_module.bujji_bridge, None)


def test_empty_query_is_rejected(settings, fake_sdk):
    bridge = BujjiBridge(settings, sdk=fake_sdk)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[api_module.bujji_bridge] = lambda: bridge
    try:
        with TestClient(app) as client:
            response = client.post("/api/bujji/chat", json={"query": "   "})
            assert response.status_code == 400
    finally:
        app.dependency_overrides.pop(get_settings, None)
        app.dependency_overrides.pop(api_module.bujji_bridge, None)
