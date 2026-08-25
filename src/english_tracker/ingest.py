from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from .contracts import (
    validate_attempts_payload,
    validate_diagnostics_payload,
    validate_progress_payload,
    validate_session_payload,
)
from .generation import mark_completed_generations_stale
from .util import canonical_json, normalize_alias, payload_hash, random_id, stable_id, utc_now


class IngestConflict(RuntimeError):
    pass


def _parse_time(value: str) -> datetime:
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    result = datetime.fromisoformat(raw)
    if result.tzinfo is None:
        result = result.replace(tzinfo=UTC)
    return result


def _record_event_row(conn, event_id: str, entity_type: str, entity_id: str, action: str, after: Any = None) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO ingest_event_rows(
          ingest_event_id, entity_type, entity_id, action, after_json
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (event_id, entity_type, entity_id, action, canonical_json(after) if after is not None else None),
    )


def _begin_event(conn, payload: dict, event_type: str, backup_path: str | None) -> tuple[bool, dict | None]:
    digest = payload_hash(payload)
    existing = conn.execute(
        """
        SELECT * FROM ingest_events
        WHERE ingest_event_id = ? OR idempotency_key = ?
        """,
        (payload["event_id"], payload["idempotency_key"]),
    ).fetchone()
    if existing:
        if existing["payload_sha256"] != digest:
            raise IngestConflict(
                "An event_id or idempotency_key already exists with a different payload; no data was changed."
            )
        return False, {
            "ingest_event_id": existing["ingest_event_id"],
            "status": existing["status"],
            "imported_at": existing["imported_at"],
            "rows_inserted": existing["rows_inserted"],
            "payload_sha256": existing["payload_sha256"],
        }
    conn.execute(
        """
        INSERT INTO ingest_events(
          ingest_event_id, idempotency_key, event_type, source_thread,
          payload_sha256, payload_json, status, backup_path, imported_at
        ) VALUES (?, ?, ?, ?, ?, ?, 'applied', ?, ?)
        """,
        (
            payload["event_id"],
            payload["idempotency_key"],
            event_type,
            payload["source_thread"],
            digest,
            canonical_json(payload),
            backup_path,
            utc_now(),
        ),
    )
    return True, None


def _finish_event(conn, event_id: str, total: int, inserted: int, skipped: int = 0) -> None:
    conn.execute(
        """
        UPDATE ingest_events
        SET rows_total = ?, rows_inserted = ?, rows_skipped = ?
        WHERE ingest_event_id = ?
        """,
        (total, inserted, skipped, event_id),
    )


def _require_student(conn, student_id: str) -> None:
    if not conn.execute("SELECT 1 FROM students WHERE student_id = ? AND active = 1", (student_id,)).fetchone():
        raise IngestConflict(f"Unknown or inactive student_id: {student_id}")


