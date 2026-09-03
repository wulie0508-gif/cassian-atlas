from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import webbrowser
from contextlib import closing
from pathlib import Path
from typing import Any, Callable

from . import __version__
from .analytics import due_reviews, export_context, session_acceptance_report, weakness_report
from .backup import create_backup
from .base_projection import (
    claim_next_projection_record,
    projection_contract,
    projection_run_detail,
    record_projection_delivery_result,
    stage_projection_run,
    validate_projection_target_config,
)
from .db import (
    ConfigurationError,
    apply_migrations,
    connect,
    database_path,
    ensure_private_layout,
    migration_status,
    require_initialized,
    resolve_data_dir,
)
from .ingest import (
    IngestConflict,
    import_attempts,
    import_progress,
    import_session,
    replace_session_attempts,
    undo_ingest_event,
)
from .grammar_catalog import coverage_matrix, passage_coverage, question_knowledge, sync_grammar_catalog, write_coverage_csv
from .enrichment import enrich_question_bank, search_knowledge
from .extraction import (
    commit_extraction_batch,
    create_extraction_batch,
    extraction_batch_detail,
    extraction_review,
    submit_human_decisions,
    submit_provider_results,
)
from .generation import generation_detail, list_generations, start_generation, update_generation
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
from .orchestration import (
    agent_dashboard,
    append_run_event,
    capability_manifest,
    list_runs,
    plan_route,
    register_run,
    run_detail,
)
from .quality import quality_markdown, run_quality_checks
from .runtime import (
    CONFIG_KEYS,
    RuntimeConfig,
    apply_runtime_config,
    config_summary,
    default_config_path,
    effective_runtime_values,
    load_runtime_config,
    set_config_value,
)
from .selection import weighted_set_cover
from .selection_manifests import (
    cache_public_explanation,
    create_question_selection_manifest,
    invalidate_public_explanations,
    lookup_public_explanation,
    question_selection_manifest_detail,
)
from .server_control import server_status, start_server, stop_server
from .util import read_json, write_json
from .webapp import configured_library_root, configured_question_bank, serve
from .weights import weight_policy_report, weighted_mastery_report
from .workflows import record_assessment, record_dictation, record_reading_diagnostics
from .workspace import (
    create_student,
    deactivate_student,
    enroll_student,
    require_student_enrollment,
    student_detail,
    student_summaries,
    update_student,
)


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


def _runtime(args) -> RuntimeConfig:
    return getattr(args, "_runtime_config", RuntimeConfig(None, "none", {}, False))


def _schema_action_message(status: dict[str, Any]) -> str:
    mismatches = [row["version"] for row in status["checksum_mismatches"]]
    unknown = list(status["unknown_applied_versions"])
    missing = list(status["missing_checksums"])
    if mismatches:
        versions = ", ".join(mismatches)
        return (
            f"Database schema checksum mismatch for applied migration(s): {versions}. "
            "Restore the matching package or a verified database backup before continuing."
        )
    if unknown:
        versions = ", ".join(unknown)
        return (
            f"Database contains migration version(s) unavailable in this package: {versions}. "
            "Use the matching application package or restore a verified database backup."
        )
    if missing:
        versions = ", ".join(missing)
        return (
            f"Database migration metadata upgrade required for version(s): {versions}. "
            "Run `cassian upgrade` before this command."
        )
    pending = list(status["pending_versions"])
    versions = ", ".join(pending)
    return (
        f"Database upgrade required; pending migration(s): {versions}. "
        "Run `cassian upgrade` before this command."
    )


def cmd_config_show(args) -> int:
    _emit(
        config_summary(
            _runtime(args),
            explicit_data_dir=args.data_dir,
        )
    )
    return 0


def cmd_config_set(args) -> int:
    current = _runtime(args)
    path = current.path or default_config_path()
    updated = set_config_value(path, args.key, args.value)
    apply_runtime_config(updated)
    result = config_summary(updated, explicit_data_dir=args.data_dir)
    result["status"] = "saved"
    result["updated_key"] = args.key
    _emit(result)
    return 0


def cmd_init(args) -> int:
    data_dir = _data_dir(args)
    ensure_private_layout(data_dir)
    db_path = database_path(data_dir)
    existed = db_path.exists()
    pending: list[str] = []
    schema_state: dict[str, Any] | None = None
    if existed:
        with closing(connect(db_path, readonly=True)) as conn:
            schema_state = migration_status(conn)
            pending = list(schema_state["pending_versions"])
    backup = (
        _backup(data_dir, "pre-schema-migration")
        if schema_state is not None and schema_state["status"] != "ready"
        else None
    )
    with closing(connect(db_path)) as conn:
        migrations = apply_migrations(conn)
    _emit(
        {
            "status": "initialized" if not existed else ("upgraded" if migrations else "ready"),
            "database": str(db_path),
            "migrations_applied": migrations,
            "pending_before": pending,
            "pre_migration_backup": backup,
            "student_count_created": 0,
            "next": "cassian student add --student STU-<ID> --display-name <name>",
        }
    )
    return 0


def cmd_upgrade(args) -> int:
    data_dir = _data_dir(args)
    db_path = require_initialized(data_dir)
    with closing(connect(db_path, readonly=True)) as conn:
        schema_state = migration_status(conn)
    pending = list(schema_state["pending_versions"])
    if schema_state["status"] == "ready":
        _emit(
            {
                "status": "up_to_date",
                "database": str(db_path),
                "migrations_applied": [],
                "student_count_created": 0,
            }
        )
        return 0
    if schema_state["checksum_mismatches"] or schema_state["unknown_applied_versions"]:
        raise ConfigurationError(_schema_action_message(schema_state))
    backup = _backup(data_dir, "pre-schema-upgrade")
    with closing(connect(db_path)) as conn:
        migrations = apply_migrations(conn)
    _emit(
        {
            "status": "upgraded",
            "database": str(db_path),
            "migrations_applied": migrations,
            "pending_before": pending,
            "schema_status_before": schema_state["status"],
            "pre_migration_backup": backup,
            "student_count_created": 0,
        }
    )
    return 0


