from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Callable

from .analytics import due_reviews, export_context, session_acceptance_report, weakness_report
from .backup import create_backup
from .db import (
    ConfigurationError,
    connect,
    database_path,
    ensure_private_layout,
    initialize_database,
    require_initialized,
    resolve_data_dir,
)
from .ingest import IngestConflict, import_attempts, import_progress, import_session, undo_ingest_event
from .migrate_legacy import migrate_legacy
from .quality import quality_markdown, run_quality_checks
from .util import read_json, write_json


def _emit(value: Any, output: str | None = None) -> None:
    if output:
        write_json(output, value)
    print(json.dumps(value, ensure_ascii=False, indent=2))


def _data_dir(args) -> Path:
    return resolve_data_dir(args.data_dir)


def _open(args):
    data_dir = _data_dir(args)
    return data_dir, connect(require_initialized(data_dir))


def _backup(data_dir: Path, reason: str) -> str | None:
    paths = ensure_private_layout(data_dir)
    result = create_backup(database_path(data_dir), paths["backups"], reason)
    return str(result) if result else None


def cmd_init(args) -> int:
    result = initialize_database(_data_dir(args), student_id=args.student, display_name=args.display_name)
    _emit(result)
    return 0


def cmd_backup(args) -> int:
    data_dir = _data_dir(args)
    path = _backup(data_dir, args.reason)
    _emit({"status": "created" if path else "skipped_empty_database", "backup": path})
    return 0


def _import_command(args, importer: Callable) -> int:
    payload = read_json(args.input)
    data_dir, conn = _open(args)
    backup = _backup(data_dir, args.command_name)
    try:
        result = importer(conn, payload, backup_path=backup)
    finally:
        conn.close()
    result["backup"] = backup
    _emit(result)
    return 0


def cmd_session_import(args) -> int:
    args.command_name = "session-import"
    return _import_command(args, import_session)


def cmd_attempts_import(args) -> int:
    args.command_name = "attempts-import"
    return _import_command(args, import_attempts)


def cmd_progress_import(args) -> int:
    args.command_name = "progress-import"
    return _import_command(args, import_progress)


def cmd_weaknesses(args) -> int:
    _, conn = _open(args)
    try:
        result = weakness_report(conn, args.student, as_of=args.as_of, days=args.days)
    finally:
        conn.close()
    _emit(result, args.output)
    return 0


def cmd_review_due(args) -> int:
    _, conn = _open(args)
    try:
        result = due_reviews(conn, args.student, as_of=args.as_of, domain=args.domain, limit=args.limit)
    finally:
        conn.close()
    _emit(result, args.output)
    return 0


def cmd_context(args) -> int:
    _, conn = _open(args)
    try:
        result = export_context(conn, args.student, args.audience, as_of=args.as_of)
    finally:
        conn.close()
    _emit(result, args.output)
    return 0


def cmd_session_report(args) -> int:
    _, conn = _open(args)
    try:
        result = session_acceptance_report(conn, args.session)
    finally:
        conn.close()
    _emit(result, args.output)
    return 0


def cmd_migrate_legacy(args) -> int:
    data_dir, conn = _open(args)
    backup = _backup(data_dir, "legacy-migration")
    try:
        result = migrate_legacy(
            conn,
            legacy_db=args.legacy_db,
            mastery_json=args.mastery_json,
            victor_db=args.victor_db,
            student_id=args.student,
            backup_path=backup,
        )
    finally:
        conn.close()
    result["backup"] = backup
    output = args.output or str(data_dir / "exports" / "legacy_migration_report.json")
    _emit(result, output)
    return 0


def cmd_ingest_undo(args) -> int:
    data_dir, conn = _open(args)
    backup = _backup(data_dir, "ingest-undo")
    try:
        result = undo_ingest_event(conn, args.event, actor=args.actor, reason=args.reason)
    finally:
        conn.close()
    result["backup"] = backup
    _emit(result)
    return 0


