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
from .grammar_catalog import coverage_matrix, passage_coverage, question_knowledge, sync_grammar_catalog, write_coverage_csv
from .enrichment import enrich_question_bank, search_knowledge
from .library import (
    convert_legacy_word,
    extract_library_text,
    hash_library,
    import_pdf_ocr_json,
    library_summary,
    propagate_duplicate_status,
    reconcile_question_bank,
    reuse_textbook_ocr,
    scan_library,
)
from .question_pipeline import pair_library_sources, structure_library, structure_summary
from .migrate_legacy import migrate_legacy
from .metrics import trend_report, weekly_report
from .quality import quality_markdown, run_quality_checks
from .selection import weighted_set_cover
from .util import read_json, write_json
from .webapp import configured_library_root, configured_question_bank, serve
from .weights import weight_policy_report, weighted_mastery_report


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
    data_dir = _data_dir(args)
    backup = _backup(data_dir, "pre-schema-migration") if database_path(data_dir).exists() else None
    result = initialize_database(data_dir, student_id=args.student, display_name=args.display_name)
    result["pre_migration_backup"] = backup
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


def cmd_knowledge_sync(args) -> int:
    data_dir, conn = _open(args)
    backup = _backup(data_dir, "grammar-catalog-sync")
    try:
        result = sync_grammar_catalog(conn, args.question_bank)
    finally:
        conn.close()
    result["backup"] = backup
    _emit(result, args.output)
    return 0


def cmd_knowledge_question(args) -> int:
    _, conn = _open(args)
    try:
        result = question_knowledge(conn, args.question, snapshot_id=args.snapshot)
    finally:
        conn.close()
    _emit(result, args.output)
    return 0


def cmd_knowledge_passage(args) -> int:
    _, conn = _open(args)
    try:
        result = passage_coverage(conn, args.passage, snapshot_id=args.snapshot)
    finally:
        conn.close()
    _emit(result, args.output)
    return 0


def cmd_knowledge_matrix(args) -> int:
    _, conn = _open(args)
    try:
        options = {
            "snapshot_id": args.snapshot,
            "minimum_confirmed_questions": args.minimum,
        }
        if args.knowledge:
            options["required_codes"] = args.knowledge
        result = coverage_matrix(conn, args.passages, **options)
    finally:
        conn.close()
    if args.csv:
        write_coverage_csv(result, args.csv)
        result["csv_output"] = str(Path(args.csv).resolve())
    _emit(result, args.output)
    return 0


def cmd_select_passages(args) -> int:
    _, conn = _open(args)
    try:
        result = weighted_set_cover(
            conn,
            args.knowledge,
            student_id=args.student,
            recent_error_days=args.days,
            max_passages=args.max_passages,
            as_of=args.as_of,
            snapshot_id=args.snapshot,
        )
    finally:
        conn.close()
    _emit(result, args.output)
    return 0


def cmd_weekly_report(args) -> int:
    _, conn = _open(args)
    try:
        result = weekly_report(conn, args.student, week_start=args.week_start, as_of=args.as_of)
    finally:
        conn.close()
    _emit(result, args.output)
    return 0


def cmd_trend_report(args) -> int:
    _, conn = _open(args)
    try:
        result = trend_report(conn, args.student, start=args.start, end=args.end)
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


def cmd_library_scan(args) -> int:
    _, conn = _open(args)
    try:
        result = scan_library(conn, args.root, library_key=args.library_key)
    finally:
        conn.close()
    _emit(result, args.output)
    return 0


def cmd_library_hash(args) -> int:
    _, conn = _open(args)
    try:
        result = hash_library(
            conn,
            library_key=args.library_key,
            include_audio=args.include_audio,
            limit=args.limit,
        )
    finally:
        conn.close()
    _emit(result, args.output)
    return 0


def cmd_library_extract(args) -> int:
    data_dir, conn = _open(args)
    cache_root = Path(args.cache_root) if args.cache_root else data_dir / "library_cache" / "extracted"
    try:
        result = extract_library_text(conn, cache_root, library_key=args.library_key, limit=args.limit)
    finally:
        conn.close()
    _emit(result, args.output)
    return 0


def cmd_library_summary(args) -> int:
    _, conn = _open(args)
    try:
        result = library_summary(conn, library_key=args.library_key)
    finally:
        conn.close()
    _emit(result, args.output)
    return 0