def import_session(conn, payload: dict, *, backup_path: str | None = None) -> dict[str, Any]:
    payload = validate_session_payload(payload)
    event_id = payload["event_id"]
    session = payload["session"]
    observations = payload.get("observations", [])
    progress = payload.get("progress", [])
    artifact = payload.get("artifact")
    assessment = payload.get("assessment")
    with conn:
        created, existing = _begin_event(conn, payload, "session_import", backup_path)
        if not created:
            return {"status": "duplicate", "event_id": event_id, "existing": existing}
        _require_student(conn, payload["student_id"])
        now = utc_now()
        artifact_id = None
        if artifact:
            artifact_id = artifact.get("artifact_id") or stable_id(
                "ART",
                payload["student_id"],
                artifact.get("title"),
                session["started_at"],
            )
            conn.execute(
                """
                INSERT INTO artifacts(
                  artifact_id, student_id, artifact_type, title, material_date, private_path,
                  external_uri, content_sha256, verification_status,
                  created_by_event_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    artifact_id,
                    payload["student_id"],
                    artifact.get("artifact_type", "material"),
                    artifact["title"],
                    artifact.get("material_date"),
                    artifact.get("private_path"),
                    artifact.get("external_uri"),
                    artifact.get("content_sha256"),
                    artifact.get("verification_status", "unverified"),
                    event_id,
                    now,
                    now,
                ),
            )
            _record_event_row(conn, event_id, "artifact", artifact_id, "insert", artifact)
        conn.execute(
            """
            INSERT INTO learning_sessions(
              session_id, student_id, source_thread, session_type, title,
              started_at, ended_at, artifact_id, planned_item_count,
              completed_item_count, note, created_by_event_id, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session["session_id"],
                payload["student_id"],
                payload["source_thread"],
                session["session_type"],
                session["title"],
                session["started_at"],
                session.get("ended_at"),
                artifact_id or session.get("artifact_id"),
                session.get("planned_item_count"),
                session.get("completed_item_count"),
                session.get("note"),
                event_id,
                now,
                now,
            ),
        )
        _record_event_row(conn, event_id, "learning_session", session["session_id"], "insert", session)
        if assessment:
            conn.execute(
                """
                INSERT INTO session_assessments(
                  session_id, assessment_kind, reporting_series, delivery_mode,
                  raw_score, max_score, duration_seconds, blank_count,
                  validation_status, created_by_event_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session["session_id"],
                    assessment["assessment_kind"],
                    assessment["reporting_series"],
                    assessment.get("delivery_mode", "unspecified"),
                    assessment.get("raw_score"),
                    assessment.get("max_score"),
                    assessment.get("duration_seconds"),
                    assessment.get("blank_count"),
                    assessment.get("validation_status", "unverified"),
                    event_id,
                    now,
                ),
            )
            _record_event_row(conn, event_id, "session_assessment", session["session_id"], "insert", assessment)
        for index, observation in enumerate(observations, start=1):
            observation_id = observation.get("observation_id") or stable_id("OBS", event_id, index)
            conn.execute(
                """
                INSERT INTO session_observations(
                  observation_id, session_id, observation_type, observation_text,
                  evidence_level, created_by_event_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    observation_id,
                    session["session_id"],
                    observation.get("observation_type", "teacher_observation"),
                    observation["observation_text"],
                    observation.get("evidence_level", "session_only"),
                    event_id,
                    now,
                ),
            )
            _record_event_row(conn, event_id, "session_observation", observation_id, "insert", observation)
        inserted_progress = _insert_progress_rows(
            conn, event_id, session["session_id"], progress, now
        )
        total = 1 + bool(artifact) + bool(assessment) + len(observations) + len(progress)
        _finish_event(conn, event_id, total, 1 + int(bool(artifact)) + int(bool(assessment)) + len(observations) + inserted_progress)
        mark_completed_generations_stale(conn, student_id=payload["student_id"])
    return {"status": "applied", "event_id": event_id, "session_id": session["session_id"]}