def cmd_ingest_correct(args) -> int:
    payload = read_json(args.input)
    if payload.get("event_id") == args.event:
        raise IngestConflict("The corrected payload must use a new event_id and idempotency_key")
    importer = {"session": import_session, "attempts": import_attempts, "progress": import_progress}[args.kind]
    data_dir, conn = _open(args)
    backup = _backup(data_dir, "ingest-correct")
    try:
        replacement = importer(conn, payload, backup_path=backup)
        if replacement["status"] not in {"applied", "duplicate"}:
            raise IngestConflict("Corrected payload was not applied; original event remains active")
        reverted = undo_ingest_event(conn, args.event, actor=args.actor, reason=args.reason)
    finally:
        conn.close()
    _emit({"status": "corrected", "replacement": replacement, "reverted": reverted, "backup": backup})
    return 0


def cmd_quality(args) -> int:
    data_dir, conn = _open(args)
    try:
        result = run_quality_checks(conn)
    finally:
        conn.close()
    output = args.output or str(data_dir / "exports" / "data_quality_report.json")
    write_json(output, result)
    markdown_path = args.markdown or str(data_dir / "exports" / "DATA_QUALITY_REPORT.md")
    migration_report = None
    migration_path = Path(args.migration_report) if args.migration_report else data_dir / "exports" / "legacy_migration_report.json"
    if migration_path.exists():
        migration_report = read_json(migration_path)
    Path(markdown_path).parent.mkdir(parents=True, exist_ok=True)
    Path(markdown_path).write_text(quality_markdown(result, migration_report), encoding="utf-8", newline="\n")
    _emit({"report": result, "json_output": output, "markdown_output": markdown_path})
    return 0 if result["trust_status"] == "ready" else 2


