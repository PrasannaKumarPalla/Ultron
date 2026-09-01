"""One-time folds of legacy standalone databases into Ultron's SQLite schema.

Tables are copied verbatim unless the name already exists in Ultron's schema,
in which case they land with a ``bujji_`` prefix so nothing is lost or clobbered.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def fold_bujji_database(db_path: Path, bujji_db_path: Path) -> dict[str, str]:
    """Fold a standalone assistant SQLite database into the Ultron database.

    Returns a mapping of original table name -> folded table name.
    """
    db_path = Path(db_path)
    bujji_db_path = Path(bujji_db_path)
    if not bujji_db_path.exists():
        raise FileNotFoundError(str(bujji_db_path))
    connection = sqlite3.connect(db_path, timeout=30)
    try:
        connection.execute("ATTACH DATABASE ? AS bujji_src", (str(bujji_db_path),))
        incoming = [
            row[0] for row in connection.execute(
                "SELECT name FROM bujji_src.sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        existing = {
            row[0] for row in connection.execute(
                "SELECT name FROM main.sqlite_master WHERE type='table'"
            )
        }
        folded: dict[str, str] = {}
        for table in incoming:
            destination = table if table not in existing else f"bujji_{table}"
            columns = [
                # PRAGMA args cannot be bound; `table` comes from sqlite_master
                # and is passed through `_quote()`.
                row[1] for row in connection.execute(  # nosemgrep: python.lang.security.audit.formatted-sql-query.formatted-sql-query,python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query
                    f"PRAGMA bujji_src.table_info({_quote(table)})"
                )
            ]
            column_list = ", ".join(_quote(column) for column in columns)
            connection.execute(
                f"CREATE TABLE main.{_quote(destination)} AS "
                f"SELECT {column_list} FROM bujji_src.{_quote(table)}"
            )
            folded[table] = destination
        connection.commit()
        connection.execute("DETACH DATABASE bujji_src")
        return folded
    finally:
        connection.close()


def fold_if_present(db_path: Path, bujji_db_path: Path | None) -> dict[str, str] | None:
    """Fold the legacy database when configured and still unfolded. Idempotent."""
    if bujji_db_path is None or not Path(bujji_db_path).exists():
        return None
    marker = Path(str(bujji_db_path) + ".folded")
    if marker.exists():
        return None
    folded = fold_bujji_database(db_path, bujji_db_path)
    marker.write_text("", encoding="utf-8")
    return folded
