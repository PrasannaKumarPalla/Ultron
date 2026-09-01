import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ultron.agent_runtime import OllamaAgentStudio
from ultron.api import app
from ultron.config import Settings, get_settings
from ultron.db import Repository
from ultron.event_bus import EventBus, replay_state
from ultron.models import EventKind, MissionCreate, ProjectCreate
from ultron.providers import MockExecutionProvider
from ultron.runs import RunManager
from ultron.workflow import DurableMissionWorkflow


def make_repo(tmp_path: Path) -> tuple[Repository, str]:
    repo = Repository(tmp_path / "ultron.db")
    repo.initialize()
    project = repo.create_project(ProjectCreate(name="Harness", workspace_path=tmp_path / "workspace"))
    mission = repo.create_mission(
        project.id, MissionCreate(title="Build", objective="Build and verify a small local product.")
    )
    return repo, mission.id


def _mission_and_project(repo: Repository, run_id: str):
    mission = repo.get_mission(run_id)
    return mission, repo.get_project(mission.project_id)


@pytest.mark.asyncio
async def test_publish_persists_to_events_and_fans_out_to_subscribers(tmp_path: Path):
    repo, run_id = make_repo(tmp_path)
    bus = EventBus()
    iterator = bus.subscribe(run_id).__aiter__()

    bus.publish(repo, run_id, EventKind.NODE_STARTED, "intake", {"node": "intake"})
    bus.publish(repo, run_id, EventKind.TOKEN, "developer", {"index": 1, "text": "{"})

    live_event = await iterator.__anext__()
    assert live_event.kind == "node.started"
    assert live_event.payload == {"node": "intake"}

    persisted = repo.run_events(run_id)
    assert [(e.kind, e.agent) for e in persisted] == [("node.started", "intake"), ("token", "developer")]

    mirrored = repo.events(run_id)
    assert [e.kind for e in mirrored] == ["mission.created", "node.started"]


def test_replay_state_folds_event_history():
    events = [
        type("E", (), {"kind": "agent.completed", "agent": "developer", "payload": {}})(),
        type("E", (), {"kind": "tests.completed", "agent": "test-runner",
                       "payload": {"passed": True, "manual_checks": False}})(),
        type("E", (), {"kind": "security.scanned", "agent": "security-gate", "payload": {"passed": True}})(),
        type("E", (), {"kind": "node.completed", "agent": "security_gate", "payload": {"node": "security_gate"}})(),
    ]
    assert replay_state(events) == {
        "iteration": 1,
        "test_passed": True,
        "manual_checks": False,
        "security_passed": True,
        "current_node": "security_gate",
    }


@pytest.mark.asyncio
async def test_nodes_auto_emit_lifecycle_events_and_checkpoint_breadcrumbs(tmp_path: Path):
    repo, run_id = make_repo(tmp_path)
    workflow = DurableMissionWorkflow(repo, MockExecutionProvider(), tmp_path / "checkpoints.db", event_bus=EventBus())

    result = await workflow.start(*_mission_and_project(repo, run_id))

    assert result.status.value == "BLOCKED"
    kinds = [e.kind for e in repo.run_events(run_id)]
    assert kinds[0] == "run.started"
    assert kinds[-1] == "run.completed"
    assert kinds.count("node.started") == 3
    assert kinds.count("node.completed") == 3
    assert "node.error" not in kinds
    breadcrumb = repo.latest_checkpoint(run_id)
    assert breadcrumb["node"] == "execution_integration"
    assert breadcrumb["state"]["current_node"] == "execution_integration"


@pytest.mark.asyncio
async def test_kill_switch_aborts_run_at_node_boundary_without_node_error(tmp_path: Path):
    repo, run_id = make_repo(tmp_path)
    manager = RunManager()
    manager.register(run_id)
    manager.cancel(run_id)
    workflow = DurableMissionWorkflow(repo, MockExecutionProvider(), tmp_path / "checkpoints.db",
                                      event_bus=EventBus(), run_manager=manager)

    from ultron.event_bus import RunCancelled
    with pytest.raises(RunCancelled):
        await workflow.start(*_mission_and_project(repo, run_id))

    kinds = [e.kind for e in repo.run_events(run_id)]
    assert "node.started" in kinds
    assert "node.error" not in kinds
    assert "run.cancelled" in kinds
    assert "run.completed" not in kinds


