from __future__ import annotations

import os
import sqlite3
from hashlib import sha256
from importlib import resources
from pathlib import Path
from typing import Any

from .util import utc_now


DATA_DIR_ENV = "ENGLISH_TRACKER_DATA_DIR"
DB_NAME_ENV = "ENGLISH_TRACKER_DB_NAME"


class ConfigurationError(RuntimeError):
    pass


class MigrationStateError(RuntimeError):
    """Raised when packaged migrations cannot be safely reconciled with a database."""


class MigrationChecksumMismatch(MigrationStateError):
    """Raised when an applied migration no longer matches its packaged SQL."""


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


def _migration_manifest() -> list[dict[str, str]]:
    manifest: list[dict[str, str]] = []
    seen: set[str] = set()
    for resource in _migration_files():
        version = resource.name.split("_", 1)[0]
        if version in seen:
            raise MigrationStateError(f"Duplicate packaged migration version: {version}")
        seen.add(version)
        sql = resource.read_text(encoding="utf-8")
        manifest.append(
            {
                "version": version,
                "filename": resource.name,
                "checksum_sha256": sha256(sql.encode("utf-8")).hexdigest(),
                "sql": sql,
            }
        )
    return manifest


def _schema_migrations_exists(conn: sqlite3.Connection) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
        ).fetchone()
    )


def _schema_migrations_has_checksum(conn: sqlite3.Connection) -> bool:
    if not _schema_migrations_exists(conn):
        return False
    return any(row[1] == "checksum_sha256" for row in conn.execute("PRAGMA table_info(schema_migrations)"))


def migration_status(conn: sqlite3.Connection) -> dict[str, Any]:
    """Return a read-only migration state with explicit pending and mismatch evidence."""
    manifest = _migration_manifest()
    packaged = {row["version"]: row for row in manifest}
    table_exists = _schema_migrations_exists(conn)
    checksum_available = _schema_migrations_has_checksum(conn)
    if not table_exists:
        applied: dict[str, str | None] = {}
    elif checksum_available:
        applied = {
            str(row["version"]): row["checksum_sha256"]
            for row in conn.execute(
                "SELECT version, checksum_sha256 FROM schema_migrations ORDER BY version"
            )
        }
    else:
        applied = {
            str(row["version"]): None
            for row in conn.execute("SELECT version FROM schema_migrations ORDER BY version")
        }

    pending = [row["version"] for row in manifest if row["version"] not in applied]
    missing_checksums = [
        version for version, checksum in applied.items()
        if version in packaged and not checksum
    ]
    mismatches = [
        {
            "version": version,
            "expected_sha256": packaged[version]["checksum_sha256"],
            "actual_sha256": checksum,
        }
        for version, checksum in applied.items()
        if version in packaged and checksum and checksum != packaged[version]["checksum_sha256"]
    ]
    unknown_applied = sorted(version for version in applied if version not in packaged)
    if mismatches or unknown_applied or (checksum_available and missing_checksums):
        state = "mismatch"
    elif pending:
        state = "pending"
    elif missing_checksums:
        state = "checksum_uninitialized"
    else:
        state = "ready"
    return {
        "status": state,
        "pending_versions": pending,
        "checksum_mismatches": mismatches,
        "missing_checksums": missing_checksums,
        "unknown_applied_versions": unknown_applied,
        "checksum_storage": "available" if checksum_available else "uninitialized",
    }


def pending_migration_versions(conn: sqlite3.Connection) -> list[str]:
    """Return packaged migration versions that have not been applied yet.

    This check is intentionally read-only.  Callers can decide whether a
    backup is needed before applying the returned migrations.
    """
    return list(migration_status(conn)["pending_versions"])


def _prepare_schema_migrations(conn: sqlite3.Connection) -> None:
    table_existed = _schema_migrations_exists(conn)
    checksum_existed = _schema_migrations_has_checksum(conn)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL,
            checksum_sha256 TEXT NOT NULL
        )
        """
    )
    legacy_without_checksum = table_existed and not checksum_existed
    if legacy_without_checksum:
        conn.execute("ALTER TABLE schema_migrations ADD COLUMN checksum_sha256 TEXT")
        packaged = {row["version"]: row["checksum_sha256"] for row in _migration_manifest()}
        for row in conn.execute(
            "SELECT version FROM schema_migrations WHERE checksum_sha256 IS NULL OR checksum_sha256=''"
        ):
            checksum = packaged.get(str(row["version"]))
            if checksum:
                conn.execute(
                    "UPDATE schema_migrations SET checksum_sha256=? WHERE version=?",
                    (checksum, row["version"]),
                )
    conn.commit()


def apply_migrations(conn: sqlite3.Connection) -> list[str]:
    _prepare_schema_migrations(conn)
    status = migration_status(conn)
    if status["checksum_mismatches"] or status["missing_checksums"]:
        versions = ", ".join(
            [row["version"] for row in status["checksum_mismatches"]]
            + list(status["missing_checksums"])
        )
        raise MigrationChecksumMismatch(
            f"Applied migration checksum mismatch for version(s): {versions}. "
            "Restore the matching package or a verified database backup before continuing."
        )
    if status["unknown_applied_versions"]:
        versions = ", ".join(status["unknown_applied_versions"])
        raise MigrationStateError(
            f"Database contains migration version(s) unavailable in this package: {versions}"
        )
    pending = set(status["pending_versions"])
    newly_applied: list[str] = []
    for migration in _migration_manifest():
        version = migration["version"]
        if version not in pending:
            continue
        sql = migration["sql"]
        version_escaped = version.replace("'", "''")
        now_escaped = utc_now().replace("'", "''")
        checksum_escaped = migration["checksum_sha256"].replace("'", "''")
        script = (
            "BEGIN IMMEDIATE;\n"
            + sql
            + "\nINSERT INTO schema_migrations(version, applied_at, checksum_sha256) "
            + f"VALUES ('{version_escaped}', '{now_escaped}', '{checksum_escaped}');\n"
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
    student_id: str,
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
        conn.execute(
            """
            INSERT INTO student_subjects(student_id,subject_code,active,enrolled_at)
            VALUES (?,'english',1,?)
            ON CONFLICT(student_id,subject_code) DO UPDATE SET active=1
            """,
            (student_id, now),
        )
    conn.close()
    return {"database": str(db_path), "migrations_applied": migrations, "student_id": student_id}


def require_initialized(data_dir: Path) -> Path:
    path = database_path(data_dir)
    if not path.exists():
        raise ConfigurationError(f"Database not found: {path}. Run the init command first.")
    return path