def cmd_backup(args) -> int:
    data_dir = _data_dir(args)
    path = _backup(data_dir, args.reason)
    _emit({"status": "created" if path else "skipped_empty_database", "backup": path})
    return 0


def _student_profile_from_args(args) -> dict[str, Any]:
    return {
        field: value
        for field in (
            "grade_level",
            "exam_system",
            "target_exam_date",
            "target_score",
            "weekly_hours",
            "course_stage",
            "teacher_notes",
        )
        if (value := getattr(args, field, None)) is not None
    }


def cmd_student_list(args) -> int:
    _, conn = _open(args)
    try:
        result = student_summaries(conn, include_inactive=args.include_inactive)
    finally:
        conn.close()
    _emit(result, args.output)
    return 0


def cmd_student_add(args) -> int:
    data_dir, conn = _open(args)
    backup = _backup(data_dir, "student-add")
    try:
        result = create_student(
            conn,
            {
                "student_id": args.student,
                "display_name": args.display_name,
                "timezone": args.timezone,
                "target_retention": args.target_retention,
                "subject_codes": args.subject,
                "profile": _student_profile_from_args(args),
            },
        )
    finally:
        conn.close()
    result["backup"] = backup
    _emit(result, args.output)
    return 0


def cmd_student_show(args) -> int:
    _, conn = _open(args)
    try:
        result = student_detail(conn, args.student, include_inactive=True)
    finally:
        conn.close()
    _emit(result, args.output)
    return 0


def cmd_student_update(args) -> int:
    payload: dict[str, Any] = _student_profile_from_args(args)
    for field in ("display_name", "timezone", "target_retention"):
        value = getattr(args, field, None)
        if value is not None:
            payload[field] = value
    data_dir, conn = _open(args)
    backup = _backup(data_dir, "student-update")
    try:
        result = update_student(conn, args.student, payload)
    finally:
        conn.close()
    result["backup"] = backup
    _emit(result, args.output)
    return 0


def cmd_student_enroll(args) -> int:
    data_dir, conn = _open(args)
    backup = _backup(data_dir, "student-enroll")
    try:
        result = enroll_student(conn, args.student, args.subject)
    finally:
        conn.close()
    result["backup"] = backup
    _emit(result, args.output)
    return 0


def cmd_student_deactivate(args) -> int:
    data_dir, conn = _open(args)
    backup = _backup(data_dir, "student-deactivate")
    try:
        result = deactivate_student(conn, args.student)
    finally:
        conn.close()
    result["backup"] = backup
    _emit(result, args.output)
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


def _explicit_student_payload(args) -> dict[str, Any]:
    payload = read_json(args.input)
    if not isinstance(payload, dict):
        raise ValueError("input JSON must be an object")
    explicit_student = str(args.student or "").strip().upper()
    payload_student = str(payload.get("student_id") or "").strip().upper()
    if payload_student and payload_student != explicit_student:
        raise ValueError(
            f"input student_id {payload_student} conflicts with explicit --student {explicit_student}"
        )
    payload["student_id"] = explicit_student
    return payload


def _json_object(path: str) -> dict[str, Any]:
    payload = read_json(path)
    if not isinstance(payload, dict):
        raise ValueError("input JSON must be an object")
    return payload


def _record_workflow(args, recorder: Callable, *, reason: str) -> int:
    payload = _explicit_student_payload(args)
    data_dir, conn = _open(args)
    backup = _backup(data_dir, reason)
    try:
        result = recorder(conn, payload, backup_path=backup)
    finally:
        conn.close()
    result["backup"] = backup
    _emit(result, args.output)
    return 0


def cmd_assessment_record(args) -> int:
    return _record_workflow(args, record_assessment, reason="assessment-record")


def cmd_dictation_record(args) -> int:
    return _record_workflow(args, record_dictation, reason="dictation-record")


def cmd_reading_diagnostics_record(args) -> int:
    return _record_workflow(
        args,
        record_reading_diagnostics,
        reason="reading-diagnostics-record",
    )


def cmd_generation_start(args) -> int:
    payload = _explicit_student_payload(args)
    data_dir, conn = _open(args)
    backup = _backup(data_dir, "generation-start")
    try:
        result = start_generation(conn, payload)
    finally:
        conn.close()
    result["backup"] = backup
    _emit(result, args.output)
    return 0


def cmd_generation_update(args) -> int:
    payload = _explicit_student_payload(args)
    data_dir, conn = _open(args)
    backup = _backup(data_dir, "generation-update")
    try:
        result = update_generation(
            conn,
            args.generation,
            payload,
            student_id=payload["student_id"],
        )
    finally:
        conn.close()
    result["backup"] = backup
    _emit(result, args.output)
    return 0


def cmd_generation_show(args) -> int:
    _, conn = _open(args)
    try:
        student = student_detail(conn, args.student, include_inactive=False)
        result = generation_detail(
            conn,
            args.generation,
            student_id=student["student_id"],
        )
    finally:
        conn.close()
    _emit(result, args.output)
    return 0


def cmd_generation_list(args) -> int:
    _, conn = _open(args)
    try:
        student = student_detail(conn, args.student, include_inactive=False)
        result = list_generations(
            conn,
            student_id=student["student_id"],
            limit=args.limit,
        )
    finally:
        conn.close()
    _emit(result, args.output)
    return 0


def cmd_extraction_create(args) -> int:
    payload = _explicit_student_payload(args)
    data_dir, conn = _open(args)
    backup = _backup(data_dir, "extraction-create")
    try:
        result = create_extraction_batch(conn, payload)
    finally:
        conn.close()
    result = dict(result)
    result["backup"] = backup
    _emit(result, args.output)
    return 0


