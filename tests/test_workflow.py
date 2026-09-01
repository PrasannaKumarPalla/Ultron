from pathlib import Path

import pytest

from ultron.agent_runtime import RoleResult, WorkspaceGuard
from ultron.db import Repository
from ultron.models import MissionCreate, MissionStatus, ProjectCreate
from ultron.providers import MockExecutionProvider
from ultron.workflow import AutonomousMissionWorkflow, DurableMissionWorkflow


class SecretPlantingStudio:
    """Fake studio whose developer role always writes a real test plus a planted secret."""

    async def run_role(self, mission_id, project_id, workspace, role, objective, feedback="", test_evidence=""):
        files = []
        if role == "developer":
            guard = WorkspaceGuard(workspace)
            files = guard.write_files([
                {"path": "test_product.py", "content": "def test_product():\n    assert 2 + 2 == 4\n"},
                {"path": "config.py", "content": "AWS_KEY = \"AKIAABCDEFGHIJKLMNOP\"\n"},
            ], role)
        verdict = "PASS" if role == "tester" else "NOT_APPLICABLE"
        return RoleResult(role, f"{role} complete", files, verdict, "")

    async def run_specialist(self, mission_id, project_id, workspace, role, name, purpose, skills, objective, feedback="", test_evidence=""):
        files = []
        if role == "backend-developer":
            files = WorkspaceGuard(workspace).write_files([
                {"path": "test_product.py", "content": "def test_product():\n    assert 2 + 2 == 4\n"},
                {"path": "config.py", "content": "AWS_KEY = \"AKIAABCDEFGHIJKLMNOP\"\n"},
            ], role)
        return RoleResult(role, f"{role} complete", files, "NOT_APPLICABLE", "")


@pytest.mark.asyncio
async def test_bootstrap_workflow_records_execution_boundary(tmp_path: Path):
    repo = Repository(tmp_path / "ultron.db")
    repo.initialize()
    project = repo.create_project(
        ProjectCreate(name="Workflow", workspace_path=tmp_path / "workspace")
    )
    mission = repo.create_mission(
        project.id,
        MissionCreate(title="Execute", objective="Submit a real execution request safely."),
    )
    workflow = DurableMissionWorkflow(
        repo, MockExecutionProvider(), tmp_path / "checkpoints.db"
    )
    result = await workflow.start(mission, project)
    assert result.status is MissionStatus.BLOCKED
    assert result.current_node == "execution_integration"
    assert [event.kind for event in repo.events(mission.id)] == [
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
    checkpoint = await workflow.checkpoint_state(mission.id)
    assert checkpoint is not None
    assert checkpoint["execution_provider"] == "mock"
    assert checkpoint["current_node"] == "execution_integration"


@pytest.mark.asyncio
async def test_security_gate_blocks_mission_when_secret_is_planted(tmp_path: Path):
    repo = Repository(tmp_path / "ultron.db")
    repo.initialize()
    project = repo.create_project(ProjectCreate(name="Autonomous", workspace_path=tmp_path / "workspace"))
    mission = repo.create_mission(project.id, MissionCreate(title="Build", objective="Build and verify a small local product."))
    workflow = AutonomousMissionWorkflow(repo, SecretPlantingStudio(), tmp_path / "checkpoints.db")

    result = await workflow.start(mission, project)

    assert result.status is MissionStatus.FAILED
    assert result.current_node == "complete"
    security_events = [event for event in repo.events(mission.id) if event.kind == "security.scanned"]
    assert security_events
    assert all(event.payload["passed"] is False for event in security_events)
    assert all(event.payload["secrets_findings"] >= 1 for event in security_events)
