"""Blob store GC: unreferenced blobs are swept, referenced ones survive (#19)."""

from __future__ import annotations

from pathlib import Path

from ultron.db import Repository
from ultron.models import MissionCreate, ProjectCreate


def _repo(tmp_path: Path):
    repo = Repository(tmp_path / "ultron.db")
    repo.initialize()
    project = repo.create_project(ProjectCreate(name="GC", workspace_path=tmp_path / "ws"))
    mission = repo.create_mission(
        project.id, MissionCreate(title="Build", objective="Build and verify a small product."))
    return repo, mission.id


def test_gc_keeps_referenced_blobs_and_sweeps_orphans(tmp_path):
    repo, run_id = _repo(tmp_path)

    repo.append_run_event(run_id, "log", "dev", {"x": "y" * 80_000})
    live = repo._referenced_blob_digests()
    assert len(live) == 1

    orphan = repo.blobs.put_text("z" * 80_000)
    assert repo.blobs.has(orphan)

    result = repo.gc_blobs()
    assert result["deleted"] == 1
    assert result["kept"] == 1
    assert result["freed_bytes"] >= 80_000
    assert not repo.blobs.has(orphan)
    assert repo.blobs.has(next(iter(live)))


def test_gc_picks_up_mirror_only_references(tmp_path):
    repo, run_id = _repo(tmp_path)
    # add_event (the mirror) with a large payload -> blob marker in mission_events
    repo.add_event(run_id, "agent.completed", "developer", {"big": "q" * 80_000})
    refs = repo._referenced_blob_digests()
    assert len(refs) == 1
    assert repo.gc_blobs()["deleted"] == 0


def test_gc_is_a_noop_when_everything_is_referenced(tmp_path):
    repo, run_id = _repo(tmp_path)
    repo.append_run_event(run_id, "log", "dev", {"x": "y" * 80_000})
    assert repo.gc_blobs() == {"deleted": 0, "kept": 1, "freed_bytes": 0}