def cmd_extraction_provider_submit(args) -> int:
    payload = _explicit_student_payload(args)
    data_dir, conn = _open(args)
    backup = _backup(data_dir, "extraction-provider-submit")
    try:
        result = submit_provider_results(
            conn,
            args.batch,
            payload,
            student_id=payload["student_id"],
        )
    finally:
        conn.close()
    result = dict(result)
    result["backup"] = backup
    _emit(result, args.output)
    return 0


def cmd_extraction_review(args) -> int:
    _, conn = _open(args)
    try:
        student = student_detail(conn, args.student, include_inactive=False)
        result = extraction_review(
            conn,
            args.batch,
            student_id=student["student_id"],
        )
    finally:
        conn.close()
    _emit(result, args.output)
    return 0


def cmd_extraction_decide(args) -> int:
    payload = _explicit_student_payload(args)
    data_dir, conn = _open(args)
    backup = _backup(data_dir, "extraction-decide")
    try:
        result = submit_human_decisions(
            conn,
            args.batch,
            payload,
            student_id=payload["student_id"],
        )
    finally:
        conn.close()
    result = dict(result)
    result["backup"] = backup
    _emit(result, args.output)
    return 0


def cmd_extraction_commit(args) -> int:
    payload = _explicit_student_payload(args)
    data_dir, conn = _open(args)
    backup = _backup(data_dir, "extraction-commit")
    try:
        result = commit_extraction_batch(
            conn,
            args.batch,
            payload,
            student_id=payload["student_id"],
            backup_path=backup,
        )
    finally:
        conn.close()
    result = dict(result)
    result["backup"] = backup
    _emit(result, args.output)
    return 0


def cmd_extraction_show(args) -> int:
    _, conn = _open(args)
    try:
        student = student_detail(conn, args.student, include_inactive=False)
        result = extraction_batch_detail(
            conn,
            args.batch,
            student_id=student["student_id"],
        )
    finally:
        conn.close()
    _emit(result, args.output)
    return 0


def cmd_projection_contract(args) -> int:
    _emit(projection_contract(), args.output)
    return 0


def cmd_projection_target_check(args) -> int:
    target_config = _json_object(args.input)
    result = validate_projection_target_config(
        target_config,
        student_id=args.student,
    )
    _emit(result, args.output)
    return 0


def cmd_projection_stage(args) -> int:
    payload = _explicit_student_payload(args)
    target_config = _json_object(args.target_config)
    data_dir, conn = _open(args)
    backup = _backup(data_dir, "projection-stage")
    try:
        result = stage_projection_run(
            conn,
            payload,
            target_config=target_config,
        )
    finally:
        conn.close()
    result = dict(result)
    result["backup"] = backup
    _emit(result, args.output)
    return 0


def cmd_projection_claim(args) -> int:
    data_dir, conn = _open(args)
    backup = _backup(data_dir, "projection-claim")
    try:
        claimed = claim_next_projection_record(
            conn,
            args.run,
            student_id=args.student,
        )
    finally:
        conn.close()
    if claimed is None:
        result: dict[str, Any] = {
            "status": "no_eligible_record",
            "projection_run_id": args.run,
            "student_id": str(args.student).strip().upper(),
        }
    else:
        result = {"status": "claimed", **claimed}
    result["backup"] = backup
    _emit(result, args.output)
    return 0


def cmd_projection_receipt(args) -> int:
    payload = _json_object(args.input)
    explicit_student = str(args.student or "").strip().upper()
    payload_student = str(payload.get("student_id") or "").strip().upper()
    if payload_student and payload_student != explicit_student:
        raise ValueError(
            f"input student_id {payload_student} conflicts with explicit --student {explicit_student}"
        )
    payload.pop("student_id", None)
    data_dir, conn = _open(args)
    backup = _backup(data_dir, "projection-receipt")
    try:
        result = record_projection_delivery_result(
            conn,
            payload,
            student_id=explicit_student,
        )
    finally:
        conn.close()
    result = dict(result)
    result["backup"] = backup
    _emit(result, args.output)
    return 0


def cmd_projection_show(args) -> int:
    _, conn = _open(args)
    try:
        result = projection_run_detail(
            conn,
            args.run,
            student_id=args.student,
        )
    finally:
        conn.close()
    _emit(result, args.output)
    return 0


def cmd_selection_create(args) -> int:
    payload = _explicit_student_payload(args)
    question_bank = configured_question_bank(args.question_bank)
    data_dir, conn = _open(args)
    backup = _backup(data_dir, "question-selection-create")
    try:
        result = create_question_selection_manifest(conn, question_bank, payload)
    finally:
        conn.close()
    result = dict(result)
    result["backup"] = backup
    _emit(result, args.output)
    return 0


def cmd_selection_show(args) -> int:
    _, conn = _open(args)
    try:
        student = student_detail(conn, args.student, include_inactive=False)
        result = question_selection_manifest_detail(
            conn,
            args.manifest,
            student_id=student["student_id"],
        )
    finally:
        conn.close()
    _emit(result, args.output)
    return 0


def cmd_explanation_cache(args) -> int:
    payload = _json_object(args.input)
    question_bank = configured_question_bank(args.question_bank)
    data_dir, conn = _open(args)
    backup = _backup(data_dir, "public-explanation-cache")
    try:
        result = cache_public_explanation(conn, question_bank, payload)
    finally:
        conn.close()
    result = dict(result)
    result["backup"] = backup
    _emit(result, args.output)
    return 0


def cmd_explanation_lookup(args) -> int:
    contract = _json_object(args.input) if args.input else None
    question_bank = configured_question_bank(args.question_bank)
    _, conn = _open(args)
    try:
        result = lookup_public_explanation(
            conn,
            question_bank,
            args.question,
            explanation_contract=contract,
        )
    finally:
        conn.close()
    _emit(result, args.output)
    return 0


