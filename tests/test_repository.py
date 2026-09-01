from datetime import timedelta
from pathlib import Path
import sqlite3

import pytest

from ultron.db import Repository, utcnow
from ultron.models import Classification, MemoryCreate, MissionCreate, MissionStatus, ProjectCreate


def test_close_releases_connections_on_windows(tmp_path: Path):
    repo = Repository(tmp_path / "ultron.db")
    repo.initialize()
    repo.append_run_event("run-close", "log", "runtime", {"x": 1})
    repo.close()

    # After close, the database file can be deleted (no open handle on Windows).
    for name in ("ultron.db", "ultron.db-wal", "ultron.db-shm"):
        path = tmp_path / name
        if path.exists():
            path.unlink()


def test_project_mission_and_events(tmp_path: Path):
    repo = Repository(tmp_path / "ultron.db")
    repo.initialize()
    project = repo.create_project(
        ProjectCreate(
            name="Test Project",
            description="Isolated fixture",
            workspace_path=tmp_path / "workspace",
            classification=Classification.PERSONAL,
        )
    )
    mission = repo.create_mission(
        project.id,
        MissionCreate(title="Build fixture", objective="Build and verify the test fixture."),
    )
    assert mission.status is MissionStatus.QUEUED
    assert repo.get_project(project.id) == project
    assert repo.list_missions(project.id)[0].id == mission.id
    assert repo.events(mission.id)[0].kind == "mission.created"


def _project(repo: Repository, tmp_path: Path, name: str = "Memory Project"):
    return repo.create_project(
        ProjectCreate(name=name, description="", workspace_path=tmp_path / name, classification=Classification.PERSONAL)
    )


def _memory(**overrides):
    defaults = dict(scope="project", role="supervisor", content="Some fact", provenance="test", confidence=1.0)
    defaults.update(overrides)
    return MemoryCreate(**defaults)


def test_supersede_memory_replaces_active_record(tmp_path: Path):
    repo = Repository(tmp_path / "ultron.db")
    repo.initialize()
    project = _project(repo, tmp_path)
    old = repo.add_memory(project.id, _memory(content="Use REST for the API"))

    new = repo.supersede_memory(old.id, "Use GraphQL for the API", "architect")

    active = repo.memories(project.id)
    contents = [m.content for m in active]
    assert "Use REST for the API" not in contents
    assert "Use GraphQL for the API" in contents
    assert new.id != old.id

    context = repo.memory_context(project.id)
    context_contents = [m.content for m in context]
    assert "Use REST for the API" not in context_contents
    assert "Use GraphQL for the API" in context_contents


def test_memories_relevance_ranking_beats_recency(tmp_path: Path):
    repo = Repository(tmp_path / "ultron.db")
    repo.initialize()
    project = _project(repo, tmp_path)

    with repo.connect() as db:
        old_ts = (utcnow() - timedelta(days=60)).isoformat()
        db.execute(
            "INSERT INTO memories VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            ("old-relevant", project.id, "project", "supervisor",
             "Deploy uses kubernetes with blue-green rollout", "test", 0.5, "PERSONAL", "active", old_ts, None, None),
        )
        recent_ts = utcnow().isoformat()
        db.execute(
            "INSERT INTO memories VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            ("recent-irrelevant", project.id, "project", "supervisor",
             "Team prefers dark mode UI", "test", 0.5, "PERSONAL", "active", recent_ts, None, None),
        )

    ranked = repo.memories(project.id, query="kubernetes blue-green rollout")
    assert ranked[0].id == "old-relevant"


def test_memory_context_stops_before_including_everything(tmp_path: Path):
    repo = Repository(tmp_path / "ultron.db")
    repo.initialize()
    project = _project(repo, tmp_path)
    for i in range(10):
        repo.add_memory(project.id, _memory(content="X" * 1000, provenance=f"item-{i}"))

    context = repo.memory_context(project.id, max_chars=3000)
    assert len(context) < 10
    assert len(context) >= 1


def test_expired_memories_excluded(tmp_path: Path):
    repo = Repository(tmp_path / "ultron.db")
    repo.initialize()
    project = _project(repo, tmp_path)
    repo.add_memory(project.id, _memory(content="Still valid", expires_at=utcnow() + timedelta(days=1)))
    repo.add_memory(project.id, _memory(content="Already expired", expires_at=utcnow() - timedelta(days=1)))

    active = repo.memories(project.id)
    contents = [m.content for m in active]
    assert "Still valid" in contents
    assert "Already expired" not in contents

    context_contents = [m.content for m in repo.memory_context(project.id)]
    assert "Already expired" not in context_contents


def test_purge_expired_memories_removes_and_counts(tmp_path: Path):
    repo = Repository(tmp_path / "ultron.db")
    repo.initialize()
    project = _project(repo, tmp_path)
    repo.add_memory(project.id, _memory(content="Keep me"))
    repo.add_memory(project.id, _memory(content="Expired one", expires_at=utcnow() - timedelta(days=1)))
    repo.add_memory(project.id, _memory(content="Expired two", expires_at=utcnow() - timedelta(days=2)))

    purged = repo.purge_expired_memories()

    assert purged == 2
    with repo.connect() as db:
        remaining = db.execute("SELECT content FROM memories").fetchall()
    assert [row[0] for row in remaining] == ["Keep me"]


from ultron.models import ChatSessionCreate


