from __future__ import annotations

import os
import sqlite3
from importlib import resources
from pathlib import Path

from .util import utc_now


DATA_DIR_ENV = "ENGLISH_TRACKER_DATA_DIR"
DB_NAME_ENV = "ENGLISH_TRACKER_DB_NAME"


class ConfigurationError(RuntimeError):
    pass


def resolve_data_dir(explicit: str | Path | None = None) -> Path:
    raw = str(explicit) if explicit else os.environ.get(DATA_DIR_ENV)
    if not raw:
        raise ConfigurationError(
            f"Set {DATA_DIR_ENV} or pass --data-dir. The repository never embeds a private path."
        )
    return Path(raw).expanduser().resolve()


def ensure_private_layout(data_dir: Path) -> dict[str, Path]:
    paths = {
        "root": data_dir,
        "db": data_dir / "db",
        "dictation_inbox": data_dir / "inbox" / "dictation",
        "courseware_inbox": data_dir / "inbox" / "courseware",
        "manual_inbox": data_dir / "inbox" / "manual",
        "backups": data_dir / "backups",
        "exports": data_dir / "exports",
        "logs": data_dir / "logs",
        "library_cache": data_dir / "library_cache",
    }
    for path in paths.values():
        path.mkdir(parents=True, exist_ok=True)
    return paths


def database_path(data_dir: Path) -> Path:
    filename = os.environ.get(DB_NAME_ENV, "learning.sqlite")
    if Path(filename).name != filename or not filename.lower().endswith((".sqlite", ".sqlite3", ".db")):
        raise ConfigurationError(f"{DB_NAME_ENV} must be a plain SQLite filename")
    return data_dir / "db" / filename


def connect(db_path: str | Path, *, readonly: bool = False) -> sqlite3.Connection:
    path = Path(db_path).resolve()
    if readonly:
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=10)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    if not readonly:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def _migration_files() -> list:
    root = resources.files("english_tracker.migrations")
    return sorted((item for item in root.iterdir() if item.name.endswith(".sql")), key=lambda x: x.name)


def apply_migrations(conn: sqlite3.Connection) -> list[str]:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    applied = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
    newly_applied: list[str] = []
    for resource in _migration_files():
        version = resource.name.split("_", 1)[0]
        if version in applied:
            continue
        sql = resource.read_text(encoding="utf-8")
        version_escaped = version.replace("'", "''")
        now_escaped = utc_now().replace("'", "''")
        script = (
            "BEGIN IMMEDIATE;\n"
            + sql
            + f"\nINSERT INTO schema_migrations(version, applied_at) VALUES ('{version_escaped}', '{now_escaped}');\n"
            + "COMMIT;"
        )
        try:
            conn.executescript(script)
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        newly_applied.append(version)
    return newly_applied


def initialize_database(
    data_dir: Path,
    *,
    student_id: str = "STU-001",
    display_name: str | None = None,
) -> dict[str, object]:
    ensure_private_layout(data_dir)
    db_path = database_path(data_dir)
    conn = connect(db_path)
    migrations = apply_migrations(conn)
    now = utc_now()
    with conn:
        conn.execute(
            """
            INSERT INTO students(student_id, display_name, created_at, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(student_id) DO UPDATE SET
              display_name = CASE
                WHEN excluded.display_name IS NULL THEN students.display_name
                ELSE excluded.display_name
              END,
              updated_at = excluded.updated_at
            """,
            (student_id, display_name, now, now),
        )
    conn.close()
    return {"database": str(db_path), "migrations_applied": migrations, "student_id": student_id}


def require_initialized(data_dir: Path) -> Path:
    path = database_path(data_dir)
    if not path.exists():
        raise ConfigurationError(f"Database not found: {path}. Run the init command first.")
    return path
