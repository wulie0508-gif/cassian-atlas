from __future__ import annotations

import sqlite3
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from .db import connect
from .util import utc_now
from .weights import evidence_weight


def _score(result: str, score: float | None, max_score: float | None) -> float | None:
    if score is not None and max_score not in (None, 0):
        return max(0.0, min(1.0, float(score) / float(max_score)))
    return {"correct": 1.0, "partial": 0.5, "wrong": 0.0}.get(result)


def _classify_session(session_type: str, assessment_kind: str | None, reporting_series: str | None) -> tuple[str, str]:
    if assessment_kind:
        return assessment_kind, reporting_series or assessment_kind
    normalized = (session_type or "").strip().lower()
    kind = {
        "class": "lesson",
        "lesson": "lesson",
        "legacy_activity": "lesson",
        "dictation": "dictation",
        "homework": "homework",
        "topic_quiz": "topic_quiz",
        "test": "other",
    }.get(normalized, "other")
    return kind, f"classroom_{normalized or 'activity'}"


def session_performance(conn, student_id: str, *, domain: str | None = None, limit: int = 100) -> dict[str, Any]:
    clauses = ["ls.student_id=?", "ls.record_status='active'", "a.record_status='active'"]
    params: list[Any] = [student_id]
    if domain:
        clauses.append("ci.domain=?")
        params.append(domain)
    rows = [
        dict(row)
        for row in conn.execute(
            f"""
            SELECT ls.session_id,ls.title,ls.session_type,ls.started_at,ls.ended_at,
                   sa.assessment_kind,sa.reporting_series,sa.delivery_mode,
                   sa.raw_score reported_raw_score,sa.max_score reported_max_score,
                   sa.duration_seconds,sa.blank_count reported_blank_count,
                   a.attempt_id,a.item_id,a.answer_capture_status,a.attempt_phase,
                   a.validation_status,a.attempted_at,
                   ci.domain,ci.item_type,ci.difficulty_label,
                   e.result,e.score,e.max_score
            FROM learning_sessions ls
            JOIN attempts a ON a.session_id=ls.session_id
            JOIN content_items ci ON ci.item_id=a.item_id AND ci.record_status='active'
            JOIN evaluations e ON e.attempt_id=a.attempt_id AND e.is_current=1
            LEFT JOIN session_assessments sa ON sa.session_id=ls.session_id
            WHERE {' AND '.join(clauses)}
            ORDER BY ls.started_at DESC,a.attempted_at,a.attempt_id
            """,
            params,
        )
    ]
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[row["session_id"]].append(row)

    items: list[dict[str, Any]] = []
    for group in grouped.values():
        head = group[0]
        kind, series = _classify_session(head["session_type"], head["assessment_kind"], head["reporting_series"])
        delivery = head["delivery_mode"] or "unspecified"
        scores: list[float] = []
        weighted_success = 0.0
        weighted_sample = 0.0
        result_counts = Counter()
        domains = Counter()
        for row in group:
            result_counts[row["result"]] += 1
            domains[row["domain"]] += 1
            normalized = _score(row["result"], row["score"], row["max_score"])
            if normalized is None:
                continue
            scores.append(normalized)
            weight = evidence_weight(
                conn,
                assessment_kind=kind,
                delivery_mode=delivery,
                difficulty=row["difficulty_label"],
                verification_status=row["validation_status"],
                answer_capture_status=row["answer_capture_status"],
            )
            weighted_sample += weight["evidence_weight"]
            weighted_success += normalized * weight["evidence_weight"]
        base_weight = evidence_weight(
            conn,
            assessment_kind=kind,
            delivery_mode=delivery,
            difficulty=None,
            verification_status=None,
            answer_capture_status=None,
        )
        items.append(
            {
                "session_id": head["session_id"],
                "title": head["title"],
                "started_at": head["started_at"],
                "ended_at": head["ended_at"],
                "assessment_kind": kind,
                "reporting_series": series,
                "delivery_mode": delivery,
                "is_real_performance_evidence": True,
                "is_calibration_anchor": base_weight["is_calibration_anchor"],
                "assessment_weight": base_weight["assessment_weight"],
                "attempt_count": len(group),
                "distinct_item_count": len({row["item_id"] for row in group}),
                "scored_attempt_count": len(scores),
                "derived_score": round(sum(scores), 3),
                "derived_max_score": len(scores),
                "accuracy": round(sum(scores) / len(scores), 4) if scores else None,
                "weighted_accuracy": round(weighted_success / weighted_sample, 4) if weighted_sample else None,
                "weighted_sample_size": round(weighted_sample, 3),
                "correct_count": result_counts["correct"],
                "partial_count": result_counts["partial"],
                "wrong_count": result_counts["wrong"],
                "needs_check_count": result_counts["needs_check"],
                "blank_count": sum(row["answer_capture_status"] == "captured_blank" for row in group),
                "not_captured_count": sum(row["answer_capture_status"] == "not_captured" for row in group),
                "domains": [{"domain": key, "attempt_count": value} for key, value in domains.most_common()],
                "reported_score": head["reported_raw_score"],
                "reported_max_score": head["reported_max_score"],
                "duration_seconds": head["duration_seconds"],
                "score_origin": "attempt_detail",
                "evidence_note": "课堂与平时作答都是真实成绩证据；线下闭卷整卷或混合测仅作为高权重校准锚点。",
            }
        )
    items.sort(key=lambda row: (row["started_at"], row["session_id"]), reverse=True)
    limited = items[: max(1, min(int(limit), 500))]
    return {
        "student_id": student_id,
        "domain": domain,
        "generated_at": utc_now(),
        "count": len(limited),
        "total": len(items),
        "definition": "Every active attempt with a current evaluation is real performance evidence. Offline closed tests receive higher calibration weight, but do not define whether a score is real.",
        "items": limited,
    }


