from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
import uuid
import weakref
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from .models import (
    Approval,
    ApprovalCreate,
    ApprovalDecision,
    ChatMessage,
    ChatMessageCreate,
    ChatSession,
    ChatSessionCreate,
    MemoryCreate,
    MemoryRecord,
    Mission,
    MissionCreate,
    MissionEvent,
    MissionStatus,
    Project,
    ProjectCreate,
    RunEvent,
    TeamMember,
)
from .store import BLOB_SPILL_THRESHOLD, BlobStore


logger = logging.getLogger(__name__)


def utcnow() -> datetime:
    return datetime.now(UTC)


def _close_slots(connections: list) -> None:
    for connection in connections:
        try:
            connection.close()
        except Exception:
            pass


class Repository:
    """Bootstrap persistence. The API isolates SQLite behind this interface."""

    def __init__(self, path: Path, blob_root: Path | None = None):
        self.path = path.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.blobs = BlobStore(blob_root or self.path.parent / "blobs")
        self._local = threading.local()
        self._slots: list[sqlite3.Connection] = []
        # Close every opened connection once the Repository is collected so
        # Windows does not keep file handles (and pytest temp dirs) open.
        self._finalizer = weakref.finalize(self, _close_slots, self._slots)

    def close(self) -> None:
        self._finalizer()

    def _connection(self) -> sqlite3.Connection:
        """One connection per thread, reused across calls: event appends were
        paying full connect+teardown cost per event (~17ms p50)."""
        connection = getattr(self._local, "conn", None)
        if connection is None:
            connection = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            self._local.conn = connection
            self._slots.append(connection)
        return connection

    @contextmanager
    def connect(self):
        connection = self._connection()
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise

    def initialize(self) -> None:
        with self.connect() as db:
            db.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS projects (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL,
                    workspace_path TEXT NOT NULL UNIQUE,
                    classification TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS missions (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL REFERENCES projects(id),
                    title TEXT NOT NULL,
                    objective TEXT NOT NULL,
                    status TEXT NOT NULL,
                    current_node TEXT NOT NULL,
                    graph_version TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS mission_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    mission_id TEXT NOT NULL REFERENCES missions(id),
                    kind TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_missions_project_created
                    ON missions(project_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_events_mission_id
                    ON mission_events(mission_id, id);
                CREATE TABLE IF NOT EXISTS approvals (
                    id TEXT PRIMARY KEY,
                    mission_id TEXT NOT NULL REFERENCES missions(id),
                    action TEXT NOT NULL,
                    risk TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    rationale TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    decided_at TEXT
                );
                CREATE TABLE IF NOT EXISTS memories (
                    id TEXT PRIMARY KEY,
                    project_id TEXT REFERENCES projects(id),
                    scope TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    provenance TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    sensitivity TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    superseded_by TEXT REFERENCES memories(id),
                    expires_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_approvals_mission ON approvals(mission_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_memories_project ON memories(project_id, created_at DESC);
                CREATE TABLE IF NOT EXISTS mission_team (
                    id TEXT PRIMARY KEY,
                    mission_id TEXT NOT NULL REFERENCES missions(id),
                    role_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    purpose TEXT NOT NULL,
                    skills TEXT NOT NULL,
                    permissions TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(mission_id, role_id)
                );
                CREATE INDEX IF NOT EXISTS idx_mission_team_sequence ON mission_team(mission_id, sequence);
                CREATE TABLE IF NOT EXISTS app_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS file_snapshots (
                    id TEXT PRIMARY KEY,
                    mission_id TEXT NOT NULL REFERENCES missions(id),
                    event_id INTEGER REFERENCES mission_events(id),
                    path TEXT NOT NULL,
                    before_content TEXT NOT NULL,
                    after_content TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_file_snapshots_mission ON file_snapshots(mission_id, created_at);
                CREATE TABLE IF NOT EXISTS chat_sessions (
                    id TEXT PRIMARY KEY,
                    project_id TEXT REFERENCES projects(id),
                    title TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    archived_at TEXT
                );
                CREATE TABLE IF NOT EXISTS chat_messages (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL REFERENCES chat_sessions(id),
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    tool_name TEXT,
                    tool_calls TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_chat_sessions_project ON chat_sessions(project_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_chat_messages_session ON chat_messages(session_id, created_at);
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    agent TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    ts TEXT NOT NULL,
                    hash TEXT,
                    parent_hash TEXT,
                    blob_ref TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id, id);
                CREATE TABLE IF NOT EXISTS checkpoints (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    node TEXT NOT NULL,
                    state_json TEXT NOT NULL,
                    ts TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_checkpoints_run ON checkpoints(run_id, id);
                CREATE TABLE IF NOT EXISTS episodic_memories (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL REFERENCES projects(id),
                    text TEXT NOT NULL,
                    embedding TEXT NOT NULL,
                    consolidated INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_episodic_project ON episodic_memories(project_id, created_at DESC);
                CREATE TABLE IF NOT EXISTS semantic_lessons (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL REFERENCES projects(id),
                    lesson TEXT NOT NULL,
                    source_count INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    UNIQUE(project_id, lesson)
                );
                CREATE TABLE IF NOT EXISTS model_catalog (
                    key TEXT PRIMARY KEY,
                    id TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    source_provider TEXT,
                    context INTEGER,
                    capabilities_json TEXT NOT NULL,
                    tokens_per_sec_estimate REAL,
                    free INTEGER NOT NULL,
                    refreshed_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS model_calls (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT,
                    provider TEXT NOT NULL,
                    upstream TEXT,
                    model TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    latency_ms INTEGER,
                    tokens_in INTEGER,
                    tokens_out INTEGER,
                    compressed_tokens INTEGER,
                    cache_hit INTEGER NOT NULL DEFAULT 0,
                    fallback_reason TEXT,
                    cost_usd REAL NOT NULL DEFAULT 0,
                    ts TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_model_calls_ts ON model_calls(ts);
                """
            )
            columns = {row[1] for row in db.execute("PRAGMA table_info(memories)").fetchall()}
            if "superseded_by" not in columns:
                db.execute("ALTER TABLE memories ADD COLUMN superseded_by TEXT REFERENCES memories(id)")
            if "expires_at" not in columns:
                db.execute("ALTER TABLE memories ADD COLUMN expires_at TEXT")
            chat_message_columns = {row[1] for row in db.execute("PRAGMA table_info(chat_messages)").fetchall()}
            if "tool_calls" not in chat_message_columns:
                db.execute("ALTER TABLE chat_messages ADD COLUMN tool_calls TEXT")
            chat_session_columns = {row[1]: row for row in db.execute("PRAGMA table_info(chat_sessions)").fetchall()}
            if chat_session_columns["project_id"][3]:
                self._migrate_general_chat_sessions(db)
            event_columns = {row[1] for row in db.execute("PRAGMA table_info(events)").fetchall()}
            for column in ("hash", "parent_hash", "blob_ref"):
                if column not in event_columns:
                    # `column` is one of the three literals above, not input.
                    db.execute(f"ALTER TABLE events ADD COLUMN {column} TEXT")  # nosemgrep: python.lang.security.audit.formatted-sql-query.formatted-sql-query,python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query

    @staticmethod
    def _migrate_general_chat_sessions(db: sqlite3.Connection) -> None:
        db.execute("PRAGMA foreign_keys=OFF")
        db.executescript(
            """
            ALTER TABLE chat_messages RENAME TO chat_messages_legacy;
            ALTER TABLE chat_sessions RENAME TO chat_sessions_legacy;
            CREATE TABLE chat_sessions (
                id TEXT PRIMARY KEY,
                project_id TEXT REFERENCES projects(id),
                title TEXT NOT NULL,
                created_at TEXT NOT NULL,
                archived_at TEXT
            );
            CREATE TABLE chat_messages (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL REFERENCES chat_sessions(id),
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                tool_name TEXT,
                tool_calls TEXT,
                created_at TEXT NOT NULL
            );
            INSERT INTO chat_sessions SELECT id, project_id, title, created_at, archived_at FROM chat_sessions_legacy;
            INSERT INTO chat_messages SELECT id, session_id, role, content, tool_name, tool_calls, created_at FROM chat_messages_legacy;
            DROP TABLE chat_messages_legacy;
            DROP TABLE chat_sessions_legacy;
            CREATE INDEX idx_chat_sessions_project ON chat_sessions(project_id, created_at DESC);
            CREATE INDEX idx_chat_messages_session ON chat_messages(session_id, created_at);
            """
        )
        db.execute("PRAGMA foreign_keys=ON")

    def ping(self) -> bool:
        with self.connect() as db:
            return db.execute("SELECT 1").fetchone()[0] == 1

    def get_setting(self, key: str, default: str | None = None) -> str | None:
        with self.connect() as db:
            row = db.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT INTO app_settings(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def replace_catalog(self, entries: list[dict]) -> None:
        refreshed = utcnow().isoformat()
        with self.connect() as db:
            db.execute("DELETE FROM model_catalog")
            for entry in entries:
                key = f"{entry['provider']}:{entry['id']}"
                db.execute(
                    "INSERT OR REPLACE INTO model_catalog VALUES (?,?,?,?,?,?,?,?,?)",
                    (key, entry["id"], entry["provider"], entry.get("source_provider"),
                     entry.get("context"), json.dumps(entry.get("capabilities", [])),
                     entry.get("tokens_per_sec_estimate"), int(bool(entry.get("free"))),
                     refreshed))

    def catalog_entries(self) -> list[dict]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM model_catalog ORDER BY provider, id").fetchall()
        return [{"id": row["id"], "provider": row["provider"],
                 "source_provider": row["source_provider"], "context": row["context"],
                 "capabilities": json.loads(row["capabilities_json"]),
                 "tokens_per_sec_estimate": row["tokens_per_sec_estimate"],
                 "free": bool(row["free"]), "refreshed_at": row["refreshed_at"]}
                for row in rows]

    def record_model_call(self, *, run_id: str | None, provider: str, upstream: str | None,
                          model: str, mode: str, latency_ms: int | None,
                          tokens_in: int | None, tokens_out: int | None,
                          compressed_tokens: int | None, cache_hit: bool = False,
                          fallback_reason: str | None = None, cost_usd: float = 0.0) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT INTO model_calls(run_id, provider, upstream, model, mode, latency_ms,"
                " tokens_in, tokens_out, compressed_tokens, cache_hit, fallback_reason,"
                " cost_usd, ts) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, provider, upstream, model, mode, latency_ms, tokens_in, tokens_out,
                 compressed_tokens, int(cache_hit), fallback_reason, cost_usd,
                 utcnow().isoformat()))

    def usage_summary(self) -> dict:
        with self.connect() as db:
            split = [dict(row) for row in db.execute(
                "SELECT provider, COUNT(*) AS calls,"
                " COALESCE(SUM(tokens_in),0) AS tokens_in,"
                " COALESCE(SUM(tokens_out),0) AS tokens_out,"
                " COALESCE(SUM(compressed_tokens),0) AS compressed_tokens,"
                " COALESCE(AVG(latency_ms),0) AS avg_latency_ms"
                " FROM model_calls GROUP BY provider").fetchall()]
            mix = [dict(row) for row in db.execute(
                "SELECT upstream, COUNT(*) AS calls FROM model_calls"
                " WHERE provider='omniroute' AND upstream IS NOT NULL"
                " GROUP BY upstream ORDER BY calls DESC").fetchall()]
            cost_row = db.execute(
                "SELECT COALESCE(SUM(cost_usd),0) AS total_cost FROM model_calls").fetchone()
            fallbacks = [dict(row) for row in db.execute(
                "SELECT fallback_reason, COUNT(*) AS count FROM model_calls"
                " WHERE fallback_reason IS NOT NULL GROUP BY fallback_reason").fetchall()]
        total_compressed = sum(row["compressed_tokens"] for row in split)
        return {"split": split, "upstream_mix": mix,
                "total_cost_usd": round(cost_row["total_cost"], 6),
                "fallbacks": fallbacks,
                "tokens_saved_by_compression": total_compressed}


    def create_project(self, request: ProjectCreate) -> Project:
        workspace = request.workspace_path.expanduser().resolve()
        workspace.mkdir(parents=True, exist_ok=True)
        project = Project(
            id=uuid.uuid4().hex,
            name=request.name.strip(),
            description=request.description.strip(),
            workspace_path=workspace,
            classification=request.classification,
            created_at=utcnow(),
        )
        with self.connect() as db:
            db.execute(
                "INSERT INTO projects VALUES (?, ?, ?, ?, ?, ?)",
                (
                    project.id,
                    project.name,
                    project.description,
                    str(project.workspace_path),
                    project.classification,
                    project.created_at.isoformat(),
                ),
            )
        return project

    def list_projects(self) -> list[Project]:
        with self.connect() as db:
            records = db.execute(
                "SELECT * FROM projects ORDER BY created_at DESC"
            ).fetchall()
        return [self._project(row) for row in records]

    def get_project(self, project_id: str) -> Project | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        return self._project(row) if row else None

    def create_mission(self, project_id: str, request: MissionCreate) -> Mission:
        timestamp = utcnow()
        mission = Mission(
            id=uuid.uuid4().hex,
            project_id=project_id,
            title=request.title.strip(),
            objective=request.objective.strip(),
            status=MissionStatus.QUEUED,
            current_node="intake",
            graph_version="bootstrap-v1",
            created_at=timestamp,
            updated_at=timestamp,
        )
        with self.connect() as db:
            db.execute(
                "INSERT INTO missions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    mission.id,
                    mission.project_id,
                    mission.title,
                    mission.objective,
                    mission.status,
                    mission.current_node,
                    mission.graph_version,
                    mission.created_at.isoformat(),
                    mission.updated_at.isoformat(),
                ),
            )
        self.add_event(mission.id, "mission.created", "operator", {"title": mission.title})
        return mission

    def list_missions(self, project_id: str) -> list[Mission]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM missions WHERE project_id=? ORDER BY created_at DESC",
                (project_id,),
            ).fetchall()
        return [self._mission(row) for row in rows]

    def get_mission(self, mission_id: str) -> Mission | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM missions WHERE id=?", (mission_id,)).fetchone()
        return self._mission(row) if row else None

    def unfinished_missions(self) -> list[Mission]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM missions WHERE status IN (?,?,?) ORDER BY updated_at",
                (MissionStatus.RUNNING, MissionStatus.QUEUED, MissionStatus.BLOCKED),
            ).fetchall()
        return [self._mission(row) for row in rows]

    def list_runs(self) -> list[Mission]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM missions ORDER BY updated_at DESC").fetchall()
        return [self._mission(row) for row in rows]

    def transition(self, mission_id: str, status: MissionStatus, node: str) -> Mission:
        timestamp = utcnow()
        with self.connect() as db:
            db.execute(
                "UPDATE missions SET status=?, current_node=?, updated_at=? WHERE id=?",
                (status, node, timestamp.isoformat(), mission_id),
            )
        mission = self.get_mission(mission_id)
        if not mission:
            raise KeyError(mission_id)
        return mission

    def _encode_payload(self, payload: dict) -> str:
        """JSON-encode a payload, spilling anything over the threshold to the blob
        store and leaving a {"blob", "size", "preview"} marker in its place."""
        encoded = json.dumps(payload)
        if len(encoded) > BLOB_SPILL_THRESHOLD:
            ref = self.blobs.put_text(encoded)
            return json.dumps({"blob": ref, "size": len(encoded), "preview": encoded[:512]})
        return encoded

    def add_event(self, mission_id: str, kind: str, actor: str, payload: dict) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT INTO mission_events(mission_id,kind,actor,payload,created_at) VALUES(?,?,?,?,?)",
                (mission_id, kind, actor, self._encode_payload(payload), utcnow().isoformat()),
            )

    def events(self, mission_id: str) -> list[MissionEvent]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM mission_events WHERE mission_id=? ORDER BY id",
                (mission_id,),
            ).fetchall()
        return [
            MissionEvent(
                id=row["id"], mission_id=row["mission_id"], kind=row["kind"],
                actor=row["actor"], payload=self._decode_payload(row["payload"]),
                created_at=datetime.fromisoformat(row["created_at"]),
            )
            for row in rows
        ]

    def events_after(self, mission_id: str, event_id: int) -> list[MissionEvent]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM mission_events WHERE mission_id=? AND id>? ORDER BY id",
                (mission_id, event_id),
            ).fetchall()
        return [
            MissionEvent(
                id=row["id"], mission_id=row["mission_id"], kind=row["kind"],
                actor=row["actor"], payload=self._decode_payload(row["payload"]),
                created_at=datetime.fromisoformat(row["created_at"]),
            )
            for row in rows
        ]

    @staticmethod
    def _event_hash(parent_hash: str, run_id: str, agent: str, kind: str,
                    payload_json: str, ts: datetime) -> str:
        canonical = json.dumps(
            {"run_id": run_id, "agent": agent, "kind": kind,
             "payload": payload_json, "ts": ts.isoformat()},
            sort_keys=True, separators=(",", ":"),
        )
        return hashlib.sha256((parent_hash + canonical).encode("utf-8")).hexdigest()

    def append_run_event(self, run_id: str, kind: str, agent: str, payload: dict) -> RunEvent:
        ts = utcnow()
        encoded = self._encode_payload(payload)
        blob_ref: str | None = None
        marker = json.loads(encoded)
        if isinstance(marker, dict) and isinstance(marker.get("blob"), str):
            blob_ref = marker["blob"]
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            parent_row = db.execute(
                "SELECT hash FROM events WHERE run_id=? ORDER BY id DESC LIMIT 1",
                (run_id,),
            ).fetchone()
            parent_hash = parent_row["hash"] if parent_row and parent_row["hash"] else ""
            digest = self._event_hash(parent_hash, run_id, agent, kind, encoded, ts)
            cursor = db.execute(
                "INSERT INTO events(run_id,agent,kind,payload_json,ts,hash,parent_hash,blob_ref)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (run_id, agent, kind, encoded, ts.isoformat(), digest,
                 parent_hash or None, blob_ref),
            )
        return RunEvent(id=cursor.lastrowid, run_id=run_id, agent=agent, kind=kind,
                        payload=payload, ts=ts, hash=digest,
                        parent_hash=parent_hash or None, blob_ref=blob_ref)

    def _decode_payload(self, raw: str) -> dict:
        data = json.loads(raw)
        if isinstance(data, dict) and "blob" in data and isinstance(data.get("blob"), str):
            return self.blobs.resolve(data)
        return data

    def run_events(self, run_id: str, after_id: int = 0) -> list[RunEvent]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM events WHERE run_id=? AND id>? ORDER BY id",
                (run_id, after_id),
            ).fetchall()
        return [
            RunEvent(
                id=row["id"], run_id=row["run_id"], agent=row["agent"], kind=row["kind"],
                payload=self._decode_payload(row["payload_json"]),
                ts=datetime.fromisoformat(row["ts"]), hash=row["hash"],
                parent_hash=row["parent_hash"], blob_ref=row["blob_ref"],
            )
            for row in rows
        ]

    def verify_event_chain(self, run_id: str) -> dict:
        """Walk the per-run hash chain; report the first tampered or missing link."""
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM events WHERE run_id=? ORDER BY id", (run_id,)
            ).fetchall()
        parent_hash = ""
        for index, row in enumerate(rows):
            if not row["hash"]:
                return {"ok": False, "checked": index, "broken_at": row["id"],
                        "reason": "missing hash"}
            expected = self._event_hash(parent_hash, row["run_id"], row["agent"],
                                        row["kind"], row["payload_json"],
                                        datetime.fromisoformat(row["ts"]))
            if expected != row["hash"]:
                return {"ok": False, "checked": index, "broken_at": row["id"],
                        "reason": "hash mismatch"}
            if row["parent_hash"] != (parent_hash or None):
                return {"ok": False, "checked": index, "broken_at": row["id"],
                        "reason": "parent link mismatch"}
            parent_hash = row["hash"]
        return {"ok": True, "checked": len(rows), "broken_at": None, "reason": None}

    def add_episodic(self, project_id: str, text: str, embedding: list[float]) -> str:
        memory_id = uuid.uuid4().hex
        with self.connect() as db:
            db.execute(
                "INSERT INTO episodic_memories(id,project_id,text,embedding,consolidated,created_at)"
                " VALUES(?,?,?,?,0,?)",
                (memory_id, project_id, text, json.dumps(embedding), utcnow().isoformat()))
        return memory_id

    def episodic_rows(self, project_id: str, consolidated: bool | None = None) -> list[dict]:
        query = "SELECT * FROM episodic_memories WHERE project_id=?"
        params: list = [project_id]
        if consolidated is not None:
            query += " AND consolidated=?"
            params.append(1 if consolidated else 0)
        query += " ORDER BY created_at DESC"
        with self.connect() as db:
            rows = db.execute(query, params).fetchall()
        return [{"id": row["id"], "text": row["text"],
                 "embedding": json.loads(row["embedding"]),
                 "consolidated": bool(row["consolidated"]),
                 "created_at": row["created_at"]} for row in rows]

    def mark_episodic_consolidated(self, ids: list[str]) -> None:
        if not ids:
            return
        with self.connect() as db:
            db.executemany("UPDATE episodic_memories SET consolidated=1 WHERE id=?",
                           [(memory_id,) for memory_id in ids])

    def add_lesson(self, project_id: str, lesson: str, source_count: int = 1) -> bool:
        """Insert a distilled lesson; returns False when it already exists."""
        try:
            with self.connect() as db:
                db.execute(
                    "INSERT INTO semantic_lessons(id,project_id,lesson,source_count,created_at)"
                    " VALUES(?,?,?,?,?)",
                    (uuid.uuid4().hex, project_id, lesson, source_count, utcnow().isoformat()))
            return True
        except sqlite3.IntegrityError:
            return False

    def lessons(self, project_id: str) -> list[dict]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM semantic_lessons WHERE project_id=? ORDER BY source_count DESC, created_at DESC",
                (project_id,)).fetchall()
        return [{"id": row["id"], "lesson": row["lesson"],
                 "source_count": row["source_count"], "created_at": row["created_at"]}
                for row in rows]

    def event_timeline(self, run_id: str) -> list[dict]:
        """Hash chain metadata without decoding spilled payloads."""
        with self.connect() as db:
            rows = db.execute(
                "SELECT id, agent, kind, ts, hash, parent_hash, blob_ref,"
                " LENGTH(payload_json) AS stored_bytes"
                " FROM events WHERE run_id=? ORDER BY id", (run_id,)
            ).fetchall()
        return [
            {"id": row["id"], "kind": row["kind"], "agent": row["agent"],
             "ts": row["ts"], "hash": row["hash"], "parent_hash": row["parent_hash"],
             "blob_ref": row["blob_ref"], "stored_bytes": row["stored_bytes"]}
            for row in rows
        ]

    def event_count(self, run_id: str) -> int:
        with self.connect() as db:
            row = db.execute("SELECT COUNT(*) FROM events WHERE run_id=?", (run_id,)).fetchone()
        return int(row[0])

    def save_checkpoint(self, run_id: str, node: str, state: dict) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT INTO checkpoints(run_id,node,state_json,ts) VALUES(?,?,?,?)",
                (run_id, node, json.dumps(state), utcnow().isoformat()),
            )

    def latest_checkpoint(self, run_id: str) -> dict | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM checkpoints WHERE run_id=? ORDER BY id DESC LIMIT 1",
                (run_id,),
            ).fetchone()
        if not row:
            return None
        return {"node": row["node"], "state": json.loads(row["state_json"]), "ts": row["ts"]}

    def record_file_snapshot(self, mission_id: str, path: str, before: str, after: str) -> None:
        max_chars = 200_000
        if len(before) > max_chars:
            before = f"(content omitted: {len(before)} chars exceeds snapshot cap)"
        if len(after) > max_chars:
            after = f"(content omitted: {len(after)} chars exceeds snapshot cap)"
        with self.connect() as db:
            db.execute(
                "INSERT INTO file_snapshots(id,mission_id,event_id,path,before_content,after_content,created_at) VALUES(?,?,?,?,?,?,?)",
                (uuid.uuid4().hex, mission_id, None, path, before, after, utcnow().isoformat()),
            )

    def file_snapshots(self, mission_id: str) -> list[dict]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM file_snapshots WHERE mission_id=? ORDER BY id",
                (mission_id,),
            ).fetchall()
        return [
            {"id": row["id"], "mission_id": row["mission_id"], "event_id": row["event_id"],
             "path": row["path"], "before_content": row["before_content"], "after_content": row["after_content"],
             "created_at": row["created_at"]}
            for row in rows
        ]

    def create_approval(self, mission_id: str, request: ApprovalCreate) -> Approval:
        approval = Approval(id=uuid.uuid4().hex, mission_id=mission_id,
            action=request.action.strip(), risk=request.risk.strip(),
            decision=ApprovalDecision.PENDING, rationale="", created_at=utcnow())
        with self.connect() as db:
            db.execute("INSERT INTO approvals VALUES(?,?,?,?,?,?,?,?)", (
                approval.id, approval.mission_id, approval.action, approval.risk,
                approval.decision, approval.rationale, approval.created_at.isoformat(), None))
        self.transition(mission_id, MissionStatus.AWAITING_APPROVAL, "approval_gate")
        self.add_event(mission_id, "approval.requested", "supervisor", {"approval_id": approval.id, "action": approval.action})
        return approval

    def decide_approval(self, approval_id: str, decision: ApprovalDecision, rationale: str) -> Approval | None:
        decided_at = utcnow()
        with self.connect() as db:
            row = db.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
            if not row or row["decision"] != ApprovalDecision.PENDING:
                return None
            db.execute("UPDATE approvals SET decision=?, rationale=?, decided_at=? WHERE id=?",
                (decision, rationale.strip(), decided_at.isoformat(), approval_id))
        approval = self.get_approval(approval_id)
        if approval:
            status = MissionStatus.QUEUED if decision == ApprovalDecision.APPROVED else MissionStatus.BLOCKED
            self.transition(approval.mission_id, status, "approved" if status == MissionStatus.QUEUED else "approval_rejected")
            self.add_event(approval.mission_id, "approval.decided", "operator", {"approval_id": approval.id, "decision": decision, "rationale": rationale})
        return approval

    def get_approval(self, approval_id: str) -> Approval | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
        return Approval(**dict(row)) if row else None

    def approvals(self, mission_id: str) -> list[Approval]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM approvals WHERE mission_id=? ORDER BY created_at", (mission_id,)).fetchall()
        return [Approval(**dict(row)) for row in rows]

    def add_memory(self, project_id: str | None, request: MemoryCreate) -> MemoryRecord:
        if request.scope == "project" and not project_id:
            raise ValueError("Project-scoped memory requires a project")
        record = MemoryRecord(id=uuid.uuid4().hex, project_id=project_id, status="active", created_at=utcnow(),
                               superseded_by=None, **request.model_dump())
        with self.connect() as db:
            db.execute("INSERT INTO memories VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (
                record.id, record.project_id, record.scope, record.role, record.content,
                record.provenance, record.confidence, record.sensitivity, record.status,
                record.created_at.isoformat(), record.superseded_by,
                record.expires_at.isoformat() if record.expires_at else None,))
        return record

    def supersede_memory(self, memory_id: str, new_content: str, role: str) -> MemoryRecord:
        with self.connect() as db:
            row = db.execute("SELECT * FROM memories WHERE id=?", (memory_id,)).fetchone()
            if not row:
                raise KeyError(memory_id)
            old = MemoryRecord(**dict(row))
            new_record = MemoryRecord(
                id=uuid.uuid4().hex, project_id=old.project_id, scope=old.scope, role=role,
                content=new_content, provenance=f"Supersedes {old.id}", confidence=old.confidence,
                sensitivity=old.sensitivity, status="active", created_at=utcnow(),
                superseded_by=None, expires_at=None,
            )
            db.execute("INSERT INTO memories VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (
                new_record.id, new_record.project_id, new_record.scope, new_record.role, new_record.content,
                new_record.provenance, new_record.confidence, new_record.sensitivity, new_record.status,
                new_record.created_at.isoformat(), new_record.superseded_by, None,))
            db.execute("UPDATE memories SET status='superseded', superseded_by=? WHERE id=?", (new_record.id, memory_id))
        return new_record

    def _ranked_memories(self, project_id: str, include_global: bool, query: str | None) -> list[MemoryRecord]:
        sql = "SELECT * FROM memories WHERE project_id=? AND status='active'"
        params: tuple = (project_id,)
        if include_global:
            sql = "SELECT * FROM memories WHERE (project_id=? OR (project_id IS NULL AND scope='global')) AND status='active'"
        with self.connect() as db:
            rows = db.execute(sql, params).fetchall()
        now = utcnow()
        records = [MemoryRecord(**dict(row)) for row in rows]
        records = [r for r in records if r.expires_at is None or r.expires_at > now]

        tokens = set(query.lower().split()) if query else set()

        def score(record: MemoryRecord) -> float:
            age_days = max((now - record.created_at).total_seconds() / 86400.0, 0.0)
            recency = 0.5 ** (age_days / 7.0)  # halves every 7 days
            base = record.confidence * 0.6 + recency * 0.4
            if tokens:
                content_tokens = set(record.content.lower().split())
                overlap = len(tokens & content_tokens)
                relevance = overlap / len(tokens)
                base += relevance
            return base

        return sorted(records, key=score, reverse=True)

    def memories(self, project_id: str, include_global: bool = False, query: str | None = None) -> list[MemoryRecord]:
        return self._ranked_memories(project_id, include_global, query)

    def memory_context(self, project_id: str, objective: str | None = None, max_chars: int = 3000,
                        include_global: bool = True) -> list[MemoryRecord]:
        ranked = self._ranked_memories(project_id, include_global, objective)
        selected: list[MemoryRecord] = []
        used = 0
        for record in ranked:
            length = len(record.content)
            if selected and used + length > max_chars:
                break
            selected.append(record)
            used += length
            if used >= max_chars:
                break
        return selected

    def purge_expired_memories(self) -> int:
        # Hard-delete: expired memories carry no supersession value once past
        # expiry, and keeping them around would just bloat the table with
        # rows every query has to filter back out.
        now = utcnow().isoformat()
        with self.connect() as db:
            cursor = db.execute("DELETE FROM memories WHERE expires_at IS NOT NULL AND expires_at <= ?", (now,))
            return cursor.rowcount

    def save_team(self, mission_id: str, members: list[dict]) -> list[TeamMember]:
        created = utcnow()
        with self.connect() as db:
            db.execute("DELETE FROM mission_team WHERE mission_id=?", (mission_id,))
            for sequence, member in enumerate(members):
                db.execute("INSERT INTO mission_team VALUES(?,?,?,?,?,?,?,?,?,?)", (
                    uuid.uuid4().hex, mission_id, member["role_id"], member["name"], member["purpose"],
                    json.dumps(member["skills"]), json.dumps(member["permissions"]), sequence,
                    "PLANNED", created.isoformat()))
        return self.team(mission_id)

    def team(self, mission_id: str) -> list[TeamMember]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM mission_team WHERE mission_id=? ORDER BY sequence", (mission_id,)).fetchall()
        return [TeamMember(**{**dict(row), "skills": json.loads(row["skills"]), "permissions": json.loads(row["permissions"])}) for row in rows]

    def update_team_member(self, mission_id: str, role_id: str, status: str) -> None:
        with self.connect() as db:
            db.execute("UPDATE mission_team SET status=? WHERE mission_id=? AND role_id=?", (status, mission_id, role_id))

    def create_chat_session(self, project_id: str | None, request: ChatSessionCreate) -> ChatSession:
        session = ChatSession(id=uuid.uuid4().hex, project_id=project_id, title=request.title.strip(),
                               created_at=utcnow(), archived_at=None)
        with self.connect() as db:
            db.execute("INSERT INTO chat_sessions VALUES (?,?,?,?,?)", (
                session.id, session.project_id, session.title, session.created_at.isoformat(), None))
        return session

    def list_chat_sessions(self, project_id: str | None, include_archived: bool = False) -> list[ChatSession]:
        sql = "SELECT * FROM chat_sessions WHERE project_id IS NULL" if project_id is None else "SELECT * FROM chat_sessions WHERE project_id=?"
        params = () if project_id is None else (project_id,)
        if not include_archived:
            sql += " AND archived_at IS NULL"
        sql += " ORDER BY created_at DESC"
        with self.connect() as db:
            rows = db.execute(sql, params).fetchall()
        return [self._chat_session(row) for row in rows]

    def get_chat_session(self, session_id: str) -> ChatSession | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM chat_sessions WHERE id=?", (session_id,)).fetchone()
        return self._chat_session(row) if row else None

    def archive_chat_session(self, session_id: str) -> ChatSession:
        with self.connect() as db:
            db.execute("UPDATE chat_sessions SET archived_at=? WHERE id=?", (utcnow().isoformat(), session_id))
        session = self.get_chat_session(session_id)
        if not session:
            raise KeyError(session_id)
        return session

    def unarchive_chat_session(self, session_id: str) -> ChatSession:
        with self.connect() as db:
            db.execute("UPDATE chat_sessions SET archived_at=NULL WHERE id=?", (session_id,))
        session = self.get_chat_session(session_id)
        if not session:
            raise KeyError(session_id)
        return session

    def delete_chat_session(self, session_id: str) -> None:
        with self.connect() as db:
            db.execute("DELETE FROM chat_messages WHERE session_id=?", (session_id,))
            db.execute("DELETE FROM chat_sessions WHERE id=?", (session_id,))

    def add_chat_message(self, session_id: str, role: str, content: str, tool_name: str | None = None,
                          tool_calls: str | None = None) -> ChatMessage:
        message = ChatMessage(id=uuid.uuid4().hex, session_id=session_id, role=role, content=content,
                               tool_name=tool_name, tool_calls=tool_calls, created_at=utcnow())
        with self.connect() as db:
            db.execute("INSERT INTO chat_messages VALUES (?,?,?,?,?,?,?)", (
                message.id, message.session_id, message.role, message.content,
                message.tool_name, message.tool_calls, message.created_at.isoformat()))
        return message

    def chat_messages(self, session_id: str) -> list[ChatMessage]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM chat_messages WHERE session_id=? ORDER BY created_at, rowid", (session_id,)
            ).fetchall()
        return [ChatMessage(**dict(row)) for row in rows]

    def delete_project(self, project_id: str) -> dict:
        project = self.get_project(project_id)
        if not project:
            raise KeyError(project_id)
        with self.connect() as db:
            session_ids = [row[0] for row in db.execute("SELECT id FROM chat_sessions WHERE project_id=?", (project_id,)).fetchall()]
            for session_id in session_ids:
                db.execute("DELETE FROM chat_messages WHERE session_id=?", (session_id,))
            db.execute("DELETE FROM chat_sessions WHERE project_id=?", (project_id,))
            mission_ids = [row[0] for row in db.execute("SELECT id FROM missions WHERE project_id=?", (project_id,)).fetchall()]
            for mission_id in mission_ids:
                db.execute("DELETE FROM file_snapshots WHERE mission_id=?", (mission_id,))
                db.execute("DELETE FROM mission_team WHERE mission_id=?", (mission_id,))
                db.execute("DELETE FROM approvals WHERE mission_id=?", (mission_id,))
                db.execute("DELETE FROM mission_events WHERE mission_id=?", (mission_id,))
                db.execute("DELETE FROM events WHERE run_id=?", (mission_id,))
                db.execute("DELETE FROM checkpoints WHERE run_id=?", (mission_id,))
            db.execute("DELETE FROM memories WHERE project_id=?", (project_id,))
            existing = {row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            for layer in ("episodic_memories", "semantic_lessons"):
                if layer in existing:
                    # `layer` is one of the two literals above; project_id is bound.
                    db.execute(f"DELETE FROM {layer} WHERE project_id=?", (project_id,))  # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query,python.lang.security.audit.formatted-sql-query.formatted-sql-query
            db.execute("DELETE FROM missions WHERE project_id=?", (project_id,))
            db.execute("DELETE FROM projects WHERE id=?", (project_id,))
        return {"project": project, "missions_deleted": len(mission_ids)}

    @staticmethod
    def _project(row: sqlite3.Row) -> Project:
        return Project(**dict(row))

    @staticmethod
    def _mission(row: sqlite3.Row) -> Mission:
        return Mission(**dict(row))

    @staticmethod
    def _chat_session(row: sqlite3.Row) -> ChatSession:
        return ChatSession(**dict(row))
