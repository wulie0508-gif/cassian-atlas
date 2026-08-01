from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from .analytics import due_reviews, export_context
from .db import connect
from .library import library_summary
from .metrics import trend_report, weekly_report
from .quality import run_quality_checks
from .util import utc_now
from .weights import weighted_mastery_report


def _readonly_question_bank(path: str | Path) -> sqlite3.Connection:
    question_bank = Path(path).expanduser().resolve()
    if not question_bank.exists():
        raise ValueError(f"Question bank not found: {question_bank}")
    conn = connect(question_bank, readonly=True)
    conn.execute("PRAGMA query_only=ON")
    return conn


def question_bank_summary(question_bank: str | Path) -> dict[str, Any]:
    conn = _readonly_question_bank(question_bank)
    counts = dict(
        conn.execute(
            """
            SELECT
              COUNT(*) questions,
              SUM(verification_status IN ('verified','source_checked')) usable_questions,
              SUM(answer IS NOT NULL AND trim(answer)<>'') answers_available,
              SUM(explanation_raw IS NOT NULL AND trim(explanation_raw)<>'') explanations_available,
              COUNT(DISTINCT source_id) sources,
              COUNT(DISTINCT passage_id) FILTER (WHERE passage_id IS NOT NULL AND passage_id<>'') passages
            FROM questions
            """
        ).fetchone()
    )
    counts["teaching_methods"] = conn.execute("SELECT COUNT(*) FROM teaching_methods").fetchone()[0]
    counts["ocr_pages"] = conn.execute("SELECT COUNT(*) FROM textbook_pages").fetchone()[0]
    counts["review_queue"] = conn.execute("SELECT COUNT(*) FROM review_queue").fetchone()[0]
    distributions: dict[str, list[dict[str, Any]]] = {}
    dimensions = {
        "question_types": ("question_type", 20),
        "years": ("year", 30),
        "verification": ("verification_status", 20),
        "difficulty": ("difficulty", 20),
        "knowledge_points": ("primary_test_point", 30),
    }
    for key, (column, limit) in dimensions.items():
        distributions[key] = [
            dict(row)
            for row in conn.execute(
                f"""
                SELECT COALESCE(NULLIF(trim({column}),''),'未标注') label,COUNT(*) value
                FROM questions GROUP BY label ORDER BY value DESC,label LIMIT ?
                """,
                (limit,),
            )
        ]
    source_status = [
        dict(row)
        for row in conn.execute(
            """
            SELECT processing_status label,COUNT(*) value
            FROM sources GROUP BY processing_status ORDER BY value DESC
            """
        )
    ]
    conn.close()
    return {
        "generated_at": utc_now(),
        "source": str(Path(question_bank).resolve()),
        "counts": counts,
        "distributions": distributions,
        "source_status": source_status,
    }


def search_questions(
    question_bank: str | Path,
    *,
    query: str = "",
    question_type: str = "",
    verification_status: str = "",
    limit: int = 50,
) -> dict[str, Any]:
    conn = _readonly_question_bank(question_bank)
    clauses = ["1=1"]
    params: list[Any] = []
    if query:
        clauses.append(
            "(q.question_id LIKE ? OR q.stem LIKE ? OR q.answer LIKE ? OR q.explanation_raw LIKE ? OR q.primary_test_point LIKE ? OR q.secondary_test_points LIKE ?)"
        )
        params.extend([f"%{query}%"] * 6)
    if question_type:
        clauses.append("q.question_type=?")
        params.append(question_type)
    if verification_status:
        clauses.append("q.verification_status=?")
        params.append(verification_status)
    params.append(max(1, min(limit, 200)))
    rows = [
        dict(row)
        for row in conn.execute(
            f"""
            SELECT q.question_id,q.passage_id,q.year,q.exam_type,q.district_or_school,q.section,
                   q.question_type,q.original_number,q.stem,q.answer,q.primary_test_point,
                   q.secondary_test_points,q.difficulty,q.recommended_use,q.verification_status,
                   q.source_page,q.source_path
            FROM questions q WHERE {' AND '.join(clauses)}
            ORDER BY CASE q.verification_status WHEN 'verified' THEN 0 WHEN 'source_checked' THEN 1 ELSE 2 END,
                     q.year DESC,q.question_id LIMIT ?
            """,
            params,
        )
    ]
    total_params = params[:-1]
    total = conn.execute(f"SELECT COUNT(*) FROM questions q WHERE {' AND '.join(clauses)}", total_params).fetchone()[0]
    conn.close()
    return {"total": total, "count": len(rows), "items": rows}