def _insert_progress_rows(conn, event_id: str, session_id: str, progress: list[dict], now: str) -> int:
    inserted = 0
    for index, row in enumerate(progress, start=1):
        progress_id = row.get("progress_id") or stable_id("PRG", event_id, index)
        conn.execute(
            """
            INSERT INTO session_progress(
              progress_id, session_id, content_label, external_namespace,
              external_id, progress_status, completed_count, total_count,
              note, created_by_event_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                progress_id,
                session_id,
                row["content_label"],
                row.get("external_namespace"),
                row.get("external_id"),
                row["progress_status"],
                row.get("completed_count"),
                row.get("total_count"),
                row.get("note"),
                event_id,
                now,
            ),
        )
        _record_event_row(conn, event_id, "session_progress", progress_id, "insert", row)
        inserted += 1
    return inserted


def import_progress(conn, payload: dict, *, backup_path: str | None = None) -> dict[str, Any]:
    payload = validate_progress_payload(payload)
    event_id = payload["event_id"]
    with conn:
        created, existing = _begin_event(conn, payload, "progress_import", backup_path)
        if not created:
            return {"status": "duplicate", "event_id": event_id, "existing": existing}
        _require_student(conn, payload["student_id"])
        session = conn.execute(
            "SELECT student_id FROM learning_sessions WHERE session_id = ? AND record_status = 'active'",
            (payload["session_id"],),
        ).fetchone()
        if not session or session["student_id"] != payload["student_id"]:
            raise IngestConflict("session_id is missing, inactive, or belongs to another student")
        count = _insert_progress_rows(conn, event_id, payload["session_id"], payload["progress"], utc_now())
        _finish_event(conn, event_id, len(payload["progress"]), count)
        mark_completed_generations_stale(conn, student_id=payload["student_id"])
    return {"status": "applied", "event_id": event_id, "rows_inserted": count}


def _find_external_item(conn, refs: list[dict]) -> str | None:
    found: set[str] = set()
    for ref in refs:
        row = conn.execute(
            """
            SELECT item_id FROM external_references
            WHERE namespace = ? AND reference_type = ? AND external_id = ?
            """,
            (ref["namespace"], ref["reference_type"], ref["external_id"]),
        ).fetchone()
        if row:
            found.add(row["item_id"])
    if len(found) > 1:
        raise IngestConflict("External references resolve to different content items")
    return next(iter(found), None)


def _resolve_item(conn, attempt: dict, ingest_event_id: str, now: str) -> tuple[str, bool]:
    item_payload = dict(attempt.get("item") or {})
    requested_subject_code = str(item_payload.get("subject_code") or "english").strip().lower()
    refs = list(item_payload.get("external_references") or attempt.get("external_references") or [])
    explicit_id = attempt.get("item_id") or item_payload.get("item_id")
    resolved_id = _find_external_item(conn, refs) if refs else None
    if explicit_id and resolved_id and explicit_id != resolved_id:
        raise IngestConflict(f"item_id {explicit_id} conflicts with an existing external reference")
    item_id = explicit_id or resolved_id
    if not item_id:
        if refs:
            primary = refs[0]
            item_id = stable_id("ITEM", primary["namespace"], primary["reference_type"], primary["external_id"])
        else:
            item_id = stable_id(
                "ITEM",
                requested_subject_code,
                item_payload.get("domain"),
                item_payload.get("item_type"),
                item_payload.get("prompt_snapshot"),
                item_payload.get("answer_snapshot"),
            )
    exists = conn.execute("SELECT subject_code FROM content_items WHERE item_id = ?", (item_id,)).fetchone()
    inserted = False
    if exists and "subject_code" in item_payload and exists["subject_code"] != requested_subject_code:
        raise IngestConflict(
            f"Existing item {item_id} belongs to subject {exists['subject_code']}, not {requested_subject_code}"
        )
    if not exists:
        if not item_payload.get("domain") or not item_payload.get("item_type"):
            raise IngestConflict(f"New item {item_id} requires item.domain and item.item_type")
        subject_code = requested_subject_code
        if not conn.execute(
            "SELECT 1 FROM subjects WHERE subject_code=? AND active=1",
            (subject_code,),
        ).fetchone():
            raise IngestConflict(f"Unknown or inactive item.subject_code: {subject_code}")
        content_hash = payload_hash(
            {
                "subject": subject_code,
                "domain": item_payload.get("domain"),
                "prompt": item_payload.get("prompt_snapshot"),
                "answer": item_payload.get("answer_snapshot"),
                "type": item_payload.get("item_type"),
            }
        )
        conn.execute(
            """
            INSERT INTO content_items(
              item_id, domain, item_type, subject_code, prompt_snapshot, answer_snapshot,
              direction, response_mode, difficulty_label, difficulty_weight,
              source_validation_status, legacy_ref, metadata_json, content_hash,
              created_by_event_id, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item_id,
                item_payload["domain"],
                item_payload["item_type"],
                subject_code,
                item_payload.get("prompt_snapshot"),
                item_payload.get("answer_snapshot"),
                item_payload.get("direction"),
                item_payload.get("response_mode", attempt.get("response_mode", "mixed")),
                item_payload.get("difficulty_label"),
                item_payload.get("difficulty_weight", 1.0),
                item_payload.get("source_validation_status", attempt.get("validation_status", "unverified")),
                item_payload.get("legacy_ref"),
                canonical_json(item_payload.get("metadata")) if item_payload.get("metadata") is not None else None,
                content_hash,
                ingest_event_id,
                now,
                now,
            ),
        )
        _record_event_row(conn, ingest_event_id, "content_item", item_id, "insert", item_payload)
        inserted = True
    for ref in refs:
        existing_ref = conn.execute(
            """SELECT item_id FROM external_references
               WHERE namespace=? AND reference_type=? AND external_id=?""",
            (ref["namespace"], ref["reference_type"], ref["external_id"]),
        ).fetchone()
        if existing_ref and existing_ref["item_id"] != item_id:
            raise IngestConflict(f"External reference already belongs to {existing_ref['item_id']}")
        if not existing_ref:
            ref_id = stable_id("XREF", ref["namespace"], ref["reference_type"], ref["external_id"])
            conn.execute(
                """
                INSERT INTO external_references(
                  external_reference_id, item_id, namespace, reference_type,
                  external_id, external_parent_id, source_validation_status,
                  metadata_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ref_id,
                    item_id,
                    ref["namespace"],
                    ref["reference_type"],
                    ref["external_id"],
                    ref.get("external_parent_id"),
                    ref.get("source_validation_status", item_payload.get("source_validation_status", "unverified")),
                    canonical_json(ref.get("metadata")) if ref.get("metadata") is not None else None,
                    now,
                ),
            )
            _record_event_row(conn, ingest_event_id, "external_reference", ref_id, "link", ref)
    knowledge_codes = item_payload.get("knowledge_points") or attempt.get("knowledge_points") or []
    for index, kp in enumerate(knowledge_codes):
        if isinstance(kp, str):
            code, role, weight = kp, "primary" if index == 0 else "secondary", 1.0
        else:
            code = kp["code"]
            role = kp.get("role", "primary" if index == 0 else "secondary")
            weight = kp.get("weight", 1.0)
        row = conn.execute("SELECT knowledge_point_id FROM knowledge_points WHERE code = ? AND active = 1", (code,)).fetchone()
        if not row:
            raise IngestConflict(f"Unknown knowledge point code: {code}")
        conn.execute(
            """
            INSERT OR IGNORE INTO item_knowledge_map(
              item_id, knowledge_point_id, mapping_role, weight,
              mapping_source, confidence, verification_status, rationale
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item_id,
                row["knowledge_point_id"],
                role,
                weight,
                kp.get("mapping_source", "manual") if isinstance(kp, dict) else "manual",
                kp.get("confidence", 1.0) if isinstance(kp, dict) else 1.0,
                kp.get("verification_status", kp.get("validation_status", "unverified")) if isinstance(kp, dict) else "unverified",
                kp.get("rationale", kp.get("evidence_source")) if isinstance(kp, dict) else "Imported through the stable attempt contract.",
            ),
        )
    return item_id, inserted