def cmd_explanation_invalidate(args) -> int:
    contract = _json_object(args.input) if args.input else None
    question_bank = configured_question_bank(args.question_bank)
    data_dir, conn = _open(args)
    backup = _backup(data_dir, "public-explanation-invalidate")
    try:
        result = invalidate_public_explanations(
            conn,
            question_bank,
            args.question,
            explanation_contract=contract,
            reason=args.reason,
        )
    finally:
        conn.close()
    result = dict(result)
    result["backup"] = backup
    _emit(result, args.output)
    return 0


def cmd_session_import(args) -> int:
    args.command_name = "session-import"
    return _import_command(args, import_session)


def cmd_attempts_import(args) -> int:
    args.command_name = "attempts-import"
    return _import_command(args, import_attempts)


def cmd_attempts_replace(args) -> int:
    payload = _explicit_student_payload(args)
    payload_session = str(payload.get("session_id") or "").strip()
    if payload_session and payload_session != args.session:
        raise IngestConflict(
            f"input session_id {payload_session} conflicts with explicit --session {args.session}"
        )
    payload["session_id"] = args.session
    payload.setdefault("source_thread", "manual")
    data_dir, conn = _open(args)
    backup = _backup(data_dir, "attempts-replace")
    try:
        result = replace_session_attempts(
            conn,
            payload,
            actor=args.actor,
            reason=args.reason,
            backup_path=backup,
        )
    finally:
        conn.close()
    result["backup"] = backup
    _emit(result, args.output)
    return 0


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
    data_dir = _data_dir(args)
    db_path = database_path(data_dir)
    runtime = config_summary(_runtime(args), explicit_data_dir=args.data_dir)
    if not db_path.exists():
        _emit(
            {
                "status": "uninitialized",
                "app_version": __version__,
                "database": str(db_path),
                "pending_migrations": [],
                "migration_required": False,
                "runtime": runtime,
                "next": "cassian init",
            }
        )
        return 0
    conn = connect(db_path, readonly=True)
    try:
        table_names = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        migrations = (
            [dict(row) for row in conn.execute("SELECT * FROM schema_migrations ORDER BY version")]
            if "schema_migrations" in table_names
            else []
        )
        schema_state = migration_status(conn)
        pending = list(schema_state["pending_versions"])
        counts = {
            table: (
                conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
                if table in table_names
                else None
            )
            for table in (
                "students",
                "learning_sessions",
                "content_items",
                "attempts",
                "review_tasks",
                "ingest_events",
                "extraction_batches",
                "extraction_items",
                "extraction_confirmation_decisions",
                "base_projection_runs",
                "base_projection_outbox",
                "question_selection_manifests",
                "public_question_explanations",
            )
        }
        pragmas = {
            "foreign_keys": conn.execute("PRAGMA foreign_keys").fetchone()[0],
            "journal_mode": conn.execute("PRAGMA journal_mode").fetchone()[0],
            "busy_timeout": conn.execute("PRAGMA busy_timeout").fetchone()[0],
        }
    finally:
        conn.close()
    _emit(
        {
            "status": (
                "ready"
                if schema_state["status"] == "ready"
                else (
                    "upgrade_required"
                    if schema_state["status"] in {"pending", "checksum_uninitialized"}
                    else "schema_mismatch"
                )
            ),
            "app_version": __version__,
            "database": str(db_path),
            "migrations": migrations,
            "pending_migrations": pending,
            "migration_required": schema_state["status"] != "ready",
            "migration_status": schema_state,
            "upgrade_command": (
                "cassian upgrade"
                if schema_state["status"] in {"pending", "checksum_uninitialized"}
                else None
            ),
            "counts": counts,
            "pragmas": pragmas,
            "runtime": runtime,
        }
    )
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


def cmd_agent_capabilities(args) -> int:
    _emit(capability_manifest(), args.output)
    return 0


def cmd_agent_route(args) -> int:
    _, conn = _open(args)
    try:
        student_id, subject_code = require_student_enrollment(
            conn,
            args.student,
            args.subject,
        )
        if args.register:
            result = register_run(
                conn,
                {
                    "request_text": args.request,
                    "student_id": student_id,
                    "subject_code": subject_code,
                    "source_thread": args.source_thread,
                    "idempotency_key": args.idempotency_key,
                    "title": args.title,
                },
            )
        else:
            result = plan_route(
                args.request,
                student_id=student_id,
                subject_code=subject_code,
            )
    finally:
        conn.close()
    _emit(result, args.output)
    return 0


def cmd_agent_runs(args) -> int:
    _, conn = _open(args)
    try:
        student = student_detail(conn, args.student, include_inactive=False)
        result = list_runs(conn, student_id=student["student_id"], status=args.status, limit=args.limit)
    finally:
        conn.close()
    _emit(result, args.output)
    return 0


def cmd_agent_show(args) -> int:
    _, conn = _open(args)
    try:
        student = student_detail(conn, args.student, include_inactive=False)
        result = run_detail(conn, args.run, student_id=student["student_id"])
    finally:
        conn.close()
    _emit(result, args.output)
    return 0


def cmd_agent_dashboard(args) -> int:
    _, conn = _open(args)
    try:
        student = student_detail(conn, args.student, include_inactive=False)
        result = agent_dashboard(
            conn,
            student_id=student["student_id"],
            limit=args.limit,
        )
    finally:
        conn.close()
    _emit(result, args.output)
    return 0


def cmd_agent_event(args) -> int:
    _, conn = _open(args)
    try:
        result = append_run_event(
            conn,
            args.run,
            {
                "student_id": args.student,
                "event_type": args.event_type,
                "idempotency_key": args.idempotency_key,
                "capability_key": args.capability,
                "actor": args.actor,
                "message": args.message,
                "summary": args.summary,
                "result_ref": args.result_ref,
            },
        )
    finally:
        conn.close()
    _emit(result, args.output)
    return 0