def cmd_library_reconcile(args) -> int:
    _, conn = _open(args)
    try:
        result = reconcile_question_bank(conn, args.question_bank, library_key=args.library_key)
    finally:
        conn.close()
    _emit(result, args.output)
    return 0


def cmd_library_convert_doc(args) -> int:
    data_dir, conn = _open(args)
    cache_root = Path(args.cache_root) if args.cache_root else data_dir / "library_cache"
    try:
        result = convert_legacy_word(conn, cache_root, library_key=args.library_key, limit=args.limit)
    finally:
        conn.close()
    _emit(result, args.output)
    return 0


def cmd_library_propagate_duplicates(args) -> int:
    _, conn = _open(args)
    try:
        result = propagate_duplicate_status(conn, library_key=args.library_key)
    finally:
        conn.close()
    _emit(result, args.output)
    return 0


def cmd_library_pair(args) -> int:
    _, conn = _open(args)
    try:
        result = pair_library_sources(conn, library_key=args.library_key)
    finally:
        conn.close()
    _emit(result, args.output)
    return 0


def cmd_library_structure(args) -> int:
    data_dir, conn = _open(args)
    backup = _backup(data_dir, "pre-library-structure")
    try:
        result = structure_library(conn, library_key=args.library_key, limit=args.limit)
    finally:
        conn.close()
    result["backup"] = backup
    _emit(result, args.output)
    return 0


def cmd_library_structure_summary(args) -> int:
    _, conn = _open(args)
    try:
        result = structure_summary(conn, library_key=args.library_key)
    finally:
        conn.close()
    _emit(result, args.output)
    return 0


def cmd_library_reuse_textbook_ocr(args) -> int:
    data_dir, conn = _open(args)
    backup = _backup(data_dir, "pre-textbook-ocr-reuse")
    cache_root = Path(args.cache_root) if args.cache_root else ensure_private_layout(data_dir)["library_cache"]
    try:
        result = reuse_textbook_ocr(
            conn,
            args.question_bank,
            cache_root,
            library_key=args.library_key,
        )
    finally:
        conn.close()
    result["backup"] = backup
    _emit(result, args.output)
    return 0


def cmd_library_import_ocr(args) -> int:
    data_dir, conn = _open(args)
    backup = _backup(data_dir, "pre-pdf-ocr-import")
    cache_root = Path(args.cache_root) if args.cache_root else ensure_private_layout(data_dir)["library_cache"]
    try:
        result = import_pdf_ocr_json(conn, args.resource, args.json_dir, cache_root)
    finally:
        conn.close()
    result["backup"] = backup
    _emit(result, args.output)
    return 0


def cmd_knowledge_enrich(args) -> int:
    data_dir, conn = _open(args)
    backup = _backup(data_dir, "question-enrichment")
    try:
        result = enrich_question_bank(conn, args.question_bank, limit=args.limit)
    finally:
        conn.close()
    result["backup"] = backup
    _emit(result, args.output)
    return 0


def cmd_knowledge_search(args) -> int:
    _, conn = _open(args)
    try:
        result = search_knowledge(conn, args.query, limit=args.limit)
    finally:
        conn.close()
    _emit(result, args.output)
    return 0


def cmd_weight_policy(args) -> int:
    _, conn = _open(args)
    try:
        result = weight_policy_report(conn)
    finally:
        conn.close()
    _emit(result, args.output)
    return 0


def cmd_weighted_mastery(args) -> int:
    _, conn = _open(args)
    try:
        result = weighted_mastery_report(conn, args.student)
    finally:
        conn.close()
    _emit(result, args.output)
    return 0