def _resolve_error_type(conn, error: str | dict) -> tuple[str, str, float, str | None]:
    if isinstance(error, str):
        raw, code, confidence, note = error, None, 1.0, None
    else:
        raw = error.get("raw_error_type") or error.get("code") or ""
        code = error.get("code")
        confidence = float(error.get("confidence", 1.0))
        note = error.get("note")
    row = None
    if code:
        row = conn.execute("SELECT error_type_id FROM error_types WHERE code = ? AND active = 1", (code,)).fetchone()
    if not row:
        row = conn.execute(
            "SELECT error_type_id FROM error_type_aliases WHERE alias_normalized = ?",
            (normalize_alias(raw),),
        ).fetchone()
    if not row:
        row = conn.execute("SELECT error_type_id FROM error_types WHERE code = 'needs_check'").fetchone()
        confidence = min(confidence, 0.5)
        note = note or "Unmapped raw label retained for manual review."
    return row["error_type_id"], raw, confidence, note


def _upsert_review_state(conn, attempt_id: str, student_id: str, item_id: str, attempted_at: str, result: str, phase: str, ingest_event_id: str) -> None:
    current = conn.execute(
        "SELECT * FROM review_state WHERE student_id = ? AND item_id = ?",
        (student_id, item_id),
    ).fetchone()
    if current and current["manual_override"]:
        return
    repetitions = (current["repetitions"] if current else 0) + 1
    lapses = (current["lapses"] if current else 0) + (1 if result in {"wrong", "partial"} else 0)
    old_interval = current["interval_days"] if current else 0
    base_time = _parse_time(attempted_at)
    if result == "correct":
        interval = max(1, old_interval * 2 if old_interval else 1)
        consecutive = 0
        state = "mastered" if repetitions >= 3 and lapses == 0 else "learning"
        due = (base_time + timedelta(days=interval)).isoformat()
    else:
        interval = 1
        consecutive = (current["consecutive_errors"] if current else 0) + 1
        state = "due"
        due = (base_time + timedelta(days=1)).isoformat()
    now = utc_now()
    conn.execute(
        """
        INSERT INTO review_state(
          student_id, item_id, state, due_at, interval_days, repetitions,
          lapses, consecutive_errors, last_attempt_id, last_result,
          last_reviewed_at, scheduling_algorithm, algorithm_version, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'simple-v1', '1', ?)
        ON CONFLICT(student_id, item_id) DO UPDATE SET
          state=excluded.state, due_at=excluded.due_at,
          interval_days=excluded.interval_days, repetitions=excluded.repetitions,
          lapses=excluded.lapses, consecutive_errors=excluded.consecutive_errors,
          last_attempt_id=excluded.last_attempt_id, last_result=excluded.last_result,
          last_reviewed_at=excluded.last_reviewed_at,
          scheduling_algorithm=excluded.scheduling_algorithm,
          algorithm_version=excluded.algorithm_version, updated_at=excluded.updated_at
        """,
        (
            student_id,
            item_id,
            state,
            due,
            interval,
            repetitions,
            lapses,
            consecutive,
            attempt_id,
            result,
            attempted_at,
            now,
        ),
    )
    if result in {"wrong", "partial", "needs_check"}:
        existing_task = conn.execute(
            "SELECT review_task_id FROM review_tasks WHERE student_id=? AND item_id=? AND status='open'",
            (student_id, item_id),
        ).fetchone()
        if not existing_task:
            task_id = stable_id("RT", student_id, item_id, attempt_id)
            conn.execute(
                """
                INSERT INTO review_tasks(
                  review_task_id, student_id, item_id, source_attempt_id,
                  reason_code, due_at, priority, status, created_by_event_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)
                """,
                (
                    task_id,
                    student_id,
                    item_id,
                    attempt_id,
                    "incorrect_attempt" if result != "needs_check" else "needs_check",
                    due,
                    1.0 + min(consecutive, 5) * 0.2,
                    ingest_event_id,
                    now,
                ),
            )
            _record_event_row(conn, ingest_event_id, "review_task", task_id, "insert")
    elif result == "correct":
        conn.execute(
            """
            UPDATE review_tasks SET status='completed', completed_by_attempt_id=?, completed_at=?
            WHERE student_id=? AND item_id=? AND status='open'
            """,
            (attempt_id, attempted_at, student_id, item_id),
        )


