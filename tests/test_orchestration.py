import json
import os
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
import yaml

from ultron.agent_runtime import OllamaAgentStudio, RoleResult, WorkspaceGuard
from ultron.api import app
from ultron.config import Settings, get_settings
from ultron.db import Repository
from ultron.event_bus import EventBus
from ultron.models import MissionCreate, ProjectCreate
from ultron.role_registry import RoleRegistry
from ultron.runs import RunManager
from ultron.workflow import AutonomousMissionWorkflow


def make_repo(tmp_path: Path) -> tuple[Repository, str]:
    repo = Repository(tmp_path / "ultron.db")
    repo.initialize()
    project = repo.create_project(ProjectCreate(name="Orch", workspace_path=tmp_path / "workspace"))
    mission = repo.create_mission(
        project.id, MissionCreate(title="Build", objective="Build and verify a small local product.")
    )
    return repo, mission.id


def _mission_and_project(repo: Repository, run_id: str):
    mission = repo.get_mission(run_id)
    return mission, repo.get_project(mission.project_id)


class SpecialistFailingStudio:
    """Specialist plants a failing test; every other role behaves."""

    def __init__(self):
        self.calls: list[str] = []

    async def run_role(self, mission_id, project_id, workspace, role, objective, feedback="", test_evidence=""):
        self.calls.append(role)
        files = []
        if role == "developer":
            files = WorkspaceGuard(workspace).write_files(
                [{"path": "test_product.py", "content": "def test_product():\n    assert 2 + 2 == 4\n"}], role)
        return RoleResult(role, f"{role} done", files, "NOT_APPLICABLE", "")

    async def run_specialist(self, mission_id, project_id, workspace, role, name, purpose, skills, objective,
                             feedback="", test_evidence=""):
        self.calls.append(role)
        if role == "backend-developer":
            WorkspaceGuard(workspace).write_files(
                [{"path": "test_product.py", "content": "def test_product():\n    assert False\n"}], role)
        return RoleResult(role, f"{role} done", [], "NOT_APPLICABLE", "")


@pytest.mark.asyncio
async def test_critic_reviews_developer_before_tester_when_enabled(tmp_path: Path):
    repo, run_id = make_repo(tmp_path)
    studio = SpecialistFailingStudio()
    workflow = AutonomousMissionWorkflow(repo, studio, tmp_path / "checkpoints.db",
                                         event_bus=EventBus(), enable_critic=True)

    result = await workflow.start(*_mission_and_project(repo, run_id))

    assert result.status.value == "COMPLETED"
    assert "developer" in studio.calls and "critic" in studio.calls
    assert studio.calls.index("developer") < studio.calls.index("critic")


@pytest.mark.asyncio
async def test_critic_disabled_by_default_keeps_direct_loop(tmp_path: Path):
    repo, run_id = make_repo(tmp_path)
    studio = SpecialistFailingStudio()
    workflow = AutonomousMissionWorkflow(repo, studio, tmp_path / "checkpoints.db")

    await workflow.start(*_mission_and_project(repo, run_id))

    assert "critic" not in studio.calls


@pytest.mark.asyncio
async def test_reviewer_node_logs_retry_with_different_role_and_refines_feedback(tmp_path: Path):
    repo, run_id = make_repo(tmp_path)
    bus = EventBus()
    studio = SpecialistFailingStudio()

    async def reviewer_role(*args, **kwargs):
        return RoleResult("reviewer", "reviewed", [], "NOT_APPLICABLE", "Fix VALUE to equal 42.")

    workflow = AutonomousMissionWorkflow(repo, studio, tmp_path / "checkpoints.db", event_bus=bus)
    workflow.studio.run_role = reviewer_role
    state = {"mission_id": run_id, "project_id": "p", "objective": "obj",
             "workspace_path": str(tmp_path / "workspace"), "iteration": 1, "feedback": "tests failed"}

    output = await workflow._reviewer(state)

    assert output["feedback"] == "Fix VALUE to equal 42."
    assert workflow._route_after_test({"test_passed": False, "iteration": 1}) == "repair_review"
    assert workflow._route_after_test({"test_passed": False, "iteration": 5}) == "complete"
    events = repo.events(run_id)
    reviewer_events = [e for e in events if e.kind == "repair.reviewer"]
    assert len(reviewer_events) == 1
    assert reviewer_events[0].payload["strategy"] == "retry-with-different-role"