def cmd_serve(args) -> int:
    data_dir = _data_dir(args)
    serve(
        data_dir,
        question_bank=configured_question_bank(args.question_bank),
        library_root=configured_library_root(args.library_root),
        student_id=args.student,
        host=args.host,
        port=args.port,
        open_browser=args.open_browser,
    )
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

    knowledge = sub.add_parser("knowledge", help="Grammar knowledge catalog and coverage")
    knowledge_sub = knowledge.add_subparsers(dest="knowledge_command", required=True)
    knowledge_sync = knowledge_sub.add_parser("sync", help="Snapshot source_checked grammar metadata and mappings")
    knowledge_sync.add_argument("--question-bank", required=True)
    knowledge_sync.add_argument("--output")
    knowledge_sync.set_defaults(func=cmd_knowledge_sync)
    knowledge_question = knowledge_sub.add_parser("question", help="Query one question's knowledge mappings")
    knowledge_question.add_argument("--question", required=True)
    knowledge_question.add_argument("--snapshot")
    knowledge_question.add_argument("--output")
    knowledge_question.set_defaults(func=cmd_knowledge_question)
    knowledge_passage = knowledge_sub.add_parser("passage", help="Query one complete passage's coverage")
    knowledge_passage.add_argument("--passage", required=True)
    knowledge_passage.add_argument("--snapshot")
    knowledge_passage.add_argument("--output")
    knowledge_passage.set_defaults(func=cmd_knowledge_passage)
    knowledge_matrix = knowledge_sub.add_parser("matrix", help="Build a multi-passage coverage matrix")
    knowledge_matrix.add_argument("--passages", nargs="+", required=True)
    knowledge_matrix.add_argument("--knowledge", nargs="+")
    knowledge_matrix.add_argument("--minimum", type=int, default=2)
    knowledge_matrix.add_argument("--snapshot")
    knowledge_matrix.add_argument("--csv")
    knowledge_matrix.add_argument("--output")
    knowledge_matrix.set_defaults(func=cmd_knowledge_matrix)
    knowledge_enrich = knowledge_sub.add_parser("enrich", help="Build detailed rule-suggested knowledge and RAG metadata")
    knowledge_enrich.add_argument("--question-bank", required=True)
    knowledge_enrich.add_argument("--limit", type=int, default=0)
    knowledge_enrich.add_argument("--output")
    knowledge_enrich.set_defaults(func=cmd_knowledge_enrich)
    knowledge_search = knowledge_sub.add_parser("search", help="Search detailed knowledge and teaching-method documents")
    knowledge_search.add_argument("--query", required=True)
    knowledge_search.add_argument("--limit", type=int, default=30)
    knowledge_search.add_argument("--output")
    knowledge_search.set_defaults(func=cmd_knowledge_search)

    select = sub.add_parser("select", help="Automatic content selection")
    select_sub = select.add_subparsers(dest="select_command", required=True)
    select_passages = select_sub.add_parser("passages", help="Weighted set-cover over complete passages")
    select_passages.add_argument("--knowledge", nargs="+", required=True)
    select_passages.add_argument("--student")
    select_passages.add_argument("--days", type=int, default=30)
    select_passages.add_argument("--max-passages", type=int, default=5)
    select_passages.add_argument("--as-of")
    select_passages.add_argument("--snapshot")
    select_passages.add_argument("--output")
    select_passages.set_defaults(func=cmd_select_passages)

    report = sub.add_parser("report", help="Weekly metrics and separated trend series")
    report_sub = report.add_subparsers(dest="report_command", required=True)
    report_weekly = report_sub.add_parser("weekly")
    report_weekly.add_argument("--student", required=True)
    report_weekly.add_argument("--week-start")
    report_weekly.add_argument("--as-of")
    report_weekly.add_argument("--output")
    report_weekly.set_defaults(func=cmd_weekly_report)
    report_trends = report_sub.add_parser("trends")
    report_trends.add_argument("--student", required=True)
    report_trends.add_argument("--start", required=True)
    report_trends.add_argument("--end", required=True)
    report_trends.add_argument("--output")
    report_trends.set_defaults(func=cmd_trend_report)
    report_mastery = report_sub.add_parser("weighted-mastery", help="Offline-calibrated knowledge mastery")
    report_mastery.add_argument("--student", required=True)
    report_mastery.add_argument("--output")
    report_mastery.set_defaults(func=cmd_weighted_mastery)
    report_weights = report_sub.add_parser("weight-policy", help="Explain the current evidence-weight policy")
    report_weights.add_argument("--output")
    report_weights.set_defaults(func=cmd_weight_policy)

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

    library = sub.add_parser("library", help="Auditable source-library inventory and parsing")
    library_sub = library.add_subparsers(dest="library_command", required=True)
    library_scan = library_sub.add_parser("scan", help="Inventory source files without deleting or moving them")
    library_scan.add_argument("--root", required=True)
    library_scan.add_argument("--library-key", default="english_library")
    library_scan.add_argument("--output")
    library_scan.set_defaults(func=cmd_library_scan)
    library_hash = library_sub.add_parser("hash", help="Hash English files and mark exact duplicates")
    library_hash.add_argument("--library-key", default="english_library")
    library_hash.add_argument("--include-audio", action="store_true")
    library_hash.add_argument("--limit", type=int, default=0)
    library_hash.add_argument("--output")
    library_hash.set_defaults(func=cmd_library_hash)
    library_extract = library_sub.add_parser("extract", help="Extract text-layer content into the private cache")
    library_extract.add_argument("--library-key", default="english_library")
    library_extract.add_argument("--cache-root")
    library_extract.add_argument("--limit", type=int, default=0)
    library_extract.add_argument("--output")
    library_extract.set_defaults(func=cmd_library_extract)
    library_report = library_sub.add_parser("summary", help="Report audited full-library progress")
    library_report.add_argument("--library-key", default="english_library")
    library_report.add_argument("--output")
    library_report.set_defaults(func=cmd_library_summary)
    library_reconcile = library_sub.add_parser("reconcile", help="Mark source files already present in the question bank")
    library_reconcile.add_argument("--question-bank", required=True)
    library_reconcile.add_argument("--library-key", default="english_library")
    library_reconcile.add_argument("--output")
    library_reconcile.set_defaults(func=cmd_library_reconcile)
    library_convert = library_sub.add_parser("convert-doc", help="Convert legacy .doc files with local Microsoft Word")
    library_convert.add_argument("--library-key", default="english_library")
    library_convert.add_argument("--cache-root")
    library_convert.add_argument("--limit", type=int, default=100)
    library_convert.add_argument("--output")
    library_convert.set_defaults(func=cmd_library_convert_doc)
    library_duplicates = library_sub.add_parser("propagate-duplicates", help="Reuse canonical parse state for exact copies")
    library_duplicates.add_argument("--library-key", default="english_library")
    library_duplicates.add_argument("--output")
    library_duplicates.set_defaults(func=cmd_library_propagate_duplicates)
    library_pair = library_sub.add_parser("pair", help="Group prompt, answer, explanation, and audio files into logical sources")
    library_pair.add_argument("--library-key", default="english_library")
    library_pair.add_argument("--output")
    library_pair.set_defaults(func=cmd_library_pair)
    library_structure = library_sub.add_parser("structure", help="Build auditable RAG chunks and staged question/passages")
    library_structure.add_argument("--library-key", default="english_library")
    library_structure.add_argument("--limit", type=int, default=0)
    library_structure.add_argument("--output")
    library_structure.set_defaults(func=cmd_library_structure)
    library_structure_report = library_sub.add_parser("structure-summary", help="Report staged source, question, and review counts")
    library_structure_report.add_argument("--library-key", default="english_library")
    library_structure_report.add_argument("--output")
    library_structure_report.set_defaults(func=cmd_library_structure_summary)
    library_textbook = library_sub.add_parser("reuse-textbook-ocr", help="Reuse audited textbook OCR already present in the question bank")
    library_textbook.add_argument("--question-bank", required=True)
    library_textbook.add_argument("--library-key", default="english_library")
    library_textbook.add_argument("--cache-root")
    library_textbook.add_argument("--output")
    library_textbook.set_defaults(func=cmd_library_reuse_textbook_ocr)
    library_ocr_import = library_sub.add_parser("import-ocr", help="Import page OCR JSON for a scanned PDF")
    library_ocr_import.add_argument("--resource", required=True)
    library_ocr_import.add_argument("--json-dir", required=True)
    library_ocr_import.add_argument("--cache-root")
    library_ocr_import.add_argument("--output")
    library_ocr_import.set_defaults(func=cmd_library_import_ocr)

    web = sub.add_parser("serve", help="Start the local learning-management website")
    web.add_argument("--question-bank")
    web.add_argument("--library-root")
    web.add_argument("--student", default="STU-001")
    web.add_argument("--host", default="127.0.0.1")
    web.add_argument("--port", type=int, default=8788)
    web.add_argument("--open-browser", action="store_true")
    web.set_defaults(func=cmd_serve)

    info = sub.add_parser("info", help="Show database configuration and counts")
    info.set_defaults(func=cmd_db_info)
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(encoding="utf-8", errors="backslashreplace")
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (ConfigurationError, IngestConflict, ValueError, KeyError, RuntimeError, sqlite3.Error) as exc:  # type: ignore[name-defined]
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