def import_attempts(conn, payload: dict, *, backup_path: str | None = None) -> dict[str, Any]:
    payload = validate_attempts_payload(payload)
    ingest_event_id = payload["event_id"]
    with conn:
        created, existing = _begin_event(conn, payload, "attempts_import", backup_path)
        if not created:
            return {"status": "duplicate", "event_id": ingest_event_id, "existing": existing}
        _require_student(conn, payload["student_id"])
        session = conn.execute(
            """SELECT * FROM learning_sessions
               WHERE session_id=? AND student_id=? AND record_status='active'""",
            (payload["session_id"], payload["student_id"]),
        ).fetchone()
        if not session:
            raise IngestConflict("Active session not found for student")
        inserted_attempts = 0
        inserted_items = 0
        now = utc_now()
        for index, attempt in enumerate(payload["attempts"], start=1):
            item_id, item_inserted = _resolve_item(conn, attempt, ingest_event_id, now)
            inserted_items += int(item_inserted)
            item_subject = conn.execute(
                "SELECT subject_code FROM content_items WHERE item_id=? AND record_status='active'",
                (item_id,),
            ).fetchone()
            if item_subject:
                conn.execute(
                    """
                    INSERT INTO student_subjects(student_id,subject_code,active,enrolled_at)
                    VALUES (?,?,1,?)
                    ON CONFLICT(student_id,subject_code) DO UPDATE SET active=1
                    """,
                    (payload["student_id"], item_subject["subject_code"], now),
                )
            prior_count = conn.execute(
                """SELECT COUNT(*) FROM attempts
                   WHERE student_id=? AND item_id=? AND record_status='active'""",
                (payload["student_id"], item_id),
            ).fetchone()[0]
            phase = attempt.get("attempt_phase") or ("review" if prior_count else "first")
            attempt_id = attempt.get("attempt_id") or stable_id("ATT", attempt["event_id"])
            evaluation = attempt["evaluation"]
            conn.execute(
                """
                INSERT INTO attempts(
                  attempt_id, event_id, ingest_event_id, student_id, session_id,
                  item_id, artifact_id, attempted_at, student_answer,
                  standard_answer_snapshot, answer_capture_status, attempt_phase,
                  response_mode, validation_status, teacher_note, source_material_ref,
                  supersedes_attempt_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    attempt_id,
                    attempt["event_id"],
                    ingest_event_id,
                    payload["student_id"],
                    payload["session_id"],
                    item_id,
                    attempt.get("artifact_id") or session["artifact_id"],
                    attempt["attempted_at"],
                    attempt.get("student_answer"),
                    attempt.get("standard_answer"),
                    attempt["answer_capture_status"],
                    phase,
                    attempt.get("response_mode", "unknown"),
                    attempt.get("validation_status", "unverified"),
                    attempt.get("teacher_note"),
                    attempt.get("source_material_ref"),
                    attempt.get("supersedes_attempt_id"),
                    now,
                ),
            )
            evaluation_id = stable_id("EVAL", attempt_id, 1)
            conn.execute(
                """
                INSERT INTO evaluations(
                  evaluation_id, attempt_id, revision_no, result, score,
                  max_score, evaluated_by, is_human_corrected, note, created_at
                ) VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    evaluation_id,
                    attempt_id,
                    evaluation["result"],
                    evaluation.get("score"),
                    evaluation.get("max_score"),
                    evaluation.get("evaluated_by", "teacher"),
                    int(bool(evaluation.get("is_human_corrected", False))),
                    evaluation.get("note"),
                    now,
                ),
            )
            for error in attempt.get("error_types", []):
                error_type_id, raw, confidence, note = _resolve_error_type(conn, error)
                conn.execute(
                    """
                    INSERT INTO attempt_error_map(
                      attempt_id, error_type_id, raw_error_type, confidence, note,
                      error_source, verification_status, rationale, record_status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active')
                    """,
                    (
                        attempt_id,
                        error_type_id,
                        raw,
                        confidence,
                        note,
                        error.get("error_source", "manual") if isinstance(error, dict) else "manual",
                        error.get("verification_status", "unverified") if isinstance(error, dict) else "unverified",
                        error.get("rationale") if isinstance(error, dict) else None,
                    ),
                )
            _upsert_review_state(
                conn,
                attempt_id,
                payload["student_id"],
                item_id,
                attempt["attempted_at"],
                evaluation["result"],
                phase,
                ingest_event_id,
            )
            _record_event_row(conn, ingest_event_id, "attempt", attempt_id, "insert", attempt)
            inserted_attempts += 1
        _finish_event(
            conn,
            ingest_event_id,
            len(payload["attempts"]),
            inserted_attempts,
        )
        mark_completed_generations_stale(conn, student_id=payload["student_id"])
    return {
        "status": "applied",
        "event_id": ingest_event_id,
        "attempts_inserted": inserted_attempts,
        "content_items_inserted": inserted_items,
    }