def cmd_serve(args) -> int:
    data_dir = _data_dir(args)
    require_initialized(data_dir)
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


def _server_start(args) -> dict[str, Any]:
    data_dir = _data_dir(args)
    require_initialized(data_dir)
    question_bank = configured_question_bank(None)
    library_root = configured_library_root(None)
    if not question_bank.is_file():
        raise ValueError(f"Question bank not found: {question_bank}")
    if not library_root.is_dir():
        raise ValueError(f"Library root not found: {library_root}")
    runtime = _runtime(args)
    effective = effective_runtime_values(runtime, explicit_data_dir=args.data_dir)
    return start_server(
        data_dir,
        config_path=runtime.path,
        project_root=effective.get("project_root"),
        student_id=args.student,
        host=args.host,
        port=args.port,
        timeout=args.timeout,
    )


def cmd_server_status(args) -> int:
    _emit(server_status(_data_dir(args), host=args.host, port=args.port))
    return 0


def cmd_server_start(args) -> int:
    result = _server_start(args)
    if args.open_browser:
        webbrowser.open(result["url"])
    _emit(result)
    return 0


def cmd_server_stop(args) -> int:
    _emit(
        stop_server(
            _data_dir(args),
            host=args.host,
            port=args.port,
            timeout=args.timeout,
            force=args.force,
        )
    )
    return 0


def cmd_server_restart(args) -> int:
    data_dir = _data_dir(args)
    stopped = stop_server(
        data_dir,
        host=args.host,
        port=args.port,
        timeout=args.timeout,
        force=args.force,
    )
    started = _server_start(args)
    if args.open_browser:
        webbrowser.open(started["url"])
    _emit({"status": "running", "stop": stopped, "start": started})
    return 0


