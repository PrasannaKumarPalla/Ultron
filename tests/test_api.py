import json
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

import ultron.api as api_module
from ultron.api import app
from ultron.chat_engine import ChatEngine
from ultron.config import Settings, get_settings
from ultron.db import Repository


def test_api_vertical_slice(tmp_path: Path, monkeypatch):
    async def fake_models(_base_url: str):
        return [{"name": "qwen3:30b", "size": 18_000_000_000}, {"name": "devstral:24b", "size": 14_000_000_000}]

    monkeypatch.setattr(api_module, "ollama_models", fake_models)
    settings = Settings(
        database_path=tmp_path / "api.db",
        checkpoint_path=tmp_path / "checkpoints.db",
        projects_root=tmp_path / "projects",
        execution_provider="mock",
    )
    Repository(settings.database_path).initialize()
    app.dependency_overrides[get_settings] = lambda: settings
    try:
        with TestClient(app) as client:
            dashboard = client.get("/")
            assert dashboard.status_code == 200
            assert "Ultron" in dashboard.text
            assert client.get("/assets/app.js").status_code == 200
            assert client.get("/assets/onboarding.css").status_code == 200
            assert client.get("/assets/icons/ultron-32.png").status_code == 200
            assert client.get("/assets/icons/ultron-light-32.png").status_code == 200
            assert client.get("/assets/icons/ultron-dark-32.png").status_code == 200
            assert client.get("/assets/chat.js").status_code == 200
            assert client.get("/assets/chat.css").status_code == 200
            assert 'id="browseWorkspace"' in dashboard.text
            assert 'id="chatModelSwitcher"' in dashboard.text
            assert 'General chat — no workspace' in client.get("/assets/chat.js").text
            assert 'Ultron is thinking' in client.get("/assets/chat.js").text

            health = client.get("/health")
            assert health.status_code == 200
            assert health.json()["database"] == "healthy"

            models = client.get("/api/models")
            assert models.status_code == 200
            assert models.json()["active"] == Settings().default_model
            selected = client.put("/api/models/active", json={"model": "devstral:24b"})
            assert selected.status_code == 200
            assert client.get("/api/models").json()["active"] == "devstral:24b"
            automatic = client.put("/api/models/active", json={"model": "auto"})
            assert automatic.status_code == 200
            assert client.get("/api/models").json()["active"] == "auto"

            created = client.post(
                "/projects",
                json={
                    "name": "API fixture",
                    "description": "A complete API path",
                    "workspace_path": str(tmp_path / "workspace"),
                    "classification": "PERSONAL",
                },
            )
            assert created.status_code == 201
            project_id = created.json()["id"]
            assert client.get(f"/projects/{project_id}").json()["name"] == "API fixture"

            mission = client.post(
                f"/projects/{project_id}/missions",
                json={
                    "title": "First mission",
                    "objective": "Exercise the complete bootstrap workflow safely.",
                },
            )
            assert mission.status_code == 201
            mission_id = mission.json()["id"]
            assert client.get(f"/missions/{mission_id}").json()["title"] == "First mission"

            started = client.post(f"/missions/{mission_id}/start")
            assert started.status_code == 200
            assert started.json()["status"] == "BLOCKED"

            events = client.get(f"/missions/{mission_id}/events").json()
            assert [event["kind"] for event in events] == [
                "mission.created",
                "run.started",
                "node.started",
                "workflow.started",
                "status.changed",
                "node.completed",
                "node.started",
                "execution.submitted",
                "node.completed",
                "node.started",
                "status.changed",
                "node.completed",
                "run.completed",
            ]

            checkpoint = client.get(f"/missions/{mission_id}/checkpoint")
            assert checkpoint.status_code == 200
            assert checkpoint.json()["state"]["execution_provider"] == "mock"

            artifacts = client.get(f"/missions/{mission_id}/artifacts")
            assert artifacts.status_code == 200
            assert artifacts.json() == []
            assert client.get("/missions/does-not-exist/artifacts").status_code == 404

            approval = client.post(f"/missions/{mission_id}/approvals", json={
                "action": "Publish generated artifact", "risk": "Writes outside the project workspace"
            })
            assert approval.status_code == 201
            approval_id = approval.json()["id"]
            decided = client.post(f"/approvals/{approval_id}/decision", json={
                "decision": "APPROVED", "rationale": "Reviewed in the isolated test workspace"
            })
            assert decided.status_code == 200
            assert decided.json()["decision"] == "APPROVED"

            memory = client.post(f"/projects/{project_id}/memories", json={
                "scope": "project", "role": "architect",
                "content": "Use an event-driven boundary between services.",
                "provenance": "Architecture decision test fixture", "confidence": 0.9,
                "sensitivity": "PERSONAL"
            })
            assert memory.status_code == 201
            memories = client.get(f"/projects/{project_id}/memories").json()
            assert memories[0]["role"] == "architect"

            cancelled = client.post(f"/missions/{mission_id}/cancel", json={"reason": "Scenario complete"})
            assert cancelled.status_code == 200
            assert cancelled.json()["status"] == "CANCELLED"

            mismatch = client.request("DELETE", f"/projects/{project_id}", json={
                "confirm_name": "wrong workspace", "delete_files": False
            })
            assert mismatch.status_code == 409

            deleted = client.request("DELETE", f"/projects/{project_id}", json={
                "confirm_name": "API fixture", "delete_files": False
            })
            assert deleted.status_code == 200
            assert deleted.json()["missions_deleted"] == 1
            assert client.get(f"/projects/{project_id}").status_code == 404
            assert client.get(f"/missions/{mission_id}").status_code == 404
    finally:
        app.dependency_overrides.clear()