def import_attempt_diagnostics(conn, payload: dict, *, backup_path: str | None = None) -> dict[str, Any]:
    """Attach evidence-backed error causes without changing the original attempt."""
    payload = validate_diagnostics_payload(payload)
    ingest_event_id = payload["event_id"]
    inserted = 0
    updated = 0
    skipped = 0
    with conn:
        created, existing = _begin_event(conn, payload, "attempt_diagnostics", backup_path)
        if not created:
            return {"status": "duplicate", "event_id": ingest_event_id, "existing": existing}
        _require_student(conn, payload["student_id"])
        now = utc_now()
        for diagnostic in payload["diagnostics"]:
            attempt = conn.execute(
                """
                SELECT a.attempt_id,a.answer_capture_status,e.result
                FROM attempts a
                JOIN evaluations e ON e.attempt_id=a.attempt_id AND e.is_current=1
                WHERE a.attempt_id=? AND a.student_id=? AND a.record_status='active'
                """,
                (diagnostic["attempt_id"], payload["student_id"]),
            ).fetchone()
            if not attempt:
                raise IngestConflict(f"Unknown active attempt for student: {diagnostic['attempt_id']}")
            if attempt["answer_capture_status"] == "not_captured":
                raise IngestConflict(
                    f"Attempt {attempt['attempt_id']} has answer_capture_status=not_captured; "
                    "specific error causes cannot be inferred"
                )
            if attempt["result"] not in {"wrong", "partial", "needs_check"}:
                raise IngestConflict(f"Attempt {attempt['attempt_id']} is correct; an error cause cannot be attached")
            for error in diagnostic["error_types"]:
                error_type_id, raw, confidence, note = _resolve_error_type(conn, error)
                raw = raw or error.get("code") or "needs_check"
                source = error.get("error_source", "model_suggested")
                verification = error.get("verification_status", "suggested" if source == "model_suggested" else "unverified")
                existing_row = conn.execute(
                    """
                    SELECT * FROM attempt_error_map
                    WHERE attempt_id=? AND error_type_id=? AND COALESCE(raw_error_type,'')=?
                    ORDER BY CASE record_status WHEN 'active' THEN 0 ELSE 1 END LIMIT 1
                    """,
                    (attempt["attempt_id"], error_type_id, raw),
                ).fetchone()
                if existing_row and (
                    (existing_row["verification_status"] in {"verified", "source_checked"} and verification not in {"verified", "source_checked"})
                    or (existing_row["error_source"] in {"manual", "teacher_observation"} and source == "model_suggested")
                ):
                    skipped += 1
                    continue
                values = (
                    confidence,
                    note,
                    source,
                    verification,
                    error["rationale"],
                    attempt["attempt_id"],
                    error_type_id,
                    raw,
                )
                if existing_row:
                    before = dict(existing_row)
                    conn.execute(
                        """
                        UPDATE attempt_error_map
                        SET confidence=?,note=?,error_source=?,verification_status=?,rationale=?,
                            record_status='active',invalidation_reason=NULL
                        WHERE attempt_id=? AND error_type_id=? AND COALESCE(raw_error_type,'')=?
                        """,
                        values,
                    )
                    action = "update"
                    updated += 1
                else:
                    before = None
                    conn.execute(
                        """
                        INSERT INTO attempt_error_map(
                          attempt_id,error_type_id,raw_error_type,confidence,note,
                          error_source,verification_status,rationale,record_status
                        ) VALUES (?,?,?,?,?,?,?,?,'active')
                        """,
                        (
                            attempt["attempt_id"],
                            error_type_id,
                            raw,
                            confidence,
                            note,
                            source,
                            verification,
                            error["rationale"],
                        ),
                    )
                    action = "insert"
                    inserted += 1
                after = dict(
                    conn.execute(
                        """
                        SELECT * FROM attempt_error_map
                        WHERE attempt_id=? AND error_type_id=? AND COALESCE(raw_error_type,'')=?
                        """,
                        (attempt["attempt_id"], error_type_id, raw),
                    ).fetchone()
                )
                mapping_id = stable_id("DIAG", attempt["attempt_id"], error_type_id, raw)
                _record_event_row(conn, ingest_event_id, "attempt_error_map", mapping_id, "insert" if action == "insert" else "link", after)
                conn.execute(
                    """
                    INSERT INTO audit_log(
                      audit_id,occurred_at,actor,action,entity_type,entity_id,
                      ingest_event_id,before_json,after_json,reason
                    ) VALUES (?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        random_id("AUD"),
                        now,
                        payload["source_thread"],
                        f"diagnostic_{action}",
                        "attempt_error_map",
                        mapping_id,
                        ingest_event_id,
                        canonical_json(before) if before is not None else None,
                        canonical_json(after),
                        error["rationale"],
                    ),
                )
        _finish_event(conn, ingest_event_id, sum(len(row["error_types"]) for row in payload["diagnostics"]), inserted + updated, skipped)
        if inserted + updated:
            mark_completed_generations_stale(conn, student_id=payload["student_id"])
    return {
        "status": "applied",
        "event_id": ingest_event_id,
        "diagnostics_inserted": inserted,
        "diagnostics_updated": updated,
        "diagnostics_skipped": skipped,
    }


def undo_ingest_event(conn, target_event_id: str, *, actor: str = "engineering", reason: str = "correction") -> dict[str, Any]:
    with conn:
        event = conn.execute("SELECT * FROM ingest_events WHERE ingest_event_id = ?", (target_event_id,)).fetchone()
        if not event:
            raise IngestConflict(f"Unknown ingest event: {target_event_id}")
        if event["status"] == "reverted":
            return {"status": "duplicate", "target_event_id": target_event_id}
        affected = [
            (row["student_id"], row["item_id"])
            for row in conn.execute(
                "SELECT DISTINCT student_id, item_id FROM attempts WHERE ingest_event_id = ?",
                (target_event_id,),
            )
        ]
        before = dict(event)
        now = utc_now()
        conn.execute("UPDATE attempts SET record_status='voided' WHERE ingest_event_id=?", (target_event_id,))
        conn.execute(
            """
            UPDATE attempt_error_map SET record_status='voided',
              invalidation_reason=COALESCE(invalidation_reason, 'Parent attempt ingest event was reverted.')
            WHERE attempt_id IN (SELECT attempt_id FROM attempts WHERE ingest_event_id=?)
            """,
            (target_event_id,),
        )
        conn.execute("UPDATE learning_sessions SET record_status='voided', updated_at=? WHERE created_by_event_id=?", (now, target_event_id))
        conn.execute("UPDATE session_observations SET record_status='voided' WHERE created_by_event_id=?", (target_event_id,))
        conn.execute("UPDATE session_progress SET record_status='voided' WHERE created_by_event_id=?", (target_event_id,))
        conn.execute("UPDATE artifacts SET record_status='voided', updated_at=? WHERE created_by_event_id=?", (now, target_event_id))
        conn.execute("UPDATE review_tasks SET status='voided' WHERE created_by_event_id=? AND status='open'", (target_event_id,))
        conn.execute("UPDATE ingest_events SET status='reverted', reverted_at=? WHERE ingest_event_id=?", (now, target_event_id))
        for student_id, item_id in affected:
            _rebuild_review_state(conn, student_id, item_id)
        audit_id = random_id("AUD")
        after = dict(conn.execute("SELECT * FROM ingest_events WHERE ingest_event_id=?", (target_event_id,)).fetchone())
        conn.execute(
            """
            INSERT INTO audit_log(
              audit_id, occurred_at, actor, action, entity_type, entity_id,
              ingest_event_id, before_json, after_json, reason
            ) VALUES (?, ?, ?, 'revert', 'ingest_event', ?, ?, ?, ?, ?)
            """,
            (audit_id, now, actor, target_event_id, target_event_id, canonical_json(before), canonical_json(after), reason),
        )
    return {"status": "reverted", "target_event_id": target_event_id, "affected_student_items": len(affected)}


def _rebuild_review_state(conn, student_id: str, item_id: str) -> None:
    current = conn.execute(
        "SELECT manual_override FROM review_state WHERE student_id=? AND item_id=?",
        (student_id, item_id),
    ).fetchone()
    if current and current["manual_override"]:
        return
    rows = list(
        conn.execute(
            """
            SELECT a.attempt_id, a.attempted_at, e.result
            FROM attempts a
            JOIN evaluations e ON e.attempt_id=a.attempt_id AND e.is_current=1
            WHERE a.student_id=? AND a.item_id=? AND a.record_status='active'
            ORDER BY a.attempted_at, a.attempt_id
            """,
            (student_id, item_id),
        )
    )
    if not rows:
        conn.execute("DELETE FROM review_state WHERE student_id=? AND item_id=? AND manual_override=0", (student_id, item_id))
        return
    lapses = sum(row["result"] in {"wrong", "partial"} for row in rows)
    consecutive = 0
    for row in reversed(rows):
        if row["result"] == "correct":
            break
        consecutive += 1
    latest = rows[-1]
    result = latest["result"]
    interval = 1 if result != "correct" else max(1, 2 ** max(0, len(rows) - lapses - 1))
    due = (_parse_time(latest["attempted_at"]) + timedelta(days=interval)).isoformat()
    state = "due" if result != "correct" else ("mastered" if len(rows) >= 3 and lapses == 0 else "learning")
    conn.execute(
        """
        INSERT INTO review_state(
          student_id,item_id,state,due_at,interval_days,repetitions,lapses,
          consecutive_errors,last_attempt_id,last_result,last_reviewed_at,
          scheduling_algorithm,algorithm_version,updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,'simple-v1','1',?)
        ON CONFLICT(student_id,item_id) DO UPDATE SET
          state=excluded.state,due_at=excluded.due_at,interval_days=excluded.interval_days,
          repetitions=excluded.repetitions,lapses=excluded.lapses,
          consecutive_errors=excluded.consecutive_errors,last_attempt_id=excluded.last_attempt_id,
          last_result=excluded.last_result,last_reviewed_at=excluded.last_reviewed_at,
          updated_at=excluded.updated_at
        """,
        (
            student_id,
            item_id,
            state,
            due,
            interval,
            len(rows),
            lapses,
            consecutive,
            latest["attempt_id"],
            result,
            latest["attempted_at"],
            utc_now(),
        ),
    )
