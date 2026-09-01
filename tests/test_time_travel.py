from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ultron.agent_runtime import RoleResult, WorkspaceGuard
from ultron.api import app
from ultron.config import Settings, get_settings
from ultron.db import Repository
from ultron.event_bus import EventBus
from ultron.models import MissionCreate, ProjectCreate
from ultron.workflow import AutonomousMissionWorkflow


def make_repo(tmp_path: Path) -> tuple[Repository, str]:
    repo = Repository(tmp_path / "ultron.db")
    repo.initialize()
    project = repo.create_project(ProjectCreate(name="Travel", workspace_path=tmp_path / "workspace"))
    mission = repo.create_mission(
        project.id, MissionCreate(title="Build", objective="Build and verify a small local product.")
    )
    return repo, mission.id


def _mission_and_project(repo: Repository, run_id: str):
    mission = repo.get_mission(run_id)
    return mission, repo.get_project(mission.project_id)


class GreenStudio:
    """Developer plants one passing test; every other role is well-behaved."""

    async def run_role(self, mission_id, project_id, workspace, role, objective, feedback="", test_evidence=""):
        files = []
        if role in ("developer", "backend-developer"):
            files = WorkspaceGuard(workspace).write_files(
                [{"path": "test_product.py",
                  "content": "def test_product():\n    assert 2 + 2 == 4\n"}], role)
        return RoleResult(role, f"{role} done", files, "NOT_APPLICABLE", "")

    async def run_specialist(self, mission_id, project_id, workspace, role, name, purpose, skills, objective,
                             feedback="", test_evidence=""):
        return await self.run_role(mission_id, project_id, workspace, role, objective, feedback, test_evidence)


class CrashInjected(BaseException):
    """Simulates process death: not catchable by ordinary error handling."""


@pytest.mark.asyncio
async def test_crash_mid_run_resumes_forward_and_keeps_prefix_immutable(tmp_path: Path):
    repo, run_id = make_repo(tmp_path)
    crashed_studio = GreenStudio()
    healthy_specialist = crashed_studio.run_specialist

    async def dying_specialist(mission_id, project_id, workspace, role, name, purpose, skills,
                               objective, feedback="", test_evidence=""):
        if role == "backend-developer":
            raise CrashInjected("process died mid-team-execution")
        return await healthy_specialist(mission_id, project_id, workspace, role, name, purpose,
                                        skills, objective, feedback, test_evidence)

    crashed_studio.run_specialist = dying_specialist
    first = AutonomousMissionWorkflow(repo, crashed_studio, tmp_path / "checkpoints.db", event_bus=EventBus())

    with pytest.raises(CrashInjected):
        await first.start(*_mission_and_project(repo, run_id))

    assert repo.get_mission(run_id).status.value == "RUNNING"
    prefix = [(event.id, event.hash) for event in repo.run_events(run_id)]
    assert len(prefix) >= 2

    resumed = AutonomousMissionWorkflow(repo, GreenStudio(), tmp_path / "checkpoints.db", event_bus=EventBus())
    result = await resumed.resume(repo.get_mission(run_id))

    assert result.status.value == "COMPLETED"
    suffix = repo.run_events(run_id)
    assert [(event.id, event.hash) for event in suffix][:len(prefix)] == prefix
    verdict = repo.verify_event_chain(run_id)
    assert verdict["ok"] is True and verdict["checked"] == len(suffix)
    assert suffix[-1].kind == "run.completed"


@pytest.mark.asyncio
async def test_start_from_state_reruns_edited_past_as_fork(tmp_path: Path):
    repo, run_id = make_repo(tmp_path)
    source = repo.get_mission(run_id)
    fork_mission = repo.create_mission(
        source.project_id,
        MissionCreate(title="Build (fork)", objective="Build and verify a small local product."),
    )
    workflow = AutonomousMissionWorkflow(repo, GreenStudio(), tmp_path / "checkpoints.db", event_bus=EventBus())
    result = await workflow.start_from_state({
        "mission_id": fork_mission.id,
        "project_id": source.project_id,
        "objective": fork_mission.objective,
        "workspace_path": str(repo.get_project(source.project_id).workspace_path),
        "current_node": "intake",
        "iteration": 7,
        "feedback": "operator edit: skip ahead",
    })

    assert result.status.value == "COMPLETED"
    kinds = [event.kind for event in repo.run_events(fork_mission.id)]
    assert kinds[0] == "run.started"
    assert kinds[-1] == "run.completed"


def test_timeline_verify_and_fork_endpoints(tmp_path: Path):
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
                "name": "Time Travel", "workspace_path": str(tmp_path / "ws"),
            }).json()
            mission = client.post(f"/projects/{project['id']}/missions", json={
                "title": "Source run", "objective": "Exercise time travel surfaces end to end.",
            }).json()

            source_repo = Repository(settings.database_path)
            source_repo.set_setting("active_model", "fake-model")
            source_repo.append_run_event(mission["id"], "node.started", "intake", {"node": "intake"})
            source_repo.append_run_event(mission["id"], "log", "runtime", {"i": 1})

            timeline = client.get(f"/runs/{mission['id']}/timeline")
            assert timeline.status_code == 200
            events = timeline.json()["events"]
            assert timeline.json()["count"] >= 1
            assert all(event["hash"] for event in events)
            assert events[0]["parent_hash"] is None

            verify = client.get(f"/runs/{mission['id']}/verify")
            assert verify.json() == {"ok": True, "checked": len(events),
                                     "broken_at": None, "reason": None}

            fork_point = events[-1]["id"]
            forked = client.post(f"/runs/{mission['id']}/fork", json={
                "event_id": fork_point,
                "title": "Rewound",
                "edits": {"iteration": 9},
            })
            assert forked.status_code == 202
            body = forked.json()
            assert body["source_run"] == mission["id"]
            assert body["source_event_id"] == fork_point
            assert body["edited_keys"] == ["iteration"]
            assert body["fork_run_id"] != mission["id"]

            assert client.get(f"/runs/{body['fork_run_id']}").status_code == 200
            assert any(run["id"] == body["fork_run_id"] for run in client.get("/runs").json())

            missing = client.post(f"/runs/{mission['id']}/fork", json={"event_id": 999999})
            assert missing.status_code == 404
            assert client.get("/runs/nope/timeline").status_code == 404
            assert client.get("/runs/nope/verify").status_code == 404
    finally:
        app.dependency_overrides.pop(get_settings, None)