def _add_profile_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--grade-level")
    parser.add_argument("--exam-system")
    parser.add_argument("--target-exam-date", help="ISO date, for example 2027-01-08")
    parser.add_argument("--target-score", type=float)
    parser.add_argument("--weekly-hours", type=float)
    parser.add_argument("--course-stage")
    parser.add_argument("--teacher-notes")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cassian",
        description="Cassian Atlas control plane for local, auditable learning records",
    )
    parser.add_argument(
        "--config",
        help="Runtime JSON config; otherwise use OPEN_TUTOR_CONFIG or ~/.opentutor/config.json",
    )
    parser.add_argument("--data-dir", help="Private runtime data directory; may also use ENGLISH_TRACKER_DATA_DIR")
    sub = parser.add_subparsers(dest="command", required=True)

    config = sub.add_parser("config", help="Inspect or update the private runtime configuration")
    config_sub = config.add_subparsers(dest="config_command", required=True)
    config_show = config_sub.add_parser("show", help="Show config discovery and effective runtime values")
    config_show.set_defaults(func=cmd_config_show, allow_pending=True)
    config_set = config_sub.add_parser("set", help="Persist one runtime setting")
    config_set.add_argument("key", choices=CONFIG_KEYS)
    config_set.add_argument("value")
    config_set.set_defaults(func=cmd_config_set, allow_pending=True)

    init = sub.add_parser("init", help="Create private folders and schema without adding a learner")
    init.set_defaults(func=cmd_init, allow_pending=True)

    upgrade = sub.add_parser("upgrade", help="Back up and apply pending schema migrations only")
    upgrade.set_defaults(func=cmd_upgrade, allow_pending=True)

    backup = sub.add_parser("backup", help="Create an integrity-checked SQLite online backup")
    backup.add_argument("--reason", default="manual")
    backup.set_defaults(func=cmd_backup, allow_pending=True)

    student = sub.add_parser("student", help="Explicit multi-learner workspace operations")
    student_sub = student.add_subparsers(dest="student_command", required=True)
    student_list = student_sub.add_parser("list", help="List learner workspaces")
    student_list.add_argument("--include-inactive", action="store_true")
    student_list.add_argument("--output")
    student_list.set_defaults(func=cmd_student_list)
    student_add = student_sub.add_parser("add", help="Add one real learner with an explicit ID")
    student_add.add_argument("--student", required=True, help="Stable explicit ID such as STU-002")
    student_add.add_argument("--display-name", required=True)
    student_add.add_argument("--timezone", default="Asia/Shanghai")
    student_add.add_argument("--target-retention", type=float, default=0.90)
    student_add.add_argument("--subject", action="append", help="Repeat to enroll multiple subjects; defaults to english")
    _add_profile_arguments(student_add)
    student_add.add_argument("--output")
    student_add.set_defaults(func=cmd_student_add)
    student_show = student_sub.add_parser("show", help="Show one learner and enrollment state")
    student_show.add_argument("--student", required=True)
    student_show.add_argument("--output")
    student_show.set_defaults(func=cmd_student_show)
    student_update = student_sub.add_parser("update", help="Update identity or learning profile fields")
    student_update.add_argument("--student", required=True)
    student_update.add_argument("--display-name")
    student_update.add_argument("--timezone")
    student_update.add_argument("--target-retention", type=float)
    _add_profile_arguments(student_update)
    student_update.add_argument("--output")
    student_update.set_defaults(func=cmd_student_update)
    student_enroll = student_sub.add_parser("enroll", help="Enroll an active learner in one or more subjects")
    student_enroll.add_argument("--student", required=True)
    student_enroll.add_argument("--subject", action="append", required=True)
    student_enroll.add_argument("--output")
    student_enroll.set_defaults(func=cmd_student_enroll)
    student_deactivate = student_sub.add_parser("deactivate", help="Deactivate a learner without deleting evidence")
    student_deactivate.add_argument("--student", required=True)
    student_deactivate.add_argument("--output")
    student_deactivate.set_defaults(func=cmd_student_deactivate)

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
    attempts_replace = attempts_sub.add_parser(
        "replace",
        help="Atomically replace selected active attempts and recalculate the existing assessment",
    )
    attempts_replace.add_argument("--student", required=True)
    attempts_replace.add_argument("--session", required=True)
    attempts_replace.add_argument("--input", required=True)
    attempts_replace.add_argument("--actor", default="teacher")
    attempts_replace.add_argument("--reason", default="verified missing-answer correction")
    attempts_replace.add_argument("--output")
    attempts_replace.set_defaults(func=cmd_attempts_replace)

    progress = sub.add_parser("progress", help="Session progress operations")
    progress_sub = progress.add_subparsers(dest="progress_command", required=True)
    progress_import = progress_sub.add_parser("import")
    progress_import.add_argument("--input", required=True)
    progress_import.set_defaults(func=cmd_progress_import)

    assessment = sub.add_parser("assessment", help="Record deterministic scored assessments")
    assessment_sub = assessment.add_subparsers(dest="assessment_command", required=True)
    assessment_record = assessment_sub.add_parser("record", help="Record one assessment from JSON")
    assessment_record.add_argument("--student", required=True)
    assessment_record.add_argument("--input", required=True)
    assessment_record.add_argument("--output")
    assessment_record.set_defaults(func=cmd_assessment_record)

    dictation = sub.add_parser("dictation", help="Record and grade deterministic dictation evidence")
    dictation_sub = dictation.add_subparsers(dest="dictation_command", required=True)
    dictation_record = dictation_sub.add_parser("record", help="Record one dictation from JSON")
    dictation_record.add_argument("--student", required=True)
    dictation_record.add_argument("--input", required=True)
    dictation_record.add_argument("--output")
    dictation_record.set_defaults(func=cmd_dictation_record)

    reading = sub.add_parser("reading", help="Reading evidence operations")
    reading_sub = reading.add_subparsers(dest="reading_command", required=True)
    reading_diagnostics = reading_sub.add_parser("diagnostics", help="Reading error diagnostics")
    reading_diagnostics_sub = reading_diagnostics.add_subparsers(
        dest="reading_diagnostics_command",
        required=True,
    )
    reading_diagnostics_record = reading_diagnostics_sub.add_parser(
        "record",
        help="Record attempt-level reading diagnostics from JSON",
    )
    reading_diagnostics_record.add_argument("--student", required=True)
    reading_diagnostics_record.add_argument("--input", required=True)
    reading_diagnostics_record.add_argument("--output")
    reading_diagnostics_record.set_defaults(func=cmd_reading_diagnostics_record)

    generation = sub.add_parser("generation", help="Track source-bound Codex artifact generation")
    generation_sub = generation.add_subparsers(dest="generation_command", required=True)
    generation_start = generation_sub.add_parser("start", help="Start an idempotent generation from JSON")
    generation_start.add_argument("--student", required=True)
    generation_start.add_argument("--input", required=True)
    generation_start.add_argument("--output")
    generation_start.set_defaults(func=cmd_generation_start)
    generation_update = generation_sub.add_parser("update", help="Update generation status or output from JSON")
    generation_update.add_argument("--student", required=True)
    generation_update.add_argument("--generation", required=True)
    generation_update.add_argument("--input", required=True)
    generation_update.add_argument("--output")
    generation_update.set_defaults(func=cmd_generation_update)
    generation_show = generation_sub.add_parser("show", help="Show one learner-owned generation")
    generation_show.add_argument("--student", required=True)
    generation_show.add_argument("--generation", required=True)
    generation_show.add_argument("--output")
    generation_show.set_defaults(func=cmd_generation_show)
    generation_list = generation_sub.add_parser("list", help="List one learner's generations")
    generation_list.add_argument("--student", required=True)
    generation_list.add_argument("--limit", type=int, default=30)
    generation_list.add_argument("--output")
    generation_list.set_defaults(func=cmd_generation_list)

    extraction = sub.add_parser(
        "extraction",
        help="Stage model transcription candidates for full-batch human confirmation",
    )
    extraction_sub = extraction.add_subparsers(
        dest="extraction_command",
        required=True,
    )
    extraction_create = extraction_sub.add_parser(
        "create",
        help="Create an idempotent learner-owned extraction batch from JSON",
    )
    extraction_create.add_argument("--student", required=True)
    extraction_create.add_argument("--input", required=True)
    extraction_create.add_argument("--output")
    extraction_create.set_defaults(func=cmd_extraction_create)
    extraction_provider = extraction_sub.add_parser(
        "provider-submit",
        help="Append one provider's immutable extraction candidates",
    )
    extraction_provider.add_argument("--student", required=True)
    extraction_provider.add_argument("--batch", required=True)
    extraction_provider.add_argument("--input", required=True)
    extraction_provider.add_argument("--output")
    extraction_provider.set_defaults(func=cmd_extraction_provider_submit)
    extraction_review_parser = extraction_sub.add_parser(
        "review",
        help="Show the complete compact human-confirmation list",
    )
    extraction_review_parser.add_argument("--student", required=True)
    extraction_review_parser.add_argument("--batch", required=True)
    extraction_review_parser.add_argument("--output")
    extraction_review_parser.set_defaults(func=cmd_extraction_review)
    extraction_decide = extraction_sub.add_parser(
        "decide",
        help="Submit structured teacher decisions for one review version",
    )
    extraction_decide.add_argument("--student", required=True)
    extraction_decide.add_argument("--batch", required=True)
    extraction_decide.add_argument("--input", required=True)
    extraction_decide.add_argument("--output")
    extraction_decide.set_defaults(func=cmd_extraction_decide)
    extraction_commit = extraction_sub.add_parser(
        "commit",
        help="Commit a fully confirmed batch to formal learning evidence",
    )
    extraction_commit.add_argument("--student", required=True)
    extraction_commit.add_argument("--batch", required=True)
    extraction_commit.add_argument("--input", required=True)
    extraction_commit.add_argument("--output")
    extraction_commit.set_defaults(func=cmd_extraction_commit)
    extraction_show = extraction_sub.add_parser(
        "show",
        help="Re-read one learner-owned extraction batch",
    )
    extraction_show.add_argument("--student", required=True)
    extraction_show.add_argument("--batch", required=True)
    extraction_show.add_argument("--output")
    extraction_show.set_defaults(func=cmd_extraction_show)

    projection = sub.add_parser(
        "projection",
        help="Manage the local privacy-safe Feishu Base projection outbox",
    )
    projection_sub = projection.add_subparsers(
        dest="projection_command",
        required=True,
    )
    projection_contract_parser = projection_sub.add_parser(
        "contract",
        help="Show the strict local projection field contract",
    )
    projection_contract_parser.add_argument("--output")
    projection_contract_parser.set_defaults(
        func=cmd_projection_contract,
        allow_pending=True,
    )
    projection_target_check = projection_sub.add_parser(
        "target-check",
        help="Validate and redact one local student target configuration",
    )
    projection_target_check.add_argument("--student", required=True)
    projection_target_check.add_argument("--input", required=True)
    projection_target_check.add_argument("--output")
    projection_target_check.set_defaults(
        func=cmd_projection_target_check,
        allow_pending=True,
    )
    projection_stage = projection_sub.add_parser(
        "stage",
        help="Stage a deterministic local projection run without transport",
    )
    projection_stage.add_argument("--student", required=True)
    projection_stage.add_argument("--input", required=True)
    projection_stage.add_argument("--target-config", required=True)
    projection_stage.add_argument("--output")
    projection_stage.set_defaults(func=cmd_projection_stage)
    projection_claim = projection_sub.add_parser(
        "claim",
        help="Claim the next learner-owned local outbox record",
    )
    projection_claim.add_argument("--student", required=True)
    projection_claim.add_argument("--run", required=True)
    projection_claim.add_argument("--output")
    projection_claim.set_defaults(func=cmd_projection_claim)
    projection_receipt = projection_sub.add_parser(
        "receipt",
        help="Record a learner-owned sanitized delivery receipt",
    )
    projection_receipt.add_argument("--student", required=True)
    projection_receipt.add_argument("--input", required=True)
    projection_receipt.add_argument("--output")
    projection_receipt.set_defaults(func=cmd_projection_receipt)
    projection_show = projection_sub.add_parser(
        "show",
        help="Show one learner-owned local projection run",
    )
    projection_show.add_argument("--student", required=True)
    projection_show.add_argument("--run", required=True)
    projection_show.add_argument("--output")
    projection_show.set_defaults(func=cmd_projection_show)

    selection_manifest = sub.add_parser(
        "selection",
        help="Create and inspect verified question-selection manifests",
    )
    selection_manifest_sub = selection_manifest.add_subparsers(
        dest="selection_command",
        required=True,
    )
    selection_create = selection_manifest_sub.add_parser(
        "create",
        help="Select verified real questions and persist an immutable learner manifest",
    )
    selection_create.add_argument("--student", required=True)
    selection_create.add_argument("--input", required=True)
    selection_create.add_argument("--question-bank")
    selection_create.add_argument("--output")
    selection_create.set_defaults(func=cmd_selection_create)
    selection_show = selection_manifest_sub.add_parser(
        "show",
        help="Re-read one learner-owned question-selection manifest",
    )
    selection_show.add_argument("--student", required=True)
    selection_show.add_argument("--manifest", required=True)
    selection_show.add_argument("--output")
    selection_show.set_defaults(func=cmd_selection_show)

    explanation = sub.add_parser(
        "explanation",
        help="Manage reusable public question explanations without student diagnosis",
    )
    explanation_sub = explanation.add_subparsers(
        dest="explanation_command",
        required=True,
    )
    explanation_cache = explanation_sub.add_parser(
        "cache",
        help="Cache a source-checked or teacher-confirmed public explanation",
    )
    explanation_cache.add_argument("--input", required=True)
    explanation_cache.add_argument("--question-bank")
    explanation_cache.add_argument("--output")
    explanation_cache.set_defaults(func=cmd_explanation_cache)
    explanation_lookup = explanation_sub.add_parser(
        "lookup",
        help="Look up a deterministic public explanation cache identity",
    )
    explanation_lookup.add_argument("--question", required=True)
    explanation_lookup.add_argument(
        "--input",
        help="Optional JSON file containing the explanation contract object",
    )
    explanation_lookup.add_argument("--question-bank")
    explanation_lookup.add_argument("--output")
    explanation_lookup.set_defaults(func=cmd_explanation_lookup)
    explanation_invalidate = explanation_sub.add_parser(
        "invalidate",
        help="Mark public explanations stale when source or version identity changes",
    )
    explanation_invalidate.add_argument("--question", required=True)
    explanation_invalidate.add_argument(
        "--input",
        help="Optional JSON file containing the explanation contract object",
    )
    explanation_invalidate.add_argument(
        "--reason",
        default="deterministic_cache_identity_changed",
    )
    explanation_invalidate.add_argument("--question-bank")
    explanation_invalidate.add_argument("--output")
    explanation_invalidate.set_defaults(func=cmd_explanation_invalidate)

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

    agent = sub.add_parser("agent", help="Deterministic specialist routing and dashboard run ledger")
    agent_sub = agent.add_subparsers(dest="agent_command", required=True)
    agent_capabilities = agent_sub.add_parser("capabilities", help="List specialist skills and stable endpoints")
    agent_capabilities.add_argument("--output")
    agent_capabilities.set_defaults(func=cmd_agent_capabilities)
    agent_route = agent_sub.add_parser("route", help="Plan the smallest specialist chain for one request")
    agent_route.add_argument("--request", required=True)
    agent_route.add_argument("--student", required=True)
    agent_route.add_argument("--subject", default="english")
    agent_route.add_argument("--source-thread", default="orchestrator")
    agent_route.add_argument("--idempotency-key")
    agent_route.add_argument("--title")
    agent_route.add_argument("--register", action="store_true", help="Create a dashboard run after routing")
    agent_route.add_argument("--output")
    agent_route.set_defaults(func=cmd_agent_route)
    agent_runs = agent_sub.add_parser("runs", help="List recent specialist runs")
    agent_runs.add_argument("--student", required=True)
    agent_runs.add_argument("--status", choices=["planned", "in_progress", "needs_input", "completed", "failed", "cancelled"])
    agent_runs.add_argument("--limit", type=int, default=30)
    agent_runs.add_argument("--output")
    agent_runs.set_defaults(func=cmd_agent_runs)
    agent_show = agent_sub.add_parser("show", help="Show one learner-owned specialist run")
    agent_show.add_argument("--student", required=True)
    agent_show.add_argument("--run", required=True)
    agent_show.add_argument("--output")
    agent_show.set_defaults(func=cmd_agent_show)
    agent_dashboard_parser = agent_sub.add_parser("dashboard", help="Show the learner-scoped Agent run dashboard")
    agent_dashboard_parser.add_argument("--student", required=True)
    agent_dashboard_parser.add_argument("--limit", type=int, default=12)
    agent_dashboard_parser.add_argument("--output")
    agent_dashboard_parser.set_defaults(func=cmd_agent_dashboard)
    agent_event = agent_sub.add_parser("event", help="Append progress to a routed task")
    agent_event.add_argument("--student", required=True)
    agent_event.add_argument("--run", required=True)
    agent_event.add_argument("--event-type", required=True, choices=["started", "progress", "needs_input", "completed", "failed", "cancelled"])
    agent_event.add_argument("--idempotency-key", required=True)
    agent_event.add_argument("--capability", required=True)
    agent_event.add_argument("--actor", default="specialist")
    agent_event.add_argument("--message", required=True)
    agent_event.add_argument("--summary")
    agent_event.add_argument("--result-ref")
    agent_event.add_argument("--output")
    agent_event.set_defaults(func=cmd_agent_event)

    server = sub.add_parser("server", help="Manage the local read-only dashboard process")
    server_sub = server.add_subparsers(dest="server_command", required=True)
    server_status_parser = server_sub.add_parser("status", help="Verify PID, version, schema, and database")
    server_status_parser.add_argument("--host", default="127.0.0.1")
    server_status_parser.add_argument("--port", type=int, default=8788)
    server_status_parser.set_defaults(func=cmd_server_status, allow_pending=True)
    server_start_parser = server_sub.add_parser("start", help="Start a verified hidden background server")
    server_start_parser.add_argument("--student", help="Optional explicit initial learner; no implicit default")
    server_start_parser.add_argument("--host", default="127.0.0.1")
    server_start_parser.add_argument("--port", type=int, default=8788)
    server_start_parser.add_argument("--timeout", type=float, default=60)
    server_start_parser.add_argument("--open-browser", action="store_true")
    server_start_parser.set_defaults(func=cmd_server_start)
    server_stop_parser = server_sub.add_parser("stop", help="Stop only the process verified for this runtime")
    server_stop_parser.add_argument("--host", default="127.0.0.1")
    server_stop_parser.add_argument("--port", type=int, default=8788)
    server_stop_parser.add_argument("--timeout", type=float, default=15)
    server_stop_parser.add_argument("--force", action="store_true", help="Allow stopping a managed but unhealthy PID")
    server_stop_parser.set_defaults(func=cmd_server_stop, allow_pending=True)
    server_restart_parser = server_sub.add_parser("restart", help="Stop the verified server and start the current version")
    server_restart_parser.add_argument("--student", help="Optional explicit initial learner; no implicit default")
    server_restart_parser.add_argument("--host", default="127.0.0.1")
    server_restart_parser.add_argument("--port", type=int, default=8788)
    server_restart_parser.add_argument("--timeout", type=float, default=60)
    server_restart_parser.add_argument("--force", action="store_true", help="Allow stopping a managed but unhealthy PID")
    server_restart_parser.add_argument("--open-browser", action="store_true")
    server_restart_parser.set_defaults(func=cmd_server_restart)

    web = sub.add_parser("serve", help="Run the local dashboard in the foreground")
    web.add_argument("--question-bank")
    web.add_argument("--library-root")
    web.add_argument("--student", help="Optional explicit initial learner; no implicit default")
    web.add_argument("--host", default="127.0.0.1")
    web.add_argument("--port", type=int, default=8788)
    web.add_argument("--open-browser", action="store_true")
    web.set_defaults(func=cmd_serve)

    info = sub.add_parser("info", help="Show database configuration and counts")
    info.set_defaults(func=cmd_db_info, allow_pending=True)
    return parser


def _guard_current_schema(args) -> None:
    if getattr(args, "allow_pending", False):
        return
    data_dir = _data_dir(args)
    db_path = database_path(data_dir)
    if not db_path.exists():
        return
    with closing(connect(db_path, readonly=True)) as conn:
        schema_state = migration_status(conn)
    if schema_state["status"] != "ready":
        raise ConfigurationError(_schema_action_message(schema_state))


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(encoding="utf-8", errors="backslashreplace")
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        allow_missing_config = args.command == "config" and args.config_command == "set"
        runtime = load_runtime_config(args.config, allow_missing=allow_missing_config)
        apply_runtime_config(runtime)
        args._runtime_config = runtime
        _guard_current_schema(args)
        return int(args.func(args))
    except (ConfigurationError, IngestConflict, ValueError, KeyError, RuntimeError, sqlite3.Error) as exc:  # type: ignore[name-defined]
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
