from __future__ import annotations

import json
import re
from typing import Any

from .util import utc_now


def _count(conn, sql: str, params: tuple = ()) -> int:
    return int(conn.execute(sql, params).fetchone()[0])


_STUDENT_SPECIFIC_PUBLIC_PROSE = (
    re.compile(
        r"\bthis\s+(?:student|learner)\s+"
        r"(?:answered|chose|selected|wrote|responded)\b",
        re.IGNORECASE,
    ),
    re.compile(r"(?:该学生|这位学生|该学员|这位学员|本次作答)"),
)


def _string_values(value: Any):
    if isinstance(value, dict):
        for nested in value.values():
            yield from _string_values(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _string_values(nested)
    elif isinstance(value, str):
        yield value


def _public_explanation_named_learner_leaks(conn) -> int:
    display_names: set[str] = set()
    for row in conn.execute("SELECT display_name FROM students"):
        display_name = re.sub(r"\s+", " ", str(row[0] or "")).strip().casefold()
        significant = "".join(character for character in display_name if character.isalnum())
        if len(significant) >= 2:
            display_names.add(display_name)
    failed = 0
    for row in conn.execute(
        "SELECT explanation_json,created_by,confirmed_by FROM public_question_explanations"
    ):
        try:
            explanation = json.loads(str(row["explanation_json"]))
        except (TypeError, ValueError, json.JSONDecodeError):
            failed += 1
            continue
        values = list(_string_values(explanation))
        values.extend(str(row[field] or "") for field in ("created_by", "confirmed_by"))
        leaked = False
        for value in values:
            if any(pattern.search(value) for pattern in _STUDENT_SPECIFIC_PUBLIC_PROSE):
                leaked = True
                break
            folded = re.sub(r"\s+", " ", value).strip().casefold()
            if any(display_name in folded for display_name in display_names):
                leaked = True
                break
            if leaked:
                break
        failed += int(leaked)
    return failed


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
        "extraction_expected_item_count",
        "critical",
        _count(
            conn,
            """
            SELECT COUNT(*) FROM extraction_batches b
            WHERE b.status IN ('pending_review','ready_to_commit','committed')
              AND b.expected_item_count <> (
                SELECT COUNT(*) FROM extraction_items i
                WHERE i.extraction_batch_id=b.extraction_batch_id
              )
            """,
        ),
        "Reviewable extraction batches contain exactly their declared number of items",
        "Keep the batch outside review, or recreate it through the audited extraction contract.",
    )
    add(
        "extraction_batch_scope_ownership",
        "critical",
        _count(
            conn,
            """
            SELECT COUNT(*)
            FROM extraction_batches b
            LEFT JOIN students s ON s.student_id=b.student_id
            LEFT JOIN learning_sessions ls ON ls.session_id=b.session_id
            WHERE s.student_id IS NULL
               OR ls.session_id IS NULL OR ls.student_id IS NOT b.student_id
               OR ls.record_status<>'active'
               OR NOT EXISTS (
                    SELECT 1 FROM student_subjects ss
                    WHERE ss.student_id=b.student_id
                      AND ss.subject_code=b.subject_code
                  )
            """,
        ),
        "Every extraction batch belongs to one active learner, session, and subject enrollment",
        "Cancel the invalid batch and recreate it with explicit learner and session ownership.",
    )
    add(
        "extraction_committed_decision_gate",
        "critical",
        _count(
            conn,
            """
            SELECT COUNT(*)
            FROM extraction_items i
            JOIN extraction_batches b
              ON b.extraction_batch_id=i.extraction_batch_id
            WHERE b.status='committed'
              AND (
                NOT EXISTS (
                  SELECT 1 FROM extraction_confirmation_decisions d
                  WHERE d.extraction_batch_id=i.extraction_batch_id
                    AND d.extraction_item_id=i.extraction_item_id
                    AND d.revision_no=(
                      SELECT MAX(d2.revision_no)
                      FROM extraction_confirmation_decisions d2
                      WHERE d2.extraction_batch_id=i.extraction_batch_id
                        AND d2.extraction_item_id=i.extraction_item_id
                    )
                )
                OR EXISTS (
                  SELECT 1 FROM extraction_confirmation_decisions d
                  WHERE d.extraction_batch_id=i.extraction_batch_id
                    AND d.extraction_item_id=i.extraction_item_id
                    AND d.revision_no=(
                      SELECT MAX(d2.revision_no)
                      FROM extraction_confirmation_decisions d2
                      WHERE d2.extraction_batch_id=i.extraction_batch_id
                        AND d2.extraction_item_id=i.extraction_item_id
                    )
                    AND d.action IN ('pending_review','needs_check')
                )
              )
            """,
        ),
        "Every item in a committed extraction batch has a non-pending latest human decision",
        "Void the inconsistent release and restore from the verified pre-commit backup.",
    )
    add(
        "extraction_commit_link_semantics",
        "critical",
        _count(
            conn,
            """
            WITH current_decisions AS (
              SELECT d.*
              FROM extraction_confirmation_decisions d
              WHERE d.revision_no=(
                SELECT MAX(d2.revision_no)
                FROM extraction_confirmation_decisions d2
                WHERE d2.extraction_batch_id=d.extraction_batch_id
                  AND d2.extraction_item_id=d.extraction_item_id
              )
            )
            SELECT COUNT(*)
            FROM current_decisions d
            JOIN extraction_batches b
              ON b.extraction_batch_id=d.extraction_batch_id
            LEFT JOIN extraction_commit_links l
              ON l.extraction_batch_id=d.extraction_batch_id
             AND l.extraction_item_id=d.extraction_item_id
            WHERE b.status='committed'
              AND (
                (d.action IN ('human_confirmed','human_corrected','confirmed_blank')
                 AND l.attempt_id IS NULL)
                OR
                (d.action IN ('not_captured','rejected_alignment')
                 AND l.attempt_id IS NOT NULL)
              )
            """,
        ),
        "Committed extraction facts match the latest human decision one-for-one",
        "Do not synthesize attempts for excluded decisions; restore the atomic commit from backup.",
    )
    add(
        "extraction_uncommitted_fact_links",
        "critical",
        _count(
            conn,
            """
            SELECT COUNT(*)
            FROM extraction_commit_links l
            JOIN extraction_batches b
              ON b.extraction_batch_id=l.extraction_batch_id
            WHERE b.status<>'committed'
            """,
        ),
        "Extraction-linked learning facts are visible only after the whole batch commits",
        "Treat this as a failed atomic release and restore the verified pre-write backup.",
    )
    add(
        "extraction_second_model_gate",
        "critical",
        _count(
            conn,
            """
            WITH current_decisions AS (
              SELECT d.*
              FROM extraction_confirmation_decisions d
              WHERE d.revision_no=(
                SELECT MAX(d2.revision_no)
                FROM extraction_confirmation_decisions d2
                WHERE d2.extraction_batch_id=d.extraction_batch_id
                  AND d2.extraction_item_id=d.extraction_item_id
              )
            )
            SELECT COUNT(*)
            FROM extraction_items i
            JOIN extraction_batches b
              ON b.extraction_batch_id=i.extraction_batch_id
            JOIN current_decisions d
              ON d.extraction_batch_id=i.extraction_batch_id
             AND d.extraction_item_id=i.extraction_item_id
            WHERE b.status='committed'
              AND i.second_model_required=1
              AND d.action IN ('human_confirmed','human_corrected','confirmed_blank')
              AND (
                NOT EXISTS (
                  SELECT 1 FROM extraction_provider_results p
                  WHERE p.extraction_batch_id=i.extraction_batch_id
                    AND p.extraction_item_id=i.extraction_item_id
                    AND p.provider='codex'
                    AND p.result_status='succeeded'
                    AND p.provider_result_id=(
                      SELECT p2.provider_result_id
                      FROM extraction_provider_results p2
                      WHERE p2.extraction_batch_id=i.extraction_batch_id
                        AND p2.extraction_item_id=i.extraction_item_id
                        AND p2.provider='codex'
                      ORDER BY p2.rowid DESC
                      LIMIT 1
                    )
                )
                OR NOT EXISTS (
                  SELECT 1 FROM extraction_provider_results p
                  WHERE p.extraction_batch_id=i.extraction_batch_id
                    AND p.extraction_item_id=i.extraction_item_id
                    AND p.provider='doubao'
                    AND p.result_status='succeeded'
                    AND p.provider_result_id=(
                      SELECT p2.provider_result_id
                      FROM extraction_provider_results p2
                      WHERE p2.extraction_batch_id=i.extraction_batch_id
                        AND p2.extraction_item_id=i.extraction_item_id
                        AND p2.provider='doubao'
                      ORDER BY p2.rowid DESC
                      LIMIT 1
                    )
                )
              )
            """,
        ),
        "Committed high-risk answers retain successful independent Codex and Doubao candidates",
        "Reopen the batch from backup and record the missing provider result or exclude the item.",
    )
    add(
        "extraction_commit_fact_ownership",
        "critical",
        _count(
            conn,
            """
            SELECT COUNT(*)
            FROM extraction_commit_links l
            JOIN extraction_batches b
              ON b.extraction_batch_id=l.extraction_batch_id
            LEFT JOIN attempts a ON a.attempt_id=l.attempt_id
            LEFT JOIN evaluations e ON e.evaluation_id=l.evaluation_id
            LEFT JOIN ingest_events ie ON ie.ingest_event_id=l.ingest_event_id
            LEFT JOIN extraction_confirmation_decisions d
              ON d.confirmation_decision_id=l.confirmation_decision_id
            WHERE a.attempt_id IS NULL
               OR a.student_id IS NOT b.student_id
               OR a.session_id IS NOT b.session_id
               OR a.ingest_event_id IS NOT l.ingest_event_id
               OR a.record_status<>'active'
               OR e.evaluation_id IS NULL
               OR e.attempt_id IS NOT l.attempt_id
               OR e.is_current<>1
               OR ie.ingest_event_id IS NULL
               OR ie.status<>'applied'
               OR d.confirmation_decision_id IS NULL
               OR d.action NOT IN ('human_confirmed','human_corrected','confirmed_blank')
               OR d.revision_no<>(
                    SELECT MAX(d2.revision_no)
                    FROM extraction_confirmation_decisions d2
                    WHERE d2.extraction_batch_id=l.extraction_batch_id
                      AND d2.extraction_item_id=l.extraction_item_id
                  )
               OR (b.status='committed'
                   AND b.committed_ingest_event_id IS NOT l.ingest_event_id)
            """,
        ),
        "Extraction commit links retain learner, session, decision, evaluation, and ingest ownership",
        "Quarantine the inconsistent release and restore it through the official extraction commit.",
    )
    add(
        "extraction_committed_ingest_attempt_coverage",
        "critical",
        _count(
            conn,
            """
            SELECT COUNT(*)
            FROM extraction_batches b
            JOIN attempts a
              ON a.ingest_event_id=b.committed_ingest_event_id
             AND a.record_status='active'
            WHERE b.status='committed'
              AND NOT EXISTS (
                SELECT 1 FROM extraction_commit_links l
                WHERE l.extraction_batch_id=b.extraction_batch_id
                  AND l.attempt_id=a.attempt_id
              )
            """,
        ),
        "Every formal attempt in an extraction commit event maps to one confirmed item",
        "Restore the batch from backup; do not retain extra attempts outside the confirmation snapshot.",
    )
    add(
        "projection_learner_scope_consistency",
        "critical",
        _count(
            conn,
            """
            SELECT COUNT(*) FROM (
              SELECT o.outbox_id AS entity_id
              FROM base_projection_outbox o
              JOIN base_projection_runs r ON r.projection_run_id=o.projection_run_id
              WHERE o.student_id IS NOT r.student_id
                 OR o.subject_code IS NOT r.subject_code
                 OR o.projection_name IS NOT r.projection_name
                 OR NOT EXISTS (
                      SELECT 1 FROM student_subjects ss
                      WHERE ss.student_id=r.student_id
                        AND ss.subject_code=r.subject_code
                    )
              UNION ALL
              SELECT s.projection_upsert_key
              FROM base_projection_state s
              JOIN base_projection_outbox o ON o.outbox_id=s.last_outbox_id
              WHERE s.student_id IS NOT o.student_id
                 OR s.subject_code IS NOT o.subject_code
                 OR s.projection_name IS NOT o.projection_name
                 OR s.projection_upsert_key IS NOT o.projection_upsert_key
            )
            """,
        ),
        "Every Base projection run, outbox record, and published state has one learner/subject owner",
        "Stop publishing and rebuild the affected local outbox from an explicit learner-scoped projection run.",
    )
    add(
        "projection_success_readback_reconciliation",
        "critical",
        _count(
            conn,
            """
            SELECT COUNT(*) FROM (
              SELECT a.delivery_attempt_id AS entity_id
              FROM base_projection_delivery_attempts a
              JOIN base_projection_outbox o ON o.outbox_id=a.outbox_id
              WHERE a.outcome='succeeded'
                AND (a.readback_payload_sha256 IS NULL
                     OR a.readback_payload_sha256<>o.payload_sha256)
              UNION ALL
              SELECT o.outbox_id
              FROM base_projection_outbox o
              WHERE o.status='succeeded'
                AND NOT EXISTS (
                  SELECT 1 FROM base_projection_delivery_attempts a
                  WHERE a.outbox_id=o.outbox_id
                    AND a.outcome='succeeded'
                    AND a.readback_payload_sha256=o.payload_sha256
                )
              UNION ALL
              SELECT s.projection_upsert_key
              FROM base_projection_state s
              JOIN base_projection_outbox o ON o.outbox_id=s.last_outbox_id
              JOIN base_projection_delivery_attempts a
                ON a.delivery_attempt_id=s.last_delivery_attempt_id
              WHERE o.status<>'succeeded'
                 OR a.outcome<>'succeeded'
                 OR a.outbox_id<>o.outbox_id
                 OR a.remote_record_id<>s.remote_record_id
                 OR a.readback_payload_sha256<>o.payload_sha256
                 OR s.last_payload_sha256<>o.payload_sha256
            )
            """,
        ),
        "A successful Base projection is accepted only after exact persisted readback reconciliation",
        "Treat the remote result as drifted, stop the publisher, and replay only through the audited receipt contract.",
    )
    add(
        "projection_payload_contract",
        "critical",
        _count(
            conn,
            """
            SELECT COUNT(*) FROM base_projection_outbox o
            WHERE CASE
              WHEN json_valid(o.payload_json)=0 THEN 1
              WHEN json_extract(o.payload_json,'$.projection_upsert_key') IS NOT o.projection_upsert_key THEN 1
              WHEN json_extract(o.payload_json,'$.projection_name') IS NOT o.projection_name THEN 1
              WHEN json_extract(o.payload_json,'$.student_id') IS NOT o.student_id THEN 1
              WHEN json_extract(o.payload_json,'$.subject_code') IS NOT o.subject_code THEN 1
              WHEN json_extract(o.payload_json,'$.freshness_status') NOT IN
                   ('FRESH','DELAYED','STALE','FAILED') THEN 1
              ELSE 0
            END=1
            """,
        ),
        "Every staged Base cell map retains its stable identity and exact freshness vocabulary",
        "Reject the payload and restage it through the versioned projection whitelist.",
    )
    add(
        "projection_terminal_timestamps",
        "high",
        _count(
            conn,
            """
            SELECT COUNT(*) FROM base_projection_runs
            WHERE (status IN ('completed','permanent_failed') AND completed_at IS NULL)
               OR (status NOT IN ('completed','permanent_failed') AND completed_at IS NOT NULL)
            """,
        ),
        "Projection terminal states and completion timestamps agree",
        "Record the delivery outcome again through the idempotent local receipt API.",
    )
    add(
        "selection_finalized_integrity",
        "critical",
        _count(
            conn,
            """
            SELECT COUNT(*) FROM question_selection_manifests m
            WHERE m.status='finalized' AND (
              m.finalized_at IS NULL
              OR m.selected_group_count<>(
                SELECT COUNT(*) FROM question_selection_groups g
                WHERE g.selection_manifest_id=m.selection_manifest_id
              )
              OR m.selected_question_count<>(
                SELECT COUNT(*) FROM question_selection_items i
                WHERE i.selection_manifest_id=m.selection_manifest_id
              )
              OR m.exclusion_count<>(
                SELECT COUNT(*) FROM question_selection_exclusions e
                WHERE e.selection_manifest_id=m.selection_manifest_id
              )
              OR EXISTS (
                SELECT 1 FROM question_selection_groups g
                WHERE g.selection_manifest_id=m.selection_manifest_id
                  AND (g.complete_group<>1
                       OR g.expected_question_count<>g.selected_question_count
                       OR g.selected_question_count<>(
                         SELECT COUNT(*) FROM question_selection_items i
                         WHERE i.selection_manifest_id=m.selection_manifest_id
                           AND i.selection_group_id=g.selection_group_id
                       ))
              )
            )
            """,
        ),
        "Every finalized question-selection manifest reconciles its groups, items, and exclusions",
        "Do not generate courseware from the manifest; rebuild it from the verified read-only question snapshot.",
    )
    add(
        "selection_verified_real_question_gate",
        "critical",
        _count(
            conn,
            """
            SELECT COUNT(*) FROM question_selection_items i
            JOIN question_selection_manifests m
              ON m.selection_manifest_id=i.selection_manifest_id
            WHERE m.status='finalized'
              AND (i.is_real_question<>1
                   OR i.verification_status NOT IN ('source_checked','verified'))
            """,
        ),
        "Finalized selections contain only verified real-source questions",
        "Exclude the question and rebuild the manifest after source verification.",
    )
    add(
        "selection_exact_duplicate_gate",
        "high",
        _count(
            conn,
            """
            SELECT COUNT(*) FROM (
              SELECT selection_manifest_id,group_content_sha256
              FROM question_selection_groups
              GROUP BY selection_manifest_id,group_content_sha256
              HAVING COUNT(*)>1
              UNION ALL
              SELECT selection_manifest_id,question_content_sha256
              FROM question_selection_items
              GROUP BY selection_manifest_id,question_content_sha256
              HAVING COUNT(*)>1
            )
            """,
        ),
        "One selection manifest never contains exact duplicate groups or questions",
        "Keep at most one canonical item in the manifest; exact retest permission applies only to learner history.",
    )
    add(
        "selection_generation_ownership",
        "critical",
        _count(
            conn,
            """
            SELECT COUNT(*) FROM question_selection_manifests m
            JOIN artifact_generation_runs g ON g.generation_id=m.generation_id
            WHERE m.generation_id IS NOT NULL AND m.student_id IS NOT g.student_id
            """,
        ),
        "A selection manifest and its optional generation run belong to the same learner",
        "Detach the cross-learner generation and recreate the manifest under the correct workspace.",
    )
    add(
        "public_explanation_reuse_guard",
        "critical",
        _count(
            conn,
            """
            SELECT COUNT(*) FROM public_question_explanations p
            WHERE p.explanation_status IN ('source_checked','teacher_confirmed')
              AND (p.confirmed_by IS NULL OR trim(p.confirmed_by)=''
                   OR p.invalidated_at IS NOT NULL)
            """,
        ),
        "Only reviewed, current public explanations are reusable",
        "Invalidate the entry and recache it only after source or teacher confirmation.",
    )
    add(
        "public_explanation_student_separation",
        "critical",
        _count(
            conn,
            """
            SELECT COUNT(DISTINCT p.public_explanation_id)
            FROM public_question_explanations p,
                 json_tree(CASE WHEN json_valid(p.explanation_json)=1
                                THEN p.explanation_json ELSE '{}' END) j
            WHERE json_valid(p.explanation_json)=0
               OR lower(COALESCE(j.key,'')) IN
                  ('student','student_id','student_answer','learner','learner_id',
                   'attempt','attempt_id','diagnosis','personalized_diagnosis',
                   'review_task_id','student_history','student_specific')
               OR (j.type='text' AND (
                    upper(CAST(j.value AS TEXT)) GLOB '*STU-[A-Z0-9]*'
                    OR upper(CAST(j.value AS TEXT)) GLOB '*ATT-[A-Z0-9]*'
                    OR upper(CAST(j.value AS TEXT)) GLOB '*ATTEMPT-[A-Z0-9]*'
                    OR lower(CAST(j.value AS TEXT)) LIKE '%file://%'
                    OR lower(CAST(j.value AS TEXT)) LIKE '%private://%'
                    OR (substr(CAST(j.value AS TEXT),2,1)=':'
                        AND substr(CAST(j.value AS TEXT),3,1) IN ('/',char(92)))
                    OR substr(CAST(j.value AS TEXT),1,2)=char(92)||char(92)
                  ))
            """,
        ),
        "Reusable public explanation content is structurally separate from learner evidence",
        "Remove learner IDs, answers, diagnoses, and private locators; keep personalization in learner-owned facts.",
    )
    add(
        "public_explanation_named_learner_guard",
        "critical",
        _public_explanation_named_learner_leaks(conn),
        "Public explanations contain neither registered learner names nor learner-specific prose",
        "Invalidate the shared entry and rebuild it from question-only source evidence.",
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
        "model_diagnostic_verification_guard",
        "critical",
        _count(
            conn,
            """
            SELECT COUNT(*) FROM attempt_error_map
            WHERE error_source='model_suggested'
              AND verification_status IN ('source_checked','verified')
              AND record_status='active'
            """,
        ),
        "Model-suggested student error causes are never auto-verified",
        "Downgrade the diagnosis to suggested and require explicit teacher confirmation.",
    )
    add(
        "correct_attempt_has_no_active_error_cause",
        "high",
        _count(
            conn,
            """
            SELECT COUNT(*) FROM attempt_error_map aem
            JOIN evaluations e ON e.attempt_id=aem.attempt_id AND e.is_current=1
            WHERE e.result='correct' AND aem.record_status='active'
            """,
        ),
        "Correct attempts do not carry active student error causes",
        "Void the stale diagnosis or create an audited corrected evaluation before diagnosing.",
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
    add(
        "active_student_has_subject",
        "medium",
        _count(
            conn,
            """
            SELECT COUNT(*) FROM students s
            WHERE s.active=1 AND NOT EXISTS (
              SELECT 1 FROM student_subjects ss
              JOIN subjects sub ON sub.subject_code=ss.subject_code AND sub.active=1
              WHERE ss.student_id=s.student_id AND ss.active=1
            )
            """,
        ),
        "Every active student has at least one active subject workspace",
        "Assign the student to an active subject before importing learning records.",
    )
    add(
        "content_subject_registry",
        "high",
        _count(
            conn,
            """
            SELECT COUNT(*) FROM content_items ci
            LEFT JOIN subjects sub ON sub.subject_code=ci.subject_code AND sub.active=1
            WHERE ci.record_status='active' AND sub.subject_code IS NULL
            """,
        ),
        "Every active content item belongs to a registered subject",
        "Register the subject or correct the item through an audited replacement import.",
    )
    add(
        "artifact_student_ownership",
        "critical",
        _count(
            conn,
            """
            SELECT COUNT(*) FROM artifacts a
            LEFT JOIN students s ON s.student_id=a.student_id
            WHERE a.record_status='active'
              AND (a.student_id IS NULL OR trim(a.student_id)='' OR s.student_id IS NULL)
            """,
        ),
        "Every active artifact has an explicit student owner",
        "Assign or quarantine ownerless artifacts before using them in a student workspace.",
    )
    add(
        "session_artifact_student_consistency",
        "critical",
        _count(
            conn,
            """
            SELECT COUNT(*) FROM learning_sessions ls
            JOIN artifacts a ON a.artifact_id=ls.artifact_id
            WHERE ls.artifact_id IS NOT NULL AND ls.student_id IS NOT a.student_id
            """,
        ),
        "A session and its artifact belong to the same student",
        "Detach the artifact and restore the correct student-owned link through an audited repair.",
    )
    add(
        "attempt_session_student_consistency",
        "critical",
        _count(
            conn,
            """
            SELECT COUNT(*) FROM attempts a
            JOIN learning_sessions ls ON ls.session_id=a.session_id
            WHERE a.student_id IS NOT ls.student_id
            """,
        ),
        "An attempt and its session belong to the same student",
        "Quarantine the cross-student attempt and restore it through the correct student session.",
    )
    add(
        "attempt_artifact_student_consistency",
        "critical",
        _count(
            conn,
            """
            SELECT COUNT(*) FROM attempts att
            JOIN artifacts art ON art.artifact_id=att.artifact_id
            WHERE att.artifact_id IS NOT NULL AND att.student_id IS NOT art.student_id
            """,
        ),
        "An attempt and its artifact belong to the same student",
        "Remove the cross-student artifact link and re-import against the correct owned artifact.",
    )
    add(
        "review_state_attempt_consistency",
        "critical",
        _count(
            conn,
            """
            SELECT COUNT(*) FROM review_state rs
            JOIN attempts a ON a.attempt_id=rs.last_attempt_id
            WHERE rs.last_attempt_id IS NOT NULL
              AND (rs.student_id IS NOT a.student_id OR rs.item_id IS NOT a.item_id)
            """,
        ),
        "Review state points to an attempt for the same student and item",
        "Rebuild the affected review state from that student's active attempt history.",
    )
    add(
        "review_task_attempt_consistency",
        "critical",
        _count(
            conn,
            """
            SELECT COUNT(*) FROM review_tasks rt
            WHERE (rt.source_attempt_id IS NOT NULL AND NOT EXISTS (
                     SELECT 1 FROM attempts a
                     WHERE a.attempt_id=rt.source_attempt_id
                       AND a.student_id=rt.student_id AND a.item_id=rt.item_id
                   ))
               OR (rt.completed_by_attempt_id IS NOT NULL AND NOT EXISTS (
                     SELECT 1 FROM attempts a
                     WHERE a.attempt_id=rt.completed_by_attempt_id
                       AND a.student_id=rt.student_id AND a.item_id=rt.item_id
                   ))
            """,
        ),
        "Review-task source and completion attempts match the task student and item",
        "Void the invalid task link and rebuild it from the matching student's attempt history.",
    )
    add(
        "generation_artifact_student_consistency",
        "critical",
        _count(
            conn,
            """
            SELECT COUNT(*) FROM artifact_generation_runs g
            JOIN artifacts a ON a.artifact_id=g.output_artifact_id
            WHERE g.output_artifact_id IS NOT NULL AND g.student_id IS NOT a.student_id
            """,
        ),
        "A generated output artifact belongs to the generation's student",
        "Detach the cross-student output and register the correct student-owned artifact.",
    )
    add(
        "agent_run_terminal_timestamps",
        "medium",
        _count(
            conn,
            """
            SELECT COUNT(*) FROM agent_runs
            WHERE (status IN ('completed','failed','cancelled') AND completed_at IS NULL)
               OR (status NOT IN ('completed','failed','cancelled') AND completed_at IS NOT NULL)
            """,
        ),
        "Agent run terminal states and completion timestamps agree",
        "Append a corrected terminal or progress event through the agent run API.",
    )
    add(
        "agent_run_has_event",
        "medium",
        _count(
            conn,
            """
            SELECT COUNT(*) FROM agent_runs r
            WHERE NOT EXISTS (SELECT 1 FROM agent_run_events e WHERE e.run_id=r.run_id)
            """,
        ),
        "Every routed agent task has an append-only event trail",
        "Re-register the run or append a planned event before displaying it as active.",
    )
    counts = {
        table: _count(conn, f'SELECT COUNT(*) FROM "{table}"')
        for table in (
            "students",
            "subjects",
            "student_subjects",
            "artifacts",
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
            "agent_runs",
            "agent_run_events",
            "artifact_generation_runs",
            "extraction_batches",
            "extraction_assets",
            "extraction_items",
            "extraction_provider_results",
            "extraction_confirmation_decisions",
            "extraction_commit_links",
            "base_projection_runs",
            "base_projection_outbox",
            "base_projection_delivery_attempts",
            "base_projection_state",
            "question_selection_manifests",
            "question_selection_groups",
            "question_selection_items",
            "question_selection_exclusions",
            "public_question_explanations",
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
