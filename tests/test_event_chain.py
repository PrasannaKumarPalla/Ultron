import json
import sqlite3
from pathlib import Path

from ultron.db import Repository
from ultron.models import MissionCreate, ProjectCreate


def make_repo(tmp_path: Path) -> tuple[Repository, str]:
    repo = Repository(tmp_path / "ultron.db")
    repo.initialize()
    project = repo.create_project(ProjectCreate(name="Chain", workspace_path=tmp_path / "workspace"))
    mission = repo.create_mission(
        project.id, MissionCreate(title="Build", objective="Build and verify a small local product.")
    )
    return repo, mission.id


def make_second_run(repo: Repository, tmp_path: Path) -> str:
    project = repo.create_project(ProjectCreate(name="Chain2", workspace_path=tmp_path / "workspace-2"))
    mission = repo.create_mission(
        project.id, MissionCreate(title="Build again", objective="Build and verify another local product.")
    )
    return mission.id


def test_events_form_a_per_run_hash_chain(tmp_path: Path):
    repo, run_id = make_repo(tmp_path)

    first = repo.append_run_event(run_id, "node.started", "intake", {"node": "intake"})
    second = repo.append_run_event(run_id, "token", "developer", {"index": 1, "text": "{"})

    assert first.parent_hash is None
    assert second.parent_hash == first.hash
    assert len(first.hash) == 64

    other_run = make_second_run(repo, tmp_path)
    twin = repo.append_run_event(other_run, "node.started", "intake", {"node": "intake"})
    assert twin.hash != first.hash  # chains are scoped per run


def test_verify_detects_tampering_at_the_exact_link(tmp_path: Path):
    repo, run_id = make_repo(tmp_path)
    for index in range(4):
        repo.append_run_event(run_id, "log", "runtime", {"i": index})

    assert repo.verify_event_chain(run_id) == {
        "ok": True, "checked": 4, "broken_at": None, "reason": None}

    with repo.connect() as db:
        db.execute("UPDATE events SET payload_json=? WHERE id=?",
                   (json.dumps({"i": "tampered"}), 2))

    verdict = repo.verify_event_chain(run_id)
    assert verdict["ok"] is False
    assert verdict["checked"] == 1
    assert verdict["broken_at"] == 2


def test_large_payloads_spill_to_blobs_and_round_trip(tmp_path: Path):
    repo, run_id = make_repo(tmp_path)
    big_payload = {"snapshot": "x" * 100_000}

    event = repo.append_run_event(run_id, "tool.completed", "test-runner", big_payload)

    assert event.blob_ref is not None
    assert repo.blobs.has(event.blob_ref)
    with repo.connect() as db:
        row = db.execute("SELECT payload_json FROM events WHERE id=?", (event.id,)).fetchone()
        stored = json.loads(row["payload_json"])
    assert stored["blob"] == event.blob_ref
    assert stored["size"] > 100_000
    assert len(stored["preview"]) <= 512

    resolved = repo.run_events(run_id)[-1]
    assert resolved.payload == big_payload


def test_small_payloads_stay_inline(tmp_path: Path):
    repo, run_id = make_repo(tmp_path)

    event = repo.append_run_event(run_id, "log", "runtime", {"small": True})

    assert event.blob_ref is None
    assert repo.run_events(run_id)[-1].payload == {"small": True}


def test_initialize_migrates_legacy_events_table_additively(tmp_path: Path):
    db_path = tmp_path / "legacy.db"
    connection = sqlite3.connect(db_path)
    connection.executescript(
        """
        CREATE TABLE events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            agent TEXT NOT NULL,
            kind TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            ts TEXT NOT NULL
        );
        INSERT INTO events(run_id, agent, kind, payload_json, ts)
        VALUES('old-run', 'intake', 'node.started', '{}', '2026-01-01T00:00:00+00:00');
        """
    )
    connection.commit()
    connection.close()

    repo = Repository(db_path)
    repo.initialize()

    legacy = repo.run_events("old-run")[0]
    assert legacy.kind == "node.started"
    assert legacy.hash is None  # pre-chain rows stay readable, unverified

    appended = repo.append_run_event("old-run", "log", "runtime", {"after": "migration"})
    assert appended.parent_hash is None  # chain starts fresh after the gap

    verdict = repo.verify_event_chain("old-run")
    assert verdict == {"ok": False, "checked": 0, "broken_at": 1,
                       "reason": "missing hash"}


def test_blob_root_is_overrideable(tmp_path: Path):
    blob_root = tmp_path / "elsewhere" / "blobs"
    repo = Repository(tmp_path / "db" / "ultron.db", blob_root=blob_root)
    repo.initialize()

    event = repo.append_run_event("run-x", "log", "runtime", {"big": "y" * 70_000})

    assert list(blob_root.rglob(event.blob_ref)) != []


def test_event_timeline_returns_parsed_datetimes(tmp_path):
    from datetime import datetime

    repo, run_id = make_repo(tmp_path)
    repo.append_run_event(run_id, "log", "runtime", {"x": 1})
    timeline = repo.event_timeline(run_id)
    assert timeline and isinstance(timeline[0]["ts"], datetime)
