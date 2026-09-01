from pathlib import Path

import pytest

from ultron.agent_runtime import RoleResult, WorkspaceGuard
from ultron.db import Repository
from ultron.event_bus import EventBus
from ultron.models import MissionCreate, ProjectCreate
from ultron.shadow_git import CANDIDATE_BRANCH, MAIN_BRANCH, ShadowGit
from ultron.workflow import AutonomousMissionWorkflow


def make_shadow(tmp_path: Path) -> tuple[ShadowGit, Path]:
    workspace = tmp_path / "ws"
    workspace.mkdir(parents=True)
    return ShadowGit(workspace), workspace


def test_ensure_creates_baseline_and_is_idempotent(tmp_path: Path):
    shadow, workspace = make_shadow(tmp_path)

    assert shadow.ensure() is True
    first = shadow.head()
    assert first is not None
    assert shadow.branch() == MAIN_BRANCH

    (workspace / "seed.txt").write_text("seed", encoding="utf-8")
    shadow.ensure()

    assert shadow.head() == first


def test_candidate_lifecycle_green_fast_forwards_workspace(tmp_path: Path):
    shadow, workspace = make_shadow(tmp_path)
    shadow.ensure()
    baseline = shadow.head()

    shadow.begin_candidate()
    assert shadow.branch() == CANDIDATE_BRANCH

    (workspace / "feature.py").write_text("VALUE = 42\n", encoding="utf-8")
    sha = shadow.candidate_commit("candidate 1")

    assert sha != baseline
    assert shadow.changed_files() == ["feature.py"]
    assert "feature.py" in shadow.diff_stat()

    forwarded = shadow.fast_forward()
    assert shadow.branch() == MAIN_BRANCH
    assert forwarded == sha
    assert (workspace / "feature.py").read_text(encoding="utf-8") == "VALUE = 42\n"
    assert shadow.changed_files() == []


def test_rollback_restores_baseline_and_next_candidate_starts_clean(tmp_path: Path):
    shadow, workspace = make_shadow(tmp_path)
    shadow.ensure()
    baseline = shadow.head()

    shadow.begin_candidate()
    (workspace / "buggy.py").write_text("BROKEN = True\n", encoding="utf-8")
    shadow.candidate_commit("red candidate")
    shadow.rollback()

    assert shadow.branch() == MAIN_BRANCH
    assert shadow.head() == baseline
    assert not (workspace / "buggy.py").exists()

    shadow.begin_candidate()
    assert not (workspace / "buggy.py").exists()


def test_begin_candidate_wipes_previous_failed_iteration(tmp_path: Path):
    shadow, workspace = make_shadow(tmp_path)
    shadow.ensure()
    shadow.begin_candidate()
    (workspace / "attempt1.py").write_text("x = 1\n", encoding="utf-8")
    shadow.candidate_commit("iter 1")

    shadow.begin_candidate()

    assert not (workspace / "attempt1.py").exists()


def test_guard_blocks_writes_into_shadow_dir(tmp_path: Path):
    guard = WorkspaceGuard(tmp_path / "workspace")

    with pytest.raises(ValueError):
        guard.resolve(".ultron-shadow/config")


def test_degrades_to_pass_through_when_git_missing(tmp_path: Path, monkeypatch):
    import ultron.shadow_git as shadow_module

    monkeypatch.setattr(shadow_module.shutil, "which", lambda name: None)
    shadow, _ = make_shadow(tmp_path)

    assert shadow.ensure() is False
    assert shadow.available is False
    assert not shadow.git_dir.exists()


@pytest.mark.asyncio
async def test_mission_green_gates_through_shadow_and_forwards(tmp_path: Path):
    repo = Repository(tmp_path / "ultron.db")
    repo.initialize()
    project = repo.create_project(ProjectCreate(name="Gated", workspace_path=tmp_path / "workspace"))
    mission = repo.create_mission(
        project.id, MissionCreate(title="Build", objective="Build and verify a small local product.")
    )

    class GreenDev:
        async def run_role(self, mission_id, project_id, workspace, role, objective, feedback="", test_evidence=""):
            files = []
            if role in ("developer", "backend-developer"):
                files = WorkspaceGuard(workspace).write_files(
                    [{"path": "test_product.py",
                      "content": "def test_product():\n    assert 2 + 2 == 4\n"}], role)
            return RoleResult(role, f"{role} done", files, "NOT_APPLICABLE", "")

        async def run_specialist(self, mission_id, project_id, workspace, role, name, purpose, skills, objective,
                                 feedback="", test_evidence=""):
            return await self.run_role(mission_id, project_id, workspace, role, objective)

    workflow = AutonomousMissionWorkflow(repo, GreenDev(), tmp_path / "checkpoints.db", event_bus=EventBus())
    result = await workflow.start(mission, project)

    assert result.status.value == "COMPLETED"
    kinds = [event.kind for event in repo.events(mission.id)]
    assert "shadow.candidate_opened" in kinds
    assert "shadow.forwarded" in kinds
    assert "shadow.rolled_back" not in kinds
    assert (tmp_path / "workspace" / "test_product.py").exists()
    assert repo.verify_event_chain(mission.id)["ok"] is True


@pytest.mark.asyncio
async def test_mission_red_rolls_back_workspace_to_baseline(tmp_path: Path):
    repo = Repository(tmp_path / "ultron.db")
    repo.initialize()
    project = repo.create_project(ProjectCreate(name="RedGate", workspace_path=tmp_path / "workspace"))
    mission = repo.create_mission(
        project.id, MissionCreate(title="Build", objective="Build and verify a small local product.")
    )

    class RedDev:
        async def run_role(self, mission_id, project_id, workspace, role, objective, feedback="", test_evidence=""):
            return RoleResult(role, f"{role} done", [], "NOT_APPLICABLE", "")

        async def run_specialist(self, mission_id, project_id, workspace, role, name, purpose, skills, objective,
                                 feedback="", test_evidence=""):
            if role == "backend-developer":
                WorkspaceGuard(workspace).write_files(
                    [{"path": "test_product.py",
                      "content": "def test_product():\n    assert False\n"}], role)
            return RoleResult(role, f"{role} done", [], "NOT_APPLICABLE", "")

    workflow = AutonomousMissionWorkflow(repo, RedDev(), tmp_path / "checkpoints.db",
                                         max_repair_loops=0, event_bus=EventBus())
    result = await workflow.start(mission, project)

    assert result.status.value == "FAILED"
    kinds = [event.kind for event in repo.events(mission.id)]
    assert "shadow.candidate_opened" in kinds
    assert "shadow.rolled_back" in kinds
    assert "shadow.forwarded" not in kinds
    assert not (tmp_path / "workspace" / "test_product.py").exists()



def test_changed_files_logs_and_returns_empty_on_git_error(tmp_path, caplog):
    import logging
    from ultron.shadow_git import ShadowGit

    shadow = ShadowGit(tmp_path)  # never ensure()d -> no candidate branch
    with caplog.at_level(logging.WARNING, logger="ultron.shadow_git"):
        assert shadow.changed_files() == []
        assert shadow.diff_stat() == ""
    assert any("failed" in r.message for r in caplog.records)