def _readonly_question_bank(path: str | Path) -> sqlite3.Connection:
    target = Path(path).expanduser().resolve()
    if not target.exists():
        raise ValueError(f"Question bank not found: {target}")
    conn = connect(target, readonly=True)
    conn.execute("PRAGMA query_only=ON")
    return conn


def reading_error_taxonomy(conn) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute(
            """
            SELECT code,label_cn,label_en,description
            FROM error_types
            WHERE active=1 AND (code='reading_error' OR parent_id='ERR-READ')
            ORDER BY CASE code WHEN 'reading_error' THEN 0 ELSE 1 END,label_cn
            """
        )
    ]


def reading_passage_performance(
    conn,
    question_bank: str | Path,
    student_id: str,
    passage_id: str,
    *,
    session_id: str | None = None,
    similar_limit: int = 12,
) -> dict[str, Any]:
    qbank = _readonly_question_bank(question_bank)
    passage_row = qbank.execute("SELECT * FROM passages WHERE passage_id=?", (passage_id,)).fetchone()
    if not passage_row:
        qbank.close()
        raise ValueError(f"Unknown passage: {passage_id}")
    question_rows = [
        dict(row)
        for row in qbank.execute(
            """
            SELECT question_id,passage_id,question_type,original_number,stem,answer,
                   explanation_raw,primary_test_point,secondary_test_points,difficulty,
                   verification_status,source_path,source_page,source_ordinal
            FROM questions WHERE passage_id=?
            ORDER BY COALESCE(source_ordinal,999999),question_id
            """,
            (passage_id,),
        )
    ]
    question_ids = [row["question_id"] for row in question_rows]
    refs: dict[str, str] = {}
    if question_ids:
        placeholders = ",".join("?" for _ in question_ids)
        refs = {
            row["external_id"]: row["item_id"]
            for row in conn.execute(
                f"""
                SELECT external_id,item_id FROM external_references
                WHERE namespace='shanghai_question_bank' AND reference_type='question_id'
                  AND external_id IN ({placeholders})
                """,
                question_ids,
            )
        }

    attempt_rows: list[dict[str, Any]] = []
    if refs:
        item_ids = sorted(set(refs.values()))
        placeholders = ",".join("?" for _ in item_ids)
        params: list[Any] = [student_id, *item_ids]
        session_clause = ""
        if session_id:
            session_clause = " AND a.session_id=?"
            params.append(session_id)
        attempt_rows = [
            dict(row)
            for row in conn.execute(
                f"""
                SELECT a.attempt_id,a.item_id,a.session_id,a.attempted_at,a.student_answer,
                       a.standard_answer_snapshot,a.answer_capture_status,a.attempt_phase,
                       a.validation_status,a.teacher_note,
                       e.result,e.score,e.max_score,e.evaluated_by,
                       ls.title session_title,ls.started_at session_started_at,ls.session_type,
                       sa.assessment_kind,sa.reporting_series,sa.delivery_mode
                FROM attempts a
                JOIN evaluations e ON e.attempt_id=a.attempt_id AND e.is_current=1
                JOIN learning_sessions ls ON ls.session_id=a.session_id AND ls.record_status='active'
                LEFT JOIN session_assessments sa ON sa.session_id=a.session_id
                WHERE a.student_id=? AND a.item_id IN ({placeholders})
                  AND a.record_status='active'{session_clause}
                ORDER BY a.attempted_at,a.attempt_id
                """,
                params,
            )
        ]

    item_to_question = {item_id: question_id for question_id, item_id in refs.items()}
    errors: dict[str, list[dict[str, Any]]] = defaultdict(list)
    attempt_ids = [row["attempt_id"] for row in attempt_rows]
    if attempt_ids:
        placeholders = ",".join("?" for _ in attempt_ids)
        for row in conn.execute(
            f"""
            SELECT aem.attempt_id,et.code,et.label_cn,aem.raw_error_type,aem.confidence,
                   aem.error_source,aem.verification_status,aem.rationale,aem.note
            FROM attempt_error_map aem
            JOIN error_types et ON et.error_type_id=aem.error_type_id
            WHERE aem.record_status='active' AND aem.attempt_id IN ({placeholders})
            ORDER BY aem.attempt_id,et.code
            """,
            attempt_ids,
        ):
            errors[row["attempt_id"]].append(dict(row))

    knowledge: dict[str, list[dict[str, Any]]] = defaultdict(list)
    if question_ids:
        placeholders = ",".join("?" for _ in question_ids)
        for row in conn.execute(
            f"""
            SELECT qm.question_id,kp.code,kp.name_cn,qm.role,qm.mapping_source,
                   qm.confidence,qm.verification_status,qm.rationale
            FROM question_deep_knowledge_map qm
            JOIN knowledge_points kp ON kp.knowledge_point_id=qm.knowledge_point_id
            WHERE qm.question_id IN ({placeholders}) AND qm.verification_status<>'rejected'
            ORDER BY qm.question_id,
              CASE qm.role WHEN 'primary' THEN 0 WHEN 'secondary' THEN 1 WHEN 'prerequisite' THEN 2 ELSE 3 END,
              qm.confidence DESC
            """,
            question_ids,
        ):
            knowledge[row["question_id"]].append(dict(row))

    attempts_by_question: dict[str, list[dict[str, Any]]] = defaultdict(list)
    scored_values: list[float] = []
    result_counts = Counter()
    for row in attempt_rows:
        question_id = item_to_question[row["item_id"]]
        normalized = _score(row["result"], row["score"], row["max_score"])
        if normalized is not None:
            scored_values.append(normalized)
        result_counts[row["result"]] += 1
        causes = errors[row["attempt_id"]]
        if row["result"] == "correct":
            diagnostic_status = "not_applicable"
        elif row["answer_capture_status"] == "not_captured":
            diagnostic_status = "blocked_not_captured"
        elif causes:
            diagnostic_status = "recorded"
        else:
            diagnostic_status = "pending_diagnosis"
        attempts_by_question[question_id].append(
            {
                **row,
                "normalized_score": normalized,
                "error_causes": causes,
                "diagnostic_status": diagnostic_status,
            }
        )

    question_items = []
    for row in question_rows:
        row_attempts = attempts_by_question[row["question_id"]]
        question_items.append(
            {
                **row,
                "knowledge_points": knowledge[row["question_id"]],
                "attempts": row_attempts,
                "attempt_count": len(row_attempts),
                "latest_result": row_attempts[-1]["result"] if row_attempts else None,
            }
        )

    wrong_question_ids = {
        item_to_question[row["item_id"]]
        for row in attempt_rows
        if row["result"] in {"wrong", "partial"}
    }
    target_points = sorted(
        {
            row["primary_test_point"].strip()
            for row in question_rows
            if row["question_id"] in wrong_question_ids and row.get("primary_test_point") and row["primary_test_point"].strip()
        }
    )
    similar_questions: list[dict[str, Any]] = []
    if target_points and similar_limit > 0:
        placeholders = ",".join("?" for _ in target_points)
        similar_questions = [
            dict(row)
            for row in qbank.execute(
                f"""
                SELECT question_id,passage_id,question_type,original_number,stem,
                       primary_test_point,difficulty,verification_status,year,district_or_school
                FROM questions
                WHERE primary_test_point IN ({placeholders})
                  AND passage_id<>?
                  AND verification_status IN ('verified','source_checked')
                ORDER BY CASE verification_status WHEN 'verified' THEN 0 ELSE 1 END,
                         year DESC,question_id
                LIMIT ?
                """,
                (*target_points, passage_id, max(1, min(int(similar_limit), 50))),
            )
        ]
    qbank.close()
    attempted_questions = len({item_to_question[row["item_id"]] for row in attempt_rows})
    return {
        "student_id": student_id,
        "passage": dict(passage_row),
        "session_id": session_id,
        "generated_at": utc_now(),
        "summary": {
            "question_count": len(question_rows),
            "attempted_question_count": attempted_questions,
            "attempt_count": len(attempt_rows),
            "scored_attempt_count": len(scored_values),
            "correct_count": result_counts["correct"],
            "partial_count": result_counts["partial"],
            "wrong_count": result_counts["wrong"],
            "accuracy": round(sum(scored_values) / len(scored_values), 4) if scored_values else None,
            "blank_count": sum(row["answer_capture_status"] == "captured_blank" for row in attempt_rows),
            "not_captured_count": sum(row["answer_capture_status"] == "not_captured" for row in attempt_rows),
            "pending_diagnosis_count": sum(
                attempt["diagnostic_status"] == "pending_diagnosis"
                for attempts in attempts_by_question.values()
                for attempt in attempts
            ),
        },
        "questions": question_items,
        "similar_questions": similar_questions,
        "target_test_points": target_points,
        "allowed_error_types": reading_error_taxonomy(conn),
        "evidence_boundary": "Question knowledge points describe what was tested. Attempt error causes describe why the student was wrong. They are stored separately and never inferred when answer_capture_status=not_captured.",
    }