def cmd_db_info(args) -> int:
    data_dir, conn = _open(args)
    try:
        migrations = [dict(row) for row in conn.execute("SELECT * FROM schema_migrations ORDER BY version")]
        counts = {
            table: conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            for table in ("students", "learning_sessions", "content_items", "attempts", "review_tasks", "ingest_events")
        }
        pragmas = {
            "foreign_keys": conn.execute("PRAGMA foreign_keys").fetchone()[0],
            "journal_mode": conn.execute("PRAGMA journal_mode").fetchone()[0],
            "busy_timeout": conn.execute("PRAGMA busy_timeout").fetchone()[0],
        }
    finally:
        conn.close()
    _emit({"database": str(database_path(data_dir)), "migrations": migrations, "counts": counts, "pragmas": pragmas})
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="english-tracker", description="Local, auditable English learning records")
    parser.add_argument("--data-dir", help="Private runtime data directory; may also use ENGLISH_TRACKER_DATA_DIR")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="Create private folders and apply database migrations")
    init.add_argument("--student", default="STU-001")
    init.add_argument("--display-name", help="Private display name stored only in the local database")
    init.set_defaults(func=cmd_init)

    backup = sub.add_parser("backup", help="Create an integrity-checked SQLite online backup")
    backup.add_argument("--reason", default="manual")
    backup.set_defaults(func=cmd_backup)

    session = sub.add_parser("session", help="Learning session operations")
    session_sub = session.add_subparsers(dest="session_command", required=True)
    session_import = session_sub.add_parser("import")
    session_import.add_argument("--input", required=True)
    session_import.set_defaults(func=cmd_session_import)
    session_report = session_sub.add_parser("report")
    session_report.add_argument("--session", required=True)
    session_report.add_argument("--output")
    session_report.set_defaults(func=cmd_session_report)

    attempts = sub.add_parser("attempts", help="Attempt operations")
    attempts_sub = attempts.add_subparsers(dest="attempts_command", required=True)
    attempts_import = attempts_sub.add_parser("import")
    attempts_import.add_argument("--input", required=True)
    attempts_import.set_defaults(func=cmd_attempts_import)

    progress = sub.add_parser("progress", help="Session progress operations")
    progress_sub = progress.add_subparsers(dest="progress_command", required=True)
    progress_import = progress_sub.add_parser("import")
    progress_import.add_argument("--input", required=True)
    progress_import.set_defaults(func=cmd_progress_import)

    weaknesses = sub.add_parser("weaknesses", help="Evidence-backed weakness reports")
    weaknesses_sub = weaknesses.add_subparsers(dest="weakness_command", required=True)
    weakness_report_parser = weaknesses_sub.add_parser("report")
    weakness_report_parser.add_argument("--student", required=True)
    weakness_report_parser.add_argument("--days", type=int, default=30)
    weakness_report_parser.add_argument("--as-of")
    weakness_report_parser.add_argument("--output")
    weakness_report_parser.set_defaults(func=cmd_weaknesses)

    review = sub.add_parser("review", help="Review queue operations")
    review_sub = review.add_subparsers(dest="review_command", required=True)
    review_due = review_sub.add_parser("due")
    review_due.add_argument("--student", required=True)
    review_due.add_argument("--as-of")
    review_due.add_argument("--domain")
    review_due.add_argument("--limit", type=int, default=100)
    review_due.add_argument("--output")
    review_due.set_defaults(func=cmd_review_due)

    context = sub.add_parser("context", help="Cross-thread context exports")
    context_sub = context.add_subparsers(dest="context_command", required=True)
    context_export = context_sub.add_parser("export")
    context_export.add_argument("--student", required=True)
    context_export.add_argument("--for", dest="audience", choices=["courseware", "dictation"], required=True)
    context_export.add_argument("--as-of")
    context_export.add_argument("--output")
    context_export.set_defaults(func=cmd_context)

    migrate = sub.add_parser("migrate", help="Read-only legacy migrations")
    migrate_sub = migrate.add_subparsers(dest="migrate_command", required=True)
    migrate_legacy_parser = migrate_sub.add_parser("legacy")
    migrate_legacy_parser.add_argument("--legacy-db", required=True)
    migrate_legacy_parser.add_argument("--mastery-json", required=True)
    migrate_legacy_parser.add_argument("--victor-db", required=True)
    migrate_legacy_parser.add_argument("--student", required=True)
    migrate_legacy_parser.add_argument("--output")
    migrate_legacy_parser.set_defaults(func=cmd_migrate_legacy)

    ingest = sub.add_parser("ingest", help="Audited import correction operations")
    ingest_sub = ingest.add_subparsers(dest="ingest_command", required=True)
    undo = ingest_sub.add_parser("undo")
    undo.add_argument("--event", required=True)
    undo.add_argument("--actor", default="engineering")
    undo.add_argument("--reason", default="operator correction")
    undo.set_defaults(func=cmd_ingest_undo)
    correct = ingest_sub.add_parser("correct")
    correct.add_argument("--event", required=True, help="Original ingest event to revert after replacement succeeds")
    correct.add_argument("--kind", choices=["session", "attempts", "progress"], required=True)
    correct.add_argument("--input", required=True, help="Corrected payload with a new event_id/idempotency_key")
    correct.add_argument("--actor", default="engineering")
    correct.add_argument("--reason", default="replacement import")
    correct.set_defaults(func=cmd_ingest_correct)

    data = sub.add_parser("data", help="Data quality operations")
    data_sub = data.add_subparsers(dest="data_command", required=True)
    check = data_sub.add_parser("check")
    check.add_argument("--output")
    check.add_argument("--markdown")
    check.add_argument("--migration-report")
    check.set_defaults(func=cmd_quality)

    info = sub.add_parser("info", help="Show database configuration and counts")
    info.set_defaults(func=cmd_db_info)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (ConfigurationError, IngestConflict, ValueError, KeyError, sqlite3.Error) as exc:  # type: ignore[name-defined]
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
