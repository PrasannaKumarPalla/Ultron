"""Parity: wake word, voice-loop routing, and mission triggering."""

import asyncio

from fastapi.testclient import TestClient

import ultron.api as api_module
from ultron.api import app
from ultron.assistant_desk import (
    AssistantDesk,
    classify_utterance,
    heard_wake_word,
    pick_hardware_model,
    strip_wake_word,
)
from ultron.bujji_bridge import BujjiBridge
from ultron.config import get_settings
from ultron.model_router import route_general_chat_model
from ultron.role_registry import RoleRegistry


def test_wake_word_gates_transcripts():
    assert heard_wake_word("hey assistant what's the plan") is True
    assert heard_wake_word("hey bujji what's the plan") is True  # legacy brand still wakes
    assert heard_wake_word("hey buddy, how are you") is True  # whisper mis-hear tolerance
    assert heard_wake_word("tell me about python packaging") is False


def test_strip_wake_word_leaves_the_utterance():
    assert strip_wake_word("hey assistant, what time is it") == "what time is it"
    assert strip_wake_word("bujji what time is it") == "what time is it"
    assert strip_wake_word("no wake word here") is None
    assert strip_wake_word("hey assistant") == ""


def test_utterance_routing_mission_vs_answer():
    mission = classify_utterance("start mission called Fix the login flow")
    assert mission.action == "mission"
    assert mission.title == "Fix the login flow"
    answer = classify_utterance("what does this repo do")
    assert answer.action == "answer"
    assert answer.query == "what does this repo do"


def test_hardware_model_picker_respects_vram_budget():
    installed = {"qwen3:30b", "qwen3:8b", "qwen2.5:7b"}
    model, reason = pick_hardware_model(installed, vram_gb=12.0, fallback="phi4:latest")
    assert model == "qwen3:8b"  # qwen3:30b needs >=18 GB
    assert "VRAM" in reason
    model_big, _ = pick_hardware_model(installed, vram_gb=24.0, fallback="phi4:latest")
    assert model_big == "qwen3:30b"


def test_hardware_model_picker_falls_back_to_ultron_router():
    model, reason = pick_hardware_model({"nonexistent-model"}, vram_gb=None, fallback="phi4:latest")
    expected, _ = route_general_chat_model({"nonexistent-model"}, "phi4:latest")
    assert model == expected
    assert reason


def test_desk_answers_directly_without_a_mission(settings, fake_sdk):
    bridge = BujjiBridge(settings, sdk=fake_sdk)
    desk = AssistantDesk(settings)
    desk._bridge = bridge
    result = asyncio.run(desk.handle_transcript(
        "hey assistant summarize the incident", installed={"qwen2.5:7b"}))
    assert result["triggered"] is True
    assert result["action"] == "answer"
    assert result["content"] == "echo:summarize the incident"


def test_desk_routes_mission_without_answering(settings, fake_sdk):
    bridge = BujjiBridge(settings, sdk=fake_sdk)
    desk = AssistantDesk(settings)
    desk._bridge = bridge
    result = asyncio.run(desk.handle_transcript(
        "assistant, start a mission called Refactor auth",
        installed={"qwen2.5:7b", "qwen3:30b"}))
    assert result["triggered"] is True
    assert result["action"] == "mission"
    assert result["title"] == "Refactor auth"
    assert result["model"] in {"qwen2.5:7b", "qwen3:30b"}


def test_legacy_bujji_brand_still_triggers_missions(settings, fake_sdk):
    bridge = BujjiBridge(settings, sdk=fake_sdk)
    desk = AssistantDesk(settings)
    desk._bridge = bridge
    result = asyncio.run(desk.handle_transcript(
        "bujji start mission called Ship the release", installed={"qwen2.5:7b"}))
    assert result["triggered"] is True
    assert result["action"] == "mission"
    assert result["title"] == "Ship the release"


def test_desk_ignores_unaddressed_speech(settings):
    desk = AssistantDesk(settings)
    result = asyncio.run(desk.handle_transcript("casual background chatter"))
    assert result["triggered"] is False


def test_assistant_is_a_registered_role(roles_path):
    registry = RoleRegistry(roles_path)
    spec = registry.get("assistant")
    assert spec is not None
    assert spec.name == "Assistant"
    assert spec.desk_position, "Assistant must have a desk on the Ops floor"


def test_assistant_api_routes(settings, fake_sdk):
    bridge = BujjiBridge(settings, sdk=fake_sdk)
    desk = AssistantDesk(settings)
    desk._bridge = bridge
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[api_module.bujji_bridge] = lambda: bridge
    app.dependency_overrides[api_module.assistant_desk] = lambda: desk
    try:
        with TestClient(app) as client:
            info = client.get("/api/assistant/desk").json()
            assert info["role"]["name"] == "Assistant"
            assert info["wake_word"]
            assert info["sdk"]["available"] is True

            answered = client.post("/api/assistant/listen",
                                   json={"transcript": "hey assistant status report"})
            assert answered.status_code == 200
            body = answered.json()
            assert body["triggered"] is True
            assert body["action"] == "answer"

            unaddressed = client.post("/api/assistant/listen",
                                      json={"transcript": "random noise"})
            assert unaddressed.json()["triggered"] is False

            empty = client.post("/api/assistant/listen", json={"transcript": "   "})
            assert empty.status_code == 400

            picked = client.post("/api/assistant/model").json()
            assert picked["model"]
            assert picked["reason"]
    finally:
        app.dependency_overrides.pop(get_settings, None)
        app.dependency_overrides.pop(api_module.bujji_bridge, None)
        app.dependency_overrides.pop(api_module.assistant_desk, None)