def test_chat_session_lifecycle(tmp_path: Path):
    repo = Repository(tmp_path / "ultron.db")
    repo.initialize()
    project = _project(repo, tmp_path, name="Chat Project")

    session = repo.create_chat_session(project.id, ChatSessionCreate(title="First chat"))
    assert session.project_id == project.id
    assert session.archived_at is None

    repo.add_chat_message(session.id, "user", "hello")
    repo.add_chat_message(session.id, "assistant", "", tool_calls='[{"function": {"name": "web_search"}}]')
    repo.add_chat_message(session.id, "tool", '{"ok": true}', tool_name="web_search")
    repo.add_chat_message(session.id, "assistant", "hi there")

    messages = repo.chat_messages(session.id)
    assert [m.role for m in messages] == ["user", "assistant", "tool", "assistant"]
    assert messages[1].tool_calls == '[{"function": {"name": "web_search"}}]'
    assert messages[2].tool_name == "web_search"
    assert messages[3].tool_calls is None

    assert repo.list_chat_sessions(project.id) == [session]

    archived = repo.archive_chat_session(session.id)
    assert archived.archived_at is not None
    assert repo.list_chat_sessions(project.id) == []
    assert repo.list_chat_sessions(project.id, include_archived=True) == [archived]

    unarchived = repo.unarchive_chat_session(session.id)
    assert unarchived.archived_at is None
    assert repo.list_chat_sessions(project.id) == [unarchived]


def test_general_chat_session_has_no_workspace(tmp_path: Path):
    repo = Repository(tmp_path / "ultron.db")
    repo.initialize()

    session = repo.create_chat_session(None, ChatSessionCreate(title="General chat"))

    assert session.project_id is None
    assert repo.list_chat_sessions(None) == [session]
    repo.add_chat_message(session.id, "user", "hello")
    assert repo.chat_messages(session.id)[0].content == "hello"


def test_existing_chat_schema_migrates_without_losing_messages(tmp_path: Path):
    database = tmp_path / "legacy.db"
    with sqlite3.connect(database) as db:
        db.executescript(
            """
            CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL,
                workspace_path TEXT NOT NULL UNIQUE, classification TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE chat_sessions (id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL REFERENCES projects(id), title TEXT NOT NULL,
                created_at TEXT NOT NULL, archived_at TEXT);
            CREATE TABLE chat_messages (id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL REFERENCES chat_sessions(id), role TEXT NOT NULL,
                content TEXT NOT NULL, tool_name TEXT, tool_calls TEXT, created_at TEXT NOT NULL);
            INSERT INTO projects VALUES ('p1', 'Legacy', '', 'C:/legacy', 'PERSONAL', '2026-01-01T00:00:00+00:00');
            INSERT INTO chat_sessions VALUES ('s1', 'p1', 'Existing chat', '2026-01-01T00:00:00+00:00', NULL);
            INSERT INTO chat_messages VALUES ('m1', 's1', 'user', 'keep me', NULL, NULL, '2026-01-01T00:00:00+00:00');
            """
        )

    repo = Repository(database)
    repo.initialize()

    assert repo.get_chat_session("s1").project_id == "p1"
    assert repo.chat_messages("s1")[0].content == "keep me"
    assert repo.create_chat_session(None, ChatSessionCreate(title="General")).project_id is None


def test_archive_missing_chat_session_raises(tmp_path: Path):
    repo = Repository(tmp_path / "ultron.db")
    repo.initialize()
    with pytest.raises(KeyError):
        repo.archive_chat_session("does-not-exist")


def test_delete_project_with_file_snapshots_succeeds(tmp_path: Path):
    # Regression: file_snapshots reference missions(id) with foreign_keys=ON,
    # so deleting a project whose missions wrote files used to raise IntegrityError.
    repo = Repository(tmp_path / "ultron.db")
    repo.initialize()
    project = _project(repo, tmp_path, name="Snapshot Project")
    mission = repo.create_mission(project.id, MissionCreate(title="Build", objective="Build and verify a small product."))
    repo.record_file_snapshot(mission.id, "app.py", "before", "after")

    result = repo.delete_project(project.id)

    assert result["missions_deleted"] == 1
    assert repo.get_project(project.id) is None
    assert repo.get_mission(mission.id) is None


def test_delete_project_with_layered_memory_succeeds(tmp_path: Path):
    # Regression: episodic_memories / semantic_lessons also FK to projects(id);
    # a project with recalled memory used to fail workspace deletion.
    repo = Repository(tmp_path / "ultron.db")
    repo.initialize()
    project = _project(repo, tmp_path, name="Memory Project")
    repo.add_episodic(project.id, "the architect chose SQLite", [0.1, 0.2, 0.3])
    repo.add_lesson(project.id, "prefer SQLite for single-operator tools", source_count=1)

    result = repo.delete_project(project.id)

    assert repo.get_project(project.id) is None
    assert result["project"].name == "Memory Project"


def test_events_after_returns_only_newer_events(tmp_path: Path):
    repo = Repository(tmp_path / "ultron.db")
    repo.initialize()
    project = _project(repo, tmp_path, name="Events Project")
    mission = repo.create_mission(project.id, MissionCreate(title="Build", objective="Build and verify a small product."))
    all_events = repo.events(mission.id)

    newer = repo.events_after(mission.id, all_events[0].id)

    assert [event.id for event in newer] == [event.id for event in all_events[1:]]
