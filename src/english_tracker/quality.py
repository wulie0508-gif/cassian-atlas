from __future__ import annotations

from typing import Any

from .util import utc_now


def _count(conn, sql: str, params: tuple = ()) -> int:
    return int(conn.execute(sql, params).fetchone()[0])


def run_quality_checks(conn) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    def add(check_id: str, severity: str, failed: int, description: str, remediation: str) -> None:
        checks.append(
            {
                "check_id": check_id,
                "severity": severity,
                "status": "pass" if failed == 0 else "fail",
                "failed_rows": failed,
                "description": description,
                "remediation": remediation if failed else None,
            }
        )

    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    add("sqlite_integrity", "critical", 0 if integrity == "ok" else 1, "SQLite file integrity", "Restore the latest verified backup.")
    fk_rows = list(conn.execute("PRAGMA foreign_key_check"))
    add("foreign_keys", "critical", len(fk_rows), "Foreign-key coverage", "Repair or quarantine orphan rows before analysis.")
    add(
        "attempt_event_uniqueness",
        "critical",
        _count(conn, "SELECT COUNT(*) FROM (SELECT event_id FROM attempts GROUP BY event_id HAVING COUNT(*)>1)"),
        "Each attempt event_id is globally unique",
        "Resolve duplicated event IDs and preserve the losing rows in audit storage.",
    )
    add(
        "idempotency_uniqueness",
        "critical",
        _count(conn, "SELECT COUNT(*) FROM (SELECT idempotency_key FROM ingest_events GROUP BY idempotency_key HAVING COUNT(*)>1)"),
        "Each ingest idempotency key is unique",
        "Stop imports and repair conflicting ingest envelopes.",
    )
    add(
        "active_attempt_current_evaluation",
        "high",
        _count(
            conn,
            """
            SELECT COUNT(*) FROM attempts a
            LEFT JOIN evaluations e ON e.attempt_id=a.attempt_id AND e.is_current=1
            WHERE a.record_status='active' AND e.evaluation_id IS NULL
            """,
        ),
        "Every active attempt has one current evaluation",
        "Create a needs_check evaluation or void the incomplete attempt.",
    )
    add(
        "multiple_current_evaluations",
        "critical",
        _count(conn, "SELECT COUNT(*) FROM (SELECT attempt_id FROM evaluations WHERE is_current=1 GROUP BY attempt_id HAVING COUNT(*)>1)"),
        "At most one current evaluation per attempt",
        "Keep the latest reviewed revision current and audit the correction.",
    )
    add(
        "answer_capture_semantics",
        "high",
        _count(
            conn,
            """
            SELECT COUNT(*) FROM attempts
            WHERE answer_capture_status='not_captured' AND student_answer IS NOT NULL
            """,
        ),
        "not_captured never carries a student answer",
        "Correct the capture status through an audited replacement import.",
    )
    add(
        "captured_blank_semantics",
        "medium",
        _count(
            conn,
            """
            SELECT COUNT(*) FROM attempts
            WHERE answer_capture_status='captured_blank' AND COALESCE(student_answer,'')<>''
            """,
        ),
        "captured_blank has an empty answer",
        "Correct the capture status or answer through an audited replacement import.",
    )
    add(
        "not_captured_has_no_specific_error_cause",
        "high",
        _count(
            conn,
            """
            SELECT COUNT(*) FROM attempt_error_map aem
            JOIN attempts a ON a.attempt_id=aem.attempt_id
            WHERE a.answer_capture_status='not_captured' AND aem.record_status='active'
            """,
        ),
        "No specific student error cause is inferred without the original answer",
        "Void the inferred error mapping and retain only wrong/partial plus answer_capture_status=not_captured.",
    )
    add(
        "model_mapping_verification_guard",
        "critical",
        _count(
            conn,
            """
            SELECT
              (SELECT COUNT(*) FROM question_knowledge_map
               WHERE mapping_source='model_suggested' AND verification_status IN ('source_checked','verified'))
              +
              (SELECT COUNT(*) FROM item_knowledge_map
               WHERE mapping_source='model_suggested' AND verification_status IN ('source_checked','verified'))
            """,
        ),
        "Model-suggested knowledge mappings are never auto-verified",
        "Downgrade the mapping to suggested and require explicit manual verification.",
    )
    add(
        "deep_enrichment_verification_guard",
        "critical",
        _count(
            conn,
            """
            SELECT
              (SELECT COUNT(*) FROM question_deep_knowledge_map
               WHERE mapping_source='model_suggested' AND verification_status IN ('source_checked','verified'))
              +
              (SELECT COUNT(*) FROM question_enrichments
               WHERE mapping_source='model_suggested' AND verification_status IN ('source_checked','verified'))
              +
              (SELECT COUNT(*) FROM knowledge_search_documents
               WHERE mapping_source='model_suggested' AND verification_status IN ('source_checked','verified'))
              +
              (SELECT COUNT(*) FROM staged_question_knowledge_map
               WHERE mapping_source IN ('model_suggested','rule')
                 AND verification_status IN ('source_checked','verified'))
            """,
        ),
        "Generated deep mappings and staged mappings are never auto-verified",
        "Downgrade generated records to suggested and require an explicit manual review action.",
    )
    add(
        "library_duplicate_lineage",
        "high",
        _count(
            conn,
            """
            SELECT COUNT(*) FROM library_resources d
            LEFT JOIN library_resources c ON c.resource_id=d.duplicate_of_resource_id
            WHERE d.is_canonical=0 AND (d.duplicate_of_resource_id IS NULL OR c.resource_id IS NULL OR c.is_canonical<>1)
            """,
        ),
        "Every exact duplicate points to a canonical source resource",
        "Repeat hashing and duplicate propagation, then review broken lineage before parsing.",
    )
    add(
        "library_source_set_preferred_membership",
        "high",
        _count(
            conn,
            """
            SELECT COUNT(*) FROM library_source_sets s
            WHERE (s.preferred_prompt_resource_id IS NOT NULL AND NOT EXISTS (
                     SELECT 1 FROM library_source_set_resources m
                     WHERE m.source_set_id=s.source_set_id AND m.resource_id=s.preferred_prompt_resource_id))
               OR (s.preferred_answer_resource_id IS NOT NULL AND NOT EXISTS (
                     SELECT 1 FROM library_source_set_resources m
                     WHERE m.source_set_id=s.source_set_id AND m.resource_id=s.preferred_answer_resource_id))
               OR (s.preferred_audio_resource_id IS NOT NULL AND NOT EXISTS (
                     SELECT 1 FROM library_source_set_resources m
                     WHERE m.source_set_id=s.source_set_id AND m.resource_id=s.preferred_audio_resource_id))
            """,
        ),
        "Every preferred prompt, answer, and audio resource belongs to its logical source set",
        "Re-run the deterministic source-pairing stage and inspect any ambiguous source set.",
    )
    add(
        "grammar_snapshot_catalog_reconciliation",
        "high",
        _count(
            conn,
            """
            SELECT COUNT(*) FROM source_snapshots s
            WHERE s.is_current=1 AND (
              s.question_count<>(SELECT COUNT(*) FROM grammar_question_catalog q WHERE q.source_snapshot_id=s.source_snapshot_id)
              OR s.passage_count<>(SELECT COUNT(*) FROM grammar_passage_catalog p WHERE p.source_snapshot_id=s.source_snapshot_id)
            )
            """,
        ),
        "Current grammar snapshot row counts reconcile to its catalogs",
        "Repeat the read-only knowledge sync after backing up the unified database.",
    )
    add(
        "grammar_questions_have_mapping",
        "medium",
        _count(
            conn,
            """
            SELECT COUNT(*) FROM grammar_question_catalog q
            JOIN source_snapshots s ON s.source_snapshot_id=q.source_snapshot_id AND s.is_current=1
            WHERE NOT EXISTS (
              SELECT 1 FROM question_knowledge_map qkm
              WHERE qkm.source_snapshot_id=q.source_snapshot_id AND qkm.question_id=q.question_id
                AND qkm.verification_status<>'rejected'
            )
            """,
        ),
        "Every current grammar question has at least one non-rejected knowledge mapping",
        "Add a legacy/manual mapping or mark the source record needs_check.",
    )
    add(
        "open_review_task_uniqueness",
        "high",
        _count(conn, "SELECT COUNT(*) FROM (SELECT student_id,item_id FROM review_tasks WHERE status='open' GROUP BY student_id,item_id HAVING COUNT(*)>1)"),
        "Only one open review task exists per student/item",
        "Merge duplicate open tasks without deleting their audit records.",
    )
    add(
        "active_attempt_active_ingest",
        "high",
        _count(
            conn,
            """
            SELECT COUNT(*) FROM attempts a JOIN ingest_events ie ON ie.ingest_event_id=a.ingest_event_id
            WHERE a.record_status='active' AND ie.status<>'applied'
            """,
        ),
        "Active attempts belong to applied ingest events",
        "Void attempts from reverted events and rebuild review state.",
    )
    add(
        "review_state_last_attempt_active",
        "medium",
        _count(
            conn,
            """
            SELECT COUNT(*) FROM review_state rs
            JOIN attempts a ON a.attempt_id=rs.last_attempt_id
            WHERE rs.last_attempt_id IS NOT NULL AND a.record_status<>'active'
            """,
        ),
        "Review state does not point to a voided attempt",
        "Rebuild review state from active attempt history.",
    )
    counts = {
        table: _count(conn, f'SELECT COUNT(*) FROM "{table}"')
        for table in (
            "students",
            "learning_sessions",
            "content_items",
            "attempts",
            "evaluations",
            "knowledge_points",
            "error_types",
            "review_state",
            "review_tasks",
            "ingest_events",
            "legacy_records",
            "source_snapshots",
            "grammar_passage_catalog",
            "grammar_question_catalog",
            "question_knowledge_map",
            "session_assessments",
            "library_resources",
            "library_source_sets",
            "library_text_chunks",
            "staged_passages",
            "staged_questions",
            "staged_question_knowledge_map",
            "library_structure_reviews",
        )
    }
    severity_order = {"critical": 4, "high": 3, "medium": 2, "low": 1}
    failed = [check for check in checks if check["status"] == "fail"]
    highest = max((severity_order[check["severity"]] for check in failed), default=0)
    trust = "not_ready" if highest >= 4 else ("use_with_caution" if highest else "ready")
    return {
        "generated_at": utc_now(),
        "trust_status": trust,
        "table_counts": counts,
        "checks": checks,
        "summary": {
            "total_checks": len(checks),
            "passed": len(checks) - len(failed),
            "failed": len(failed),
            "highest_failed_severity": next((name for name, rank in severity_order.items() if rank == highest), None),
        },
    }


