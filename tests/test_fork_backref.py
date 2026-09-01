from pathlib import Path

from ultron.db import Repository
from ultron.models import MissionCreate, ProjectCreate
from ultron.providers import MockExecutionProvider
from ultron.workflow import DurableMissionWorkflow


def _setup(tmp_path: Path):
    repo = Repository(tmp_path / "ultron.db")
    repo.initialize()
    project = repo.create_project(
        ProjectCreate(name="Fork", workspace_path=tmp_path / "workspace")
    )
    source = repo.create_mission(
        project.id, MissionCreate(title="Source", objective="Build the original run.")
    )
    fork = repo.create_mission(
        project.id, MissionCreate(title="Fork", objective="Build the original run.")
    )
    repo.add_event(fork.id, "run.forked", "operator",
                   {"source_run": source.id, "source_event_id": 7})
    workflow = DurableMissionWorkflow(repo, MockExecutionProvider(), tmp_path / "ckpt.db")
    return repo, source, fork, workflow


def test_failed_fork_writes_back_reference_on_source(tmp_path: Path):
    repo, source, fork, workflow = _setup(tmp_path)

    workflow._note_fork_outcome(fork.id, "failed", "boom")

    back = [e for e in repo.events(source.id) if e.kind == "run.fork_failed"]
    assert len(back) == 1
    assert back[0].payload == {
        "fork_run": fork.id, "outcome": "failed",
        "source_event_id": 7, "error": "boom",
    }


def test_note_fork_outcome_is_a_noop_for_a_non_fork_run(tmp_path: Path):
    repo, source, fork, workflow = _setup(tmp_path)

    workflow._note_fork_outcome(source.id, "failed", "boom")

    assert not [e for e in repo.events(source.id) if e.kind == "run.fork_failed"]