def write_roles_file(path: Path, prompt: str) -> None:
    path.write_text(yaml.safe_dump({
        "developer": {
            "name": "Senior Developer",
            "system_prompt": prompt,
            "tools": ["read", "write"],
            "model": None,
            "desk_position": {"x": 5, "y": 1},
        }
    }), encoding="utf-8")


def test_role_registry_loads_yaml_and_hot_reloads(tmp_path: Path):
    roles_path = tmp_path / "roles.yaml"
    write_roles_file(roles_path, "v1 prompt")
    registry = RoleRegistry(roles_path)

    spec = registry.get("developer")
    assert spec.name == "Senior Developer"
    assert spec.system_prompt == "v1 prompt"
    assert spec.tools == ("read", "write")
    assert spec.desk_position == {"x": 5, "y": 1}

    write_roles_file(roles_path, "v2 prompt")
    later = time.time() + 5
    os.utime(roles_path, (later, later))
    spec = registry.get("developer")
    assert spec.system_prompt == "v2 prompt"

    registry.reload()
    assert registry.get("missing-role") is None


def test_run_manager_token_budget_warns_at_80_percent_then_enforces():
    manager = RunManager()
    manager.register("run-1", token_budget=10)

    assert manager.record_tokens("run-1", 7) == ""
    assert manager.record_tokens("run-1", 1) == "warn"

    from ultron.event_bus import BudgetExhausted
    with pytest.raises(BudgetExhausted):
        manager.record_tokens("run-1", 3)
    assert manager.budget_state("run-1")["used"] == 11

    assert manager.record_tokens("unbudgeted", 9999) == ""


class PayloadCapturingClient:
    last_payload = None

    def __init__(self, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def stream(self, method, url, **kwargs):
        type(self).last_payload = kwargs["json"]

        class _Response:
            def raise_for_status(self):
                pass

            async def aiter_lines(self):
                raw = json.dumps({"summary": "ok", "files": [], "verdict": "NOT_APPLICABLE", "feedback": ""})
                yield json.dumps({"message": {"content": raw}, "done": True})

        class _Ctx:
            async def __aenter__(self):
                return _Response()

            async def __aexit__(self, *exc):
                return False

        return _Ctx()


@pytest.mark.asyncio
async def test_stream_request_carries_deterministic_seed(tmp_path: Path, monkeypatch):
    repo, run_id = make_repo(tmp_path)
    PayloadCapturingClient.last_payload = None
    monkeypatch.setattr("ultron.agent_runtime.httpx.AsyncClient", PayloadCapturingClient)
    studio = OllamaAgentStudio("http://ollama.invalid", "fake-model", repo)

    assert OllamaAgentStudio._seed_for(run_id) == OllamaAgentStudio._seed_for(run_id)
    assert OllamaAgentStudio._seed_for("other-mission") != OllamaAgentStudio._seed_for(run_id)

    await studio.run_role(run_id, "project", tmp_path / "workspace", "tester", "Build it.")

    payload = PayloadCapturingClient.last_payload
    assert payload["options"]["seed"] == OllamaAgentStudio._seed_for(run_id)
    started = [e for e in repo.events(run_id) if e.kind == "agent.started"]
    assert started[0].payload["seed"] == payload["options"]["seed"]


def test_api_roles_endpoint_serves_registry_desks(tmp_path: Path):
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
            body = client.get("/api/roles").json()
        by_id = {role["id"]: role for role in body["roles"]}
        assert by_id["developer"]["desk_position"] == {"x": 5, "y": 1}
        assert "critic" in by_id and "reviewer" in by_id
    finally:
        app.dependency_overrides.pop(get_settings, None)