def test_chat_session_crud_and_archive(tmp_path: Path, monkeypatch):
    settings = Settings(
        database_path=tmp_path / "api.db",
        checkpoint_path=tmp_path / "checkpoints.db",
        projects_root=tmp_path / "projects",
        execution_provider="mock",
    )
    Repository(settings.database_path).initialize()
    app.dependency_overrides[get_settings] = lambda: settings
    try:
        with TestClient(app) as client:
            project = client.post("/projects", json={
                "name": "Chat API Project", "description": "", "workspace_path": str(tmp_path / "workspace"),
                "classification": "PERSONAL",
            }).json()

            created = client.post(f"/projects/{project['id']}/chat/sessions", json={"title": "First chat"})
            assert created.status_code == 201
            session = created.json()
            assert session["title"] == "First chat"
            assert session["archived_at"] is None

            listed = client.get(f"/projects/{project['id']}/chat/sessions")
            assert listed.status_code == 200
            assert len(listed.json()) == 1

            archived = client.post(f"/chat/sessions/{session['id']}/archive")
            assert archived.status_code == 200
            assert archived.json()["archived_at"] is not None

            listed_default = client.get(f"/projects/{project['id']}/chat/sessions")
            assert listed_default.json() == []

            listed_all = client.get(f"/projects/{project['id']}/chat/sessions?archived=true")
            assert len(listed_all.json()) == 1

            unarchived = client.post(f"/chat/sessions/{session['id']}/unarchive")
            assert unarchived.json()["archived_at"] is None

            messages = client.get(f"/chat/sessions/{session['id']}/messages")
            assert messages.status_code == 200
            assert messages.json() == []

            missing = client.get("/chat/sessions/does-not-exist/messages")
            assert missing.status_code == 404

            deleted = client.request("DELETE", f"/chat/sessions/{session['id']}")
            assert deleted.status_code == 200
            assert client.get(f"/chat/sessions/{session['id']}/messages").status_code == 404
            assert client.request("DELETE", "/chat/sessions/does-not-exist").status_code == 404
    finally:
        app.dependency_overrides.clear()


def test_general_chat_without_workspace(tmp_path: Path, monkeypatch):
    settings = Settings(database_path=tmp_path / "api.db", checkpoint_path=tmp_path / "checkpoints.db")
    Repository(settings.database_path).initialize()
    app.dependency_overrides[get_settings] = lambda: settings

    selected_models = []

    async def fake_models(_base_url: str):
        return [{"name": "qwen3:30b", "size": 1}, {"name": "phi4:latest", "size": 1}]

    async def fake_turn(self, history, user_message):
        assert self.tools.schemas() == []
        selected_models.append(self.model)
        yield {"role": "assistant", "content": "Hello from general chat"}

    monkeypatch.setattr(api_module, "ollama_models", fake_models)
    monkeypatch.setattr(api_module.ChatEngine, "turn", fake_turn)
    try:
        with TestClient(app) as client:
            session = client.post("/chat/sessions", json={"title": "General"})
            assert session.status_code == 201
            assert session.json()["project_id"] is None
            assert client.put("/api/models/active", json={"model": "auto"}).status_code == 200
            response = client.post(f"/chat/sessions/{session.json()['id']}/messages", json={"content": "hi"})
            assert response.status_code == 200
            assert "Hello from general chat" in response.text
            assert len(client.get("/chat/sessions").json()) == 1
            assert selected_models == ["phi4:latest"]
    finally:
        app.dependency_overrides.clear()


