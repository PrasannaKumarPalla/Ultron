from pathlib import Path

import pytest

from ultron.agent_runtime import RoleResult, WorkspaceGuard
from ultron.db import Repository
from ultron.models import MissionCreate, MissionStatus, ProjectCreate
from ultron.workflow import AutonomousMissionWorkflow


class FakeStudio:
    def __init__(self):
        self.roles: list[str] = []

    async def run_role(self, mission_id, project_id, workspace, role, objective, feedback="", test_evidence=""):
        self.roles.append(role)
        files = []
        if role == "developer":
            guard = WorkspaceGuard(workspace)
            files = guard.write_files([{"path": "test_product.py", "content": "def test_product():\n    assert 2 + 2 == 4\n"}], role)
        verdict = "PASS" if role == "tester" else "NOT_APPLICABLE"
        return RoleResult(role, f"{role} complete", files, verdict, "")

    async def run_specialist(self, mission_id, project_id, workspace, role, name, purpose, skills, objective, feedback="", test_evidence=""):
        self.roles.append(role)
        files = []
        if role == "backend-developer":
            files = WorkspaceGuard(workspace).write_files([{"path": "test_product.py", "content": "def test_product():\n    assert 2 + 2 == 4\n"}], role)
        return RoleResult(role, f"{role} complete", files, "NOT_APPLICABLE", "")


def test_workspace_guard_rejects_escape(tmp_path: Path):
    guard = WorkspaceGuard(tmp_path / "workspace")
    with pytest.raises(ValueError):
        guard.resolve("../outside.txt")
    with pytest.raises(ValueError):
        guard.resolve(".git/config")


def test_write_files_captures_before_after_for_new_file(tmp_path: Path):
    guard = WorkspaceGuard(tmp_path / "workspace")
    guard.write_files([{"path": "new_file.py", "content": "print('hi')\n"}], role="developer")
    assert guard.last_snapshots == [{"path": "new_file.py", "before": "", "after": "print('hi')\n"}]


def test_write_files_captures_before_after_for_modified_file(tmp_path: Path):
    guard = WorkspaceGuard(tmp_path / "workspace")
    guard.write_files([{"path": "existing.py", "content": "old content\n"}], role="developer")
    guard.write_files([{"path": "existing.py", "content": "new content\n"}], role="developer")
    assert guard.last_snapshots == [{"path": "existing.py", "before": "old content\n", "after": "new content\n"}]


def test_record_file_snapshot_round_trips(tmp_path: Path):
    repo = Repository(tmp_path / "ultron.db")
    repo.initialize()
    project = repo.create_project(ProjectCreate(name="Snap", workspace_path=tmp_path / "workspace"))
    mission = repo.create_mission(project.id, MissionCreate(title="Build", objective="Build and verify a small local product."))
    repo.record_file_snapshot(mission.id, "app.py", "before text", "after text")
    snapshots = repo.file_snapshots(mission.id)
    assert len(snapshots) == 1
    assert snapshots[0]["path"] == "app.py"
    assert snapshots[0]["before_content"] == "before text"
    assert snapshots[0]["after_content"] == "after text"
    assert snapshots[0]["event_id"] is None


def test_record_file_snapshot_caps_oversized_content(tmp_path: Path):
    repo = Repository(tmp_path / "ultron.db")
    repo.initialize()
    project = repo.create_project(ProjectCreate(name="Snap2", workspace_path=tmp_path / "workspace"))
    mission = repo.create_mission(project.id, MissionCreate(title="Build", objective="Build and verify a small local product."))
    oversized = "x" * 200_001
    repo.record_file_snapshot(mission.id, "big.py", oversized, "small")
    snapshots = repo.file_snapshots(mission.id)
    assert len(snapshots[0]["before_content"]) < len(oversized)
    assert "omitted" in snapshots[0]["before_content"]
    assert snapshots[0]["after_content"] == "small"


@pytest.mark.asyncio
async def test_autonomous_role_handoff_completes(tmp_path: Path):
    repo = Repository(tmp_path / "ultron.db")
    repo.initialize()
    project = repo.create_project(ProjectCreate(name="Autonomous", workspace_path=tmp_path / "workspace"))
    mission = repo.create_mission(project.id, MissionCreate(title="Build", objective="Build and verify a small local product."))
    studio = FakeStudio()
    workflow = AutonomousMissionWorkflow(repo, studio, tmp_path / "checkpoints.db")
    result = await workflow.start(mission, project)
    assert result.status is MissionStatus.COMPLETED
    assert studio.roles == ["cloud-architect", "product-manager", "backend-developer"]
    assert (project.workspace_path / "test_product.py").exists()
    assert "tests.completed" in [event.kind for event in repo.events(mission.id)]
    assert any(event.actor == "tester" and event.kind == "agent.completed" for event in repo.events(mission.id))