def question_detail(question_bank: str | Path, question_id: str, learning_conn=None) -> dict[str, Any]:
    conn = _readonly_question_bank(question_bank)
    question = conn.execute("SELECT * FROM questions WHERE question_id=?", (question_id,)).fetchone()
    if not question:
        conn.close()
        raise ValueError(f"Unknown question: {question_id}")
    result: dict[str, Any] = dict(question)
    result["options"] = [dict(row) for row in conn.execute("SELECT * FROM options WHERE question_id=? ORDER BY option_label", (question_id,))]
    result["tags"] = [dict(row) for row in conn.execute("SELECT tag_name,tag_role FROM question_tag_map WHERE question_id=? ORDER BY tag_role,tag_name", (question_id,))]
    if result.get("passage_id"):
        passage = conn.execute("SELECT * FROM passages WHERE passage_id=?", (result["passage_id"],)).fetchone()
        result["passage"] = dict(passage) if passage else None
    conn.close()
    if learning_conn is not None:
        result["deep_knowledge"] = [
            dict(row)
            for row in learning_conn.execute(
                """
                SELECT kp.code,kp.name_cn,qm.role,qm.mapping_source,qm.confidence,
                       qm.verification_status,qm.rationale,qm.source_locator
                FROM question_deep_knowledge_map qm
                JOIN knowledge_points kp ON kp.knowledge_point_id=qm.knowledge_point_id
                WHERE qm.question_id=? ORDER BY
                  CASE qm.role WHEN 'primary' THEN 0 WHEN 'secondary' THEN 1 WHEN 'prerequisite' THEN 2 ELSE 3 END,
                  qm.confidence DESC,kp.code
                """,
                (question_id,),
            )
        ]
        result["enrichments"] = [
            dict(row)
            for row in learning_conn.execute(
                """
                SELECT enrichment_type,enrichment_key,content_json,mapping_source,confidence,
                       verification_status,rationale,updated_at
                FROM question_enrichments WHERE question_id=? ORDER BY enrichment_type
                """,
                (question_id,),
            )
        ]
    return result


def workflow_summary(conn) -> dict[str, Any]:
    channels = [dict(row) for row in conn.execute("SELECT * FROM workflow_channels ORDER BY channel_key")]
    work_items = [
        dict(row)
        for row in conn.execute(
            """
            SELECT *,CASE WHEN total_units>0 THEN ROUND(1.0*completed_units/total_units,4) ELSE NULL END progress
            FROM project_work_items ORDER BY
              CASE status WHEN 'blocked' THEN 0 WHEN 'in_progress' THEN 1 WHEN 'needs_review' THEN 2 WHEN 'planned' THEN 3 ELSE 4 END,
              area,title
            """
        )
    ]
    return {"channels": channels, "work_items": work_items}


def learning_summary(conn, student_id: str) -> dict[str, Any]:
    counts = {
        table: conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        for table in ("learning_sessions", "attempts", "review_tasks", "knowledge_points", "question_enrichments", "question_deep_knowledge_map")
    }
    due = due_reviews(conn, student_id, limit=20)
    mastery = weighted_mastery_report(conn, student_id)
    return {"counts": counts, "due_review_count": due["count"], "mastery": mastery}


def overview(
    conn,
    *,
    student_id: str,
    question_bank: str | Path,
    library_key: str = "english_library",
) -> dict[str, Any]:
    quality = run_quality_checks(conn)
    return {
        "generated_at": utc_now(),
        "student_id": student_id,
        "question_bank": question_bank_summary(question_bank),
        "learning": learning_summary(conn, student_id),
        "library": library_summary(conn, library_key=library_key),
        "workflow": workflow_summary(conn),
        "quality": {
            "trust_status": quality["trust_status"],
            "checks_passed": quality["summary"]["passed"],
            "checks_total": quality["summary"]["total_checks"],
        },
    }


def context_for(conn, audience: str, *, student_id: str, question_bank: str | Path) -> dict[str, Any]:
    if audience in {"courseware", "dictation"}:
        result = export_context(conn, student_id, audience)
        result["question_bank"] = question_bank_summary(question_bank)["counts"]
        result["web_endpoints"] = {
            "question_search": "/api/questions",
            "knowledge_search": "/api/knowledge/search",
            "material_search": "/api/library/search",
            "staged_question_search": "/api/library/candidates",
            "record_assessment": "/api/assessments",
            "question_knowledge": "/api/grammar/questions/{question_id}",
            "passage_coverage": "/api/grammar/passages/{passage_id}/coverage",
            "coverage_matrix": "/api/grammar/coverage-matrix?passage_id={passage_id}",
            "select_complete_passages": "/api/grammar/select-passages",
            "record_classroom_attempts": "/api/classroom/attempts",
            "weekly_report": "/api/reports/weekly",
            "trend_report": "/api/reports/trends",
        }
        if audience == "dictation":
            result["web_endpoints"].update(
                {
                    "dictation_plan": "/api/dictation/plan",
                    "dictation_results": "/api/dictation/results",
                    "ocr_producer_contract": "/api/contracts/dictation-ocr",
                }
            )
        return result
    if audience == "engineering":
        return {
            "generated_at": utc_now(),
            "workflow": workflow_summary(conn),
            "library": library_summary(conn),
            "quality": run_quality_checks(conn),
        }
    raise ValueError("audience must be engineering, courseware, or dictation")


def reports_bundle(conn, student_id: str, *, week_start: str | None, trend_start: str, trend_end: str) -> dict[str, Any]:
    return {
        "weekly": weekly_report(conn, student_id, week_start=week_start),
        "trends": trend_report(conn, student_id, start=trend_start, end=trend_end),
        "weighted_mastery": weighted_mastery_report(conn, student_id),
    }
