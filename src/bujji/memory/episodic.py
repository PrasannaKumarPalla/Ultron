"""SQLite-based episodic memory store for session summaries.

Called from speech/pipeline.py at session end to persist conversation episodes.
No existing file. Schema: sessions(id INTEGER PK, summary TEXT, messages_json TEXT,
duration_s REAL, created_at REAL).
User instruction: do all remaining ones.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS sessions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    summary     TEXT    NOT NULL,
    messages_json TEXT  NOT NULL DEFAULT '[]',
    duration_s  REAL    NOT NULL DEFAULT 0.0,
    created_at  REAL    NOT NULL
)
"""


class EpisodicStore:
    """SQLite-backed store for session-level episodic memory.

    Parameters
    ----------
    db_path:
        Path to the SQLite database file. ``~`` is expanded automatically.
    """

    def __init__(self, db_path: str = "~/.bujji/episodic.db") -> None:
        resolved = Path(db_path).expanduser()
        resolved.parent.mkdir(parents=True, exist_ok=True)
        self._db_path = str(resolved)
        self._init_db()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        try:
            with self._connect() as conn:
                conn.execute(_CREATE_TABLE)
                conn.commit()
        except Exception as exc:
            logger.warning("EpisodicStore: failed to init DB: %s", exc)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def save_session(
        self,
        summary: str,
        messages: List[Dict[str, Any]],
        duration_s: float = 0.0,
    ) -> int:
        """Persist a session. Returns the new session_id (or -1 on error)."""
        try:
            messages_json = json.dumps(messages, ensure_ascii=False)
            with self._connect() as conn:
                cur = conn.execute(
                    "INSERT INTO sessions (summary, messages_json, duration_s, created_at)"
                    " VALUES (?, ?, ?, ?)",
                    (summary, messages_json, duration_s, time.time()),
                )
                conn.commit()
                return cur.lastrowid or -1
        except Exception as exc:
            logger.warning("EpisodicStore.save_session error: %s", exc)
            return -1

    def list_sessions(self, limit: int = 20) -> List[Dict[str, Any]]:
        """Return the most recent sessions as dicts.

        Each dict has keys: id, summary, created_at, duration_s.
        """
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT id, summary, created_at, duration_s"
                    " FROM sessions ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            return [dict(row) for row in rows]
        except Exception as exc:
            logger.warning("EpisodicStore.list_sessions error: %s", exc)
            return []

    def get_recent_context(self, n: int = 3) -> str:
        """Return the last *n* session summaries as a single text block.

        Suitable for injection into a system prompt.
        """
        sessions = self.list_sessions(limit=n)
        if not sessions:
            return ""
        parts: List[str] = []
        for i, s in enumerate(reversed(sessions), start=1):
            parts.append(f"[Session {i}] {s['summary']}")
        return "\n".join(parts)


__all__ = ["EpisodicStore"]
