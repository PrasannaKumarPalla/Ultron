import asyncio
import shutil
from pathlib import Path

import pytest

from ultron.agent_runtime import RoleResult
from ultron.db import Repository
from ultron.models import MissionCreate, ProjectCreate
from ultron.search import SearchConfig
from ultron.workflow import AutonomousMissionWorkflow

BEAM = 3


class ParallelProbeStudio:
    """Each variant writes a file into whatever workspace it is handed and
    blocks on a barrier, so the run only completes if all variants execute
    concurrently against distinct checkouts."""

    def __init__(self, beam: int):
        self.barrier = asyncio.Barrier(beam)
        self.seen_paths: list[Path] = []

    async def run_role(self, mission_id, project_id, workspace, role, objective,
                       feedback="", test_evidence="", variant=0):
        Path(workspace, f"variant_{variant}.py").write_text(
            f"VALUE = {variant}\n", encoding="utf-8")
        self.seen_paths.append(Path(workspace))
        async with asyncio.timeout(10):
            await self.barrier.wait()
        return RoleResult("developer", f"variant {variant}",
                          [f"variant_{variant}.py"], "PASS", "")


@pytest.mark.asyncio
async def test_variants_run_in_parallel_over_isolated_worktrees(tmp_path: Path):
    if not shutil.which("git"):
        pytest.skip("git not available")
    repo = Repository(tmp_path / "ultron.db")
    repo.initialize()
    ws = tmp_path / "workspace"
    ws.mkdir()
    project = repo.create_project(ProjectCreate(name="Spec", workspace_path=ws))
    mission = repo.create_mission(project.id, MissionCreate(
        title="Build", objective="Build and verify a small local product."))
    studio = ParallelProbeStudio(BEAM)
    workflow = AutonomousMissionWorkflow(
        repo, studio, tmp_path / "ckpt.db", search=SearchConfig(beam_width=BEAM))

    state = {"mission_id": mission.id, "project_id": project.id,
             "objective": mission.objective, "workspace_path": str(ws),
             "iteration": 0, "feedback": "", "test_evidence": ""}

    out = await workflow._speculative_developer(state)

    assert out["current_node"] == "developer"
    # each variant got its own checkout, none the shared workspace
    assert len({str(p) for p in studio.seen_paths}) == BEAM
    assert all(".worktrees" in str(p) for p in studio.seen_paths)
    # worktrees cleaned up
    assert not list((ws / ".worktrees").glob("variant-*")) if (ws / ".worktrees").exists() else True
    # winner forwarded onto main
    shadow = workflow._shadow_for(str(ws))
    assert shadow.branch() == "main"
    forwarded = [e for e in repo.events(mission.id) if e.kind == "search.selected"]
    assert forwarded and forwarded[0].payload["commit"]