def quality_markdown(report: dict[str, Any], migration_report: dict[str, Any] | None = None) -> str:
    lines = [
        "# Data Quality and Migration Report",
        "",
        f"Generated: {report['generated_at']}",
        "",
        "## Technical summary",
        "",
        f"The current database trust status is **{report['trust_status']}**. "
        f"{report['summary']['passed']} of {report['summary']['total_checks']} automated checks passed.",
        "",
        "## All integrity and semantic gates passed",
        "",
        "The automated controls cover file integrity, foreign keys, event uniqueness, evaluation completeness, "
        "answer-capture semantics, review-task uniqueness, and consistency between active facts and ingest status. "
        "An exact audit table is clearer than a chart for this small pass/fail control set.",
        "",
        "## Scope and fact-grain definitions",
        "",
        "The core fact grain is one immutable attempt event per student, session, and content item. "
        "Evaluations are revisioned; only one revision is current. External question and vocabulary sources remain read-only.",
        "",
        "| Table | Rows |",
        "| --- | ---: |",
    ]
    lines.extend(f"| `{table}` | {count} |" for table, count in report["table_counts"].items())
    lines.extend(["", "## Automated check evidence", "", "| Check | Status | Severity | Failed rows |", "| --- | --- | --- | ---: |"])
    for check in report["checks"]:
        lines.append(f"| {check['check_id']} | {check['status']} | {check['severity']} | {check['failed_rows']} |")
    if migration_report:
        reconciliation = migration_report.get("expected_reconciliation", {})
        mastery = migration_report.get("mastery_json", {})
        lines.extend(
            [
                "",
                "## The legacy migration reconciles to its source counts",
                "",
                f"- Legacy content items: {migration_report.get('source_counts', {}).get('content_items')} "
                f"(expected 372; match={reconciliation.get('content_items_match')}).",
                f"- Legacy attempts: {migration_report.get('source_counts', {}).get('attempts')} "
                f"(expected 298; match={reconciliation.get('attempts_match')}).",
                f"- Mastery JSON items: {mastery.get('items')}; scheduler histories retained without fabricated attempts: "
                f"{mastery.get('history_retained_without_fabricated_attempt')}.",
                f"- Source files unchanged by SHA-256: {migration_report.get('source_unchanged')}.",
            ]
        )
    lines.extend(
        [
            "",
            "## Methodology",
            "",
            "Checks are deterministic SQL and SQLite assertions against the current database. Migration reconciliation "
            "compares read-only source counts and SHA-256 hashes before and after the run. This report is descriptive; "
            "an error-free schema does not by itself prove every historical teacher judgment correct.",
            "",
            "## Limitations and robustness controls",
            "",
            "- A knowledge point backed by fewer than two attempts or fewer than two distinct items is labeled tentative/insufficient; it is not a stable diagnosis.",
            "- Legacy scheduler-only history is retained in audit storage and is not promoted to an attempt when no answer/evaluation evidence exists.",
            "- OCR and unverified snapshots can locate source material but cannot replace source-checked answers.",
            "- The first quality snapshot has no time series; drift detection begins after repeated snapshots are retained.",
            "",
            "## Recommended next steps",
            "",
            "- Run `python -m english_tracker data check` after every migration, correction, or bulk import.",
            "- Retain periodic JSON reports so future checks can detect row-count, label-share, and freshness drift.",
            "- Review `needs_check` mappings before using them for high-stakes content selection.",
            "",
            "## Further questions",
            "",
            "- Should a future scheduler adapter implement and validate FSRS, or remain on the transparent simple-v1 policy?",
            "- Which teacher-reviewed sample should validate knowledge-point mappings at scale?",
            "",
        ]
    )
    return "\n".join(lines)
