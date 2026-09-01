"""Parity: legacy assistant SQLite data folds into Ultron's database."""

import sqlite3
from pathlib import Path

from ultron.migrations import fold_bujji_database, fold_if_present


def make_legacy_db(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE memories (id INTEGER PRIMARY KEY, content TEXT NOT NULL);
        CREATE TABLE events (id INTEGER PRIMARY KEY, kind TEXT NOT NULL);
        CREATE TABLE wake_phrases (phrase TEXT NOT NULL);
        INSERT INTO memories VALUES (1, 'operator prefers terse answers');
        INSERT INTO events VALUES (1, 'voice.turn');
        INSERT INTO wake_phrases VALUES ('hey bujji');
        """
    )
    connection.commit()
    connection.close()


def test_folding_prefixes_colliding_tables_and_keeps_unique_names(tmp_path: Path):
    target = tmp_path / "ultron.db"
    connection = sqlite3.connect(target)
    # Ultron's schema already has memories/events; the legacy names collide.
    connection.execute("CREATE TABLE memories (id TEXT PRIMARY KEY)")
    connection.execute("CREATE TABLE events (run_id TEXT)")
    connection.commit()
    connection.close()

    legacy = tmp_path / "legacy-bujji.db"
    make_legacy_db(legacy)

    folded = fold_bujji_database(target, legacy)
    assert folded == {
        "memories": "bujji_memories",
        "events": "bujji_events",
        "wake_phrases": "wake_phrases",
    }

    check = sqlite3.connect(target)
    rows = check.execute('SELECT id, content FROM "bujji_memories"').fetchall()
    assert rows == [(1, "operator prefers terse answers")]
    kinds = check.execute('SELECT kind FROM "bujji_events"').fetchall()
    assert kinds == [("voice.turn",)]
    phrases = check.execute("SELECT phrase FROM wake_phrases").fetchall()
    assert phrases == [("hey bujji",)]
    ultron_rows = check.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    assert ultron_rows == 0
    check.close()


def test_fold_if_present_is_idempotent_and_gated(tmp_path: Path):
    target = tmp_path / "ultron.db"
    sqlite3.connect(target).close()
    legacy = tmp_path / "legacy-bujji.db"
    make_legacy_db(legacy)

    first = fold_if_present(target, legacy)
    assert first is not None and len(first) == 3
    marker = Path(str(legacy) + ".folded")
    assert marker.exists()

    second = fold_if_present(target, legacy)
    assert second is None

    assert fold_if_present(target, tmp_path / "missing.db") is None
    assert fold_if_present(target, None) is None