def test_send_chat_message_streams_events_and_persists(tmp_path: Path, monkeypatch):
    settings = Settings(
        database_path=tmp_path / "api.db",
        checkpoint_path=tmp_path / "checkpoints.db",
        projects_root=tmp_path / "projects",
        execution_provider="mock",
    )
    Repository(settings.database_path).initialize()
    app.dependency_overrides[get_settings] = lambda: settings

    selected_models = []

    async def fake_models(_base_url: str):
        return [{"name": "qwen3:30b", "size": 1}, {"name": "devstral:24b", "size": 1}]

    async def fake_turn(self, history, user_message):
        selected_models.append(self.model)
        yield {"role": "tool", "tool_name": "web_search", "content": '{"ok": true, "result": "stub"}'}
        yield {"role": "assistant", "content": f"Answering: {user_message}"}

    monkeypatch.setattr(api_module, "ollama_models", fake_models)
    monkeypatch.setattr(api_module.ChatEngine, "turn", fake_turn)

    try:
        with TestClient(app) as client:
            project = client.post("/projects", json={
                "name": "Chat Send Project", "description": "", "workspace_path": str(tmp_path / "workspace2"),
                "classification": "PERSONAL",
            }).json()
            session = client.post(f"/projects/{project['id']}/chat/sessions", json={"title": "Send test"}).json()
            assert client.put("/api/models/active", json={"model": "auto"}).status_code == 200

            response = client.post(f"/chat/sessions/{session['id']}/messages", json={"content": "hello ultron"})
            assert response.status_code == 200
            assert "event: chat-tool" in response.text
            assert "event: chat-message" in response.text
            assert "event: chat-done" in response.text
            assert "Answering: hello ultron" in response.text

            saved = client.get(f"/chat/sessions/{session['id']}/messages").json()
            roles = [m["role"] for m in saved]
            assert roles == ["user", "tool", "assistant"]
            assert saved[0]["content"] == "hello ultron"
            assert saved[2]["content"] == "Answering: hello ultron"
            assert selected_models == ["qwen3:30b"]

            missing = client.post("/chat/sessions/does-not-exist/messages", json={"content": "hi"})
            assert missing.status_code == 404
    finally:
        app.dependency_overrides.clear()


def test_send_chat_message_replays_tool_calls_with_real_engine(tmp_path: Path, monkeypatch):
    # Regression test for the history-corruption bug: before Fix 1, api.py rebuilt
    # history as [{"role": m.role, "content": m.content} for m in ...], dropping
    # tool_calls/tool_name. That meant a tool-role message sent back to Ollama on
    # turn 2 was never preceded by a matching assistant tool_calls message, which
    # real Ollama/OpenAI-style chat APIs reject. This exercises the REAL ChatEngine
    # (no monkeypatching of turn()) through the actual API endpoint across two
    # chat turns, using httpx.MockTransport to stand in for Ollama.
    settings = Settings(
        database_path=tmp_path / "api.db",
        checkpoint_path=tmp_path / "checkpoints.db",
        projects_root=tmp_path / "projects",
        execution_provider="mock",
    )
    Repository(settings.database_path).initialize()
    app.dependency_overrides[get_settings] = lambda: settings

    captured_requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        captured_requests.append(body)
        call_number = len(captured_requests)
        if call_number == 1:
            # Turn 1, first model call: ask for a tool.
            return httpx.Response(200, json={"message": {
                "role": "assistant", "content": "",
                "tool_calls": [{"function": {"name": "mission_control", "arguments": {"action": "list"}}}],
            }})
        if call_number == 2:
            # Turn 1, after the tool result: final answer.
            return httpx.Response(200, json={"message": {"role": "assistant", "content": "Turn one answer"}})
        # Turn 2: single call, no tool needed. This is the call whose messages
        # array we inspect for well-formed tool_calls/tool history replay.
        return httpx.Response(200, json={"message": {"role": "assistant", "content": "Turn two answer"}})

    transport = httpx.MockTransport(handler)
    original_init = ChatEngine.__init__

    def patched_init(self, base_url, model, tools):
        original_init(self, base_url, model, tools)
        self._client_kwargs = {"transport": transport}

    monkeypatch.setattr(api_module.ChatEngine, "__init__", patched_init)

    try:
        with TestClient(app) as client:
            project = client.post("/projects", json={
                "name": "Chat Integration Project", "description": "",
                "workspace_path": str(tmp_path / "workspace3"), "classification": "PERSONAL",
            }).json()
            session = client.post(f"/projects/{project['id']}/chat/sessions",
                                   json={"title": "Integration test"}).json()

            first = client.post(f"/chat/sessions/{session['id']}/messages",
                                 json={"content": "please check missions"})
            assert first.status_code == 200
            assert "Turn one answer" in first.text

            second = client.post(f"/chat/sessions/{session['id']}/messages",
                                  json={"content": "and now?"})
            assert second.status_code == 200
            assert "Turn two answer" in second.text
    finally:
        app.dependency_overrides.clear()

    assert len(captured_requests) == 3
    turn_two_messages = captured_requests[2]["messages"]

    tool_indices = [i for i, m in enumerate(turn_two_messages) if m["role"] == "tool"]
    assert tool_indices, "expected turn 2's replayed history to include the turn 1 tool message"
    for index in tool_indices:
        assert index > 0, "tool message must not be the first message"
        preceding = turn_two_messages[index - 1]
        assert preceding["role"] == "assistant"
        assert preceding.get("tool_calls"), "tool message must be preceded by an assistant message carrying matching tool_calls"
        assert preceding["tool_calls"][0]["function"]["name"] == turn_two_messages[index].get("tool_name")