class FakeResponse:
    def __init__(self, lines):
        self._lines = lines

    def raise_for_status(self):
        pass

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class CancelAfterFirstChunkResponse(FakeResponse):
    def __init__(self, lines, manager: RunManager, run_id: str):
        super().__init__(lines)
        self.manager = manager
        self.run_id = run_id
        self.served = 0

    async def aiter_lines(self):
        for line in self._lines:
            self.served += 1
            if self.served > 1:
                self.manager.cancel(self.run_id)
            yield line


class FakeStreamContext:
    def __init__(self, response):
        self.response = response

    async def __aenter__(self):
        return self.response

    async def __aexit__(self, *exc):
        return False


class FakeAsyncClient:
    response = None

    def __init__(self, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def stream(self, method, url, **kwargs):
        return FakeStreamContext(type(self).response)


def schema_response_chunks():
    raw = json.dumps({"summary": "did it", "files": [], "verdict": "NOT_APPLICABLE", "feedback": ""})
    mid = len(raw) // 2
    return [json.dumps({"message": {"content": raw[:mid]}, "done": False}),
            json.dumps({"message": {"content": raw[mid:]}, "done": True})]


@pytest.mark.asyncio
async def test_streaming_loop_emits_token_events(tmp_path: Path, monkeypatch):
    repo, run_id = make_repo(tmp_path)
    FakeAsyncClient.response = FakeResponse(schema_response_chunks())
    monkeypatch.setattr("ultron.agent_runtime.httpx.AsyncClient", FakeAsyncClient)
    studio = OllamaAgentStudio("http://ollama.invalid", "fake-model", repo, event_bus=EventBus())

    result = await studio.run_role(run_id, "project", tmp_path / "workspace", "tester", "Build it.")

    assert result.summary == "did it"
    token_events = [e for e in repo.run_events(run_id) if e.kind == "token"]
    assert [e.payload["index"] for e in token_events] == [1, 2]
    assert "".join(e.payload["text"] for e in token_events).startswith('{"summary"')


@pytest.mark.asyncio
async def test_streaming_loop_honours_kill_switch_midstream(tmp_path: Path, monkeypatch):
    repo, run_id = make_repo(tmp_path)
    manager = RunManager()
    manager.register(run_id)
    FakeAsyncClient.response = CancelAfterFirstChunkResponse(schema_response_chunks(), manager, run_id)
    monkeypatch.setattr("ultron.agent_runtime.httpx.AsyncClient", FakeAsyncClient)
    studio = OllamaAgentStudio("http://ollama.invalid", "fake-model", repo,
                               event_bus=EventBus(), run_manager=manager)

    from ultron.event_bus import RunCancelled
    with pytest.raises(RunCancelled):
        await studio.run_role(run_id, "project", tmp_path / "workspace", "tester", "Build it.")


def test_runs_api_list_detail_cancel_and_sse(tmp_path: Path):
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
                "name": "Runs API", "workspace_path": str(tmp_path / "ws"),
            }).json()
            mission = client.post(f"/projects/{project['id']}/missions", json={
                "title": "Run surface", "objective": "Exercise the runs API surface end to end.",
            }).json()
            run_id = mission["id"]

            assert any(r["id"] == run_id for r in client.get("/runs").json())

            detail = client.get(f"/runs/{run_id}")
            assert detail.status_code == 200
            body = detail.json()
            assert body["run"]["id"] == run_id
            assert body["checkpoint"] is None
            assert body["cancel_requested"] is False

            cancelled = client.post(f"/runs/{run_id}/cancel", json={"reason": "phase 1 verification"})
            assert cancelled.status_code == 200
            assert cancelled.json()["status"] == "CANCELLED"

            detail = client.get(f"/runs/{run_id}").json()
            assert detail["cancel_requested"] is True
            assert detail["event_count"] >= 1

            with client.stream("GET", f"/runs/{run_id}/events") as response:
                payload = b"".join(chunk for chunk in response.iter_raw()).decode("utf-8")
            assert "event: harness-event" in payload
            assert "run.cancelled" in payload
            assert "event: run.finished" in payload
    finally:
        app.dependency_overrides.pop(get_settings, None)