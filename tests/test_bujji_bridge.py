import asyncio
import json
from pathlib import Path

from fastapi.testclient import TestClient

import ultron.api as api_module
from ultron.api import app
from ultron.bujji_bridge import BujjiBridge
from ultron.config import Settings, get_settings
from ultron.db import Repository


class FakeBujjiSdk:
    version = "0.0.0+test"

    def list_engines(self):
        return ["ollama"]

    def list_models(self):
        return ["qwen2.5:7b"]

    def ask_full(self, query, *, model=None):
        return {"content": f"echo:{query}", "usage": {}, "model": model or "qwen2.5:7b", "engine": "ollama"}

    async def ask_stream(self, query, *, model=None, **_kwargs):
        for token in ["echo:", query]:
            yield token


def bujji_settings(tmp_path: Path) -> Settings:
    settings = Settings(
        database_path=tmp_path / "bujji.db",
        checkpoint_path=tmp_path / "checkpoints.db",
        projects_root=tmp_path / "projects",
        execution_provider="mock",
    )
    Repository(settings.database_path).initialize()
    return settings


def test_bujji_bridge_streams_with_injected_sdk():
    async def scenario():
        bridge = BujjiBridge(Settings(), sdk=FakeBujjiSdk())
        tokens = [token async for token in bridge.stream("hello")]
        assert tokens == ["echo:", "hello"]
        result = await bridge.ask_full("hello")
        assert result["content"] == "echo:hello"
        status = await bridge.status()
        assert status["available"] is True
        assert status["models"] == ["qwen2.5:7b"]

    asyncio.run(scenario())


def test_bujji_status_reports_unavailable_when_sdk_fails():
    async def scenario():
        bridge = BujjiBridge(Settings())

        async def failing(self):
            raise RuntimeError("ollama not reachable")

        original = BujjiBridge._ensure_sdk
        try:
            BujjiBridge._ensure_sdk = failing
            status = await bridge.status()
        finally:
            BujjiBridge._ensure_sdk = original
        assert status["available"] is False
        assert "ollama not reachable" in status["detail"]

    asyncio.run(scenario())


def test_bujji_api_routes(tmp_path: Path):
    settings = bujji_settings(tmp_path)
    fake_bridge = BujjiBridge(settings, sdk=FakeBujjiSdk())
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[api_module.bujji_bridge] = lambda: fake_bridge
    try:
        with TestClient(app) as client:
            status = client.get("/api/bujji/status")
            assert status.status_code == 200
            body = status.json()
            assert body["available"] is True
            assert body["engines"] == ["ollama"]
            assert "qwen2.5:7b" in body["models"]

            response = client.post("/api/bujji/chat", json={"query": "hi there"})
            assert response.status_code == 200
            events = {}
            for block in response.text.split("\n\n"):
                lines = block.strip().splitlines()
                name = next((line[len("event: "):] for line in lines if line.startswith("event: ")), None)
                data = next((line[len("data: "):] for line in lines if line.startswith("data: ")), None)
                if name and data:
                    events.setdefault(name, []).append(json.loads(data))
            tokens = "".join(item["token"] for item in events["bujji-token"])
            assert tokens == "echo:hi there"
            assert events["bujji-done"][0]["content"] == "echo:hi there"

            empty = client.post("/api/bujji/chat", json={"query": "   "})
            assert empty.status_code == 400

            assets = client.get("/assets/bujji.js")
            assert assets.status_code == 200
            assert "sendBujjiMessage" in assets.text

            dashboard = client.get("/")
            assert dashboard.status_code == 200
            assert 'data-view="bujji"' in dashboard.text
    finally:
        app.dependency_overrides.pop(get_settings, None)
        app.dependency_overrides.pop(api_module.bujji_bridge, None)