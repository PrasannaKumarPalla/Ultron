from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ultron.api import app
from ultron.config import Settings, get_settings
from ultron.db import Repository
from ultron.memory_layers import LayeredMemory
from ultron.models import MissionCreate, ProjectCreate


@pytest.fixture
def client(tmp_path):
    settings = Settings(
        database_path=tmp_path / "api.db",
        checkpoint_path=tmp_path / "checkpoints.db",
        projects_root=tmp_path / "projects",
        execution_provider="mock",
    )
    repo = Repository(settings.database_path)
    repo.initialize()
    project = repo.create_project(ProjectCreate(name="P3", workspace_path=tmp_path / "ws"))
    app.dependency_overrides[get_settings] = lambda: settings
    with TestClient(app) as tc:
        yield tc, repo, project.id, settings
    app.dependency_overrides.pop(get_settings, None)


def test_tools_endpoint_serves_strict_grammars(client):
    tc, _, _, _ = client

    body = tc.get("/api/tools").json()

    names = {item["name"] for item in body["tools"]}
    assert "workspace.read_file" in names
    assert all(item["format"]["additionalProperties"] is False for item in body["tools"])


def test_pool_status_shape(client):
    tc, _, _, _ = client

    snapshot = tc.get("/api/models/pool").json()

    assert snapshot["size"] >= 1
    assert isinstance(snapshot["models"], dict)


def test_memory_endpoints_round_trip(client):
    tc, repo, project_id, _ = client
    memory = LayeredMemory(repo)
    for index in range(4):
        memory.observe(project_id, f"deployment rollback after bad release {index}")

    recalled = tc.get(f"/projects/{project_id}/memory", params={"q": "deployment rollback"}).json()
    assert len(recalled["episodic"]) >= 1
    assert recalled["lessons"] == []

    consolidated = tc.post("/api/memory/consolidate").json()
    lessons_after = tc.get(f"/projects/{project_id}/memory").json()["lessons"]
    assert consolidated["lessons_created"] == len(lessons_after)

    assert tc.get("/projects/zzz/memory").status_code == 404


def test_trace_endpoint_builds_spans_from_events(client):
    tc, repo, project_id, _ = client
    mission = repo.create_mission(
        project_id,
        MissionCreate(title="Trace", objective="Trace this run end to end please."))
    repo.append_run_event(mission.id, "run.started", "supervisor", {})
    repo.append_run_event(mission.id, "node.started", "intake", {"node": "intake"})
    repo.append_run_event(mission.id, "node.completed", "intake", {"node": "intake"})
    repo.append_run_event(mission.id, "run.completed", "supervisor", {"status": "COMPLETED"})

    body = tc.get(f"/runs/{mission.id}/trace").json()

    kinds = {span["kind"] for span in body["spans"]}
    assert {"run", "node"} <= kinds
    assert body["llm_calls"] == 0
    assert tc.get("/runs/missing/trace").status_code == 404
