from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from .analytics import due_reviews, export_context
from .db import connect
from .library import library_summary
from .metrics import trend_report, weekly_report
from .performance import session_performance
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
    attempt_count = conn.execute(
        "SELECT COUNT(*) FROM attempts WHERE record_status='active'"
    ).fetchone()[0]
    return {
        "system_notice": {
            "status": "completed",
            "migration_completed": True,
            "existing_attempt_count": attempt_count,
            "message": "旧错题库和真实作答已经迁入统一数据库；无需点击启动器来触发迁移或解析。",
            "launcher_purpose": "启动器只负责打开本地可视化网站并提供HTTP接口；数据库成果已经落盘。",
            "performance_definition": "每次课堂、语法填空、阅读、听写和作业的作答都是真实成绩证据。",
            "offline_data_scope": "当前仅缺少明确分类为正式线下闭卷整卷或双周混合测的高权重校准锚点，不是缺少真实成绩。",
        },
        "channels": channels,
        "work_items": work_items,
    }


def learning_summary(conn, student_id: str) -> dict[str, Any]:
    counts = {
        "learning_sessions": conn.execute("SELECT COUNT(*) FROM learning_sessions WHERE record_status='active'").fetchone()[0],
        "attempts": conn.execute("SELECT COUNT(*) FROM attempts WHERE record_status='active'").fetchone()[0],
        "review_tasks": conn.execute("SELECT COUNT(*) FROM review_tasks WHERE status='open'").fetchone()[0],
        "knowledge_points": conn.execute("SELECT COUNT(*) FROM knowledge_points WHERE active=1").fetchone()[0],
        "question_enrichments": conn.execute("SELECT COUNT(*) FROM question_enrichments WHERE verification_status<>'rejected'").fetchone()[0],
        "question_deep_knowledge_map": conn.execute("SELECT COUNT(*) FROM question_deep_knowledge_map WHERE verification_status<>'rejected'").fetchone()[0],
    }
    due = due_reviews(conn, student_id, limit=20)
    due_total = conn.execute(
        """
        SELECT COUNT(*)
        FROM review_tasks rt
        JOIN content_items ci ON ci.item_id=rt.item_id AND ci.record_status='active'
        WHERE rt.student_id=? AND rt.status='open' AND rt.due_at<=?
        """,
        (student_id, due["as_of"]),
    ).fetchone()[0]
    mastery = weighted_mastery_report(conn, student_id)
    return {
        "counts": counts,
        "due_review_count": due_total,
        "due_review_batch_size": due["count"],
        "mastery": mastery,
    }


def low_friction_summary(conn, student_id: str) -> dict[str, Any]:
    """Return the small, deterministic surface a learner or agent needs first.

    Detailed evidence remains available from the specialist endpoints.  This
    summary intentionally avoids asking the user to maintain duplicate forms.
    """
    performance = session_performance(conn, student_id, limit=500)
    sessions = performance["items"]
    attempts = sum(int(row["attempt_count"]) for row in sessions)
    scored = sum(int(row["scored_attempt_count"]) for row in sessions)
    score = sum(float(row["derived_score"]) for row in sessions)
    reading_attempts = sum(
        int(domain["attempt_count"])
        for row in sessions
        for domain in row["domains"]
        if domain["domain"] == "reading"
    )
    anchors = sum(bool(row["is_calibration_anchor"]) for row in sessions)
    vocabulary_due = due_reviews(conn, student_id, domain="vocabulary", limit=20)
    vocabulary_due_total = conn.execute(
        """
        SELECT COUNT(*)
        FROM review_tasks rt
        JOIN content_items ci ON ci.item_id=rt.item_id AND ci.record_status='active'
        WHERE rt.student_id=? AND rt.status='open' AND rt.due_at<=? AND ci.domain='vocabulary'
        """,
        (student_id, vocabulary_due["as_of"]),
    ).fetchone()[0]
    mastery = weighted_mastery_report(conn, student_id)
    workflow = workflow_summary(conn)
    critical_issues = conn.execute(
        """
        SELECT COUNT(*)
        FROM attempts a
        WHERE a.record_status='active'
          AND NOT EXISTS (
            SELECT 1 FROM evaluations e
            WHERE e.attempt_id=a.attempt_id AND e.is_current=1
          )
        """
    ).fetchone()[0]

    next_actions = [
        {
            "priority": 1,
            "owner": "你只需",
            "title": "把结果交给对应对话",
            "detail": "课堂与阅读交给课件对话，听写交给单词听写对话；Agent 会写入统一数据库，网站无需重复录入。",
        }
    ]
    if reading_attempts == 0:
        next_actions.append(
            {
                "priority": 2,
                "owner": "下次阅读后",
                "title": "把文章编号和逐题答案交给课件对话",
                "detail": "系统将自动保存得分、考点与可核验错因；没有原始答案时不会反推错因。",
            }
        )
    if anchors == 0:
        next_actions.append(
            {
                "priority": 3,
                "owner": "下次线下测后",
                "title": "把得分、满分和用时交给课件对话",
                "detail": "线下闭卷结果只作为高权重校准锚点，不会覆盖平时真实成绩。",
            }
        )

    return {
        "generated_at": utc_now(),
        "mode": "low_friction_v1",
        "headline": "后台已接通，平时不用维护网站",
        "current": {
            "session_count": len(sessions),
            "attempt_count": attempts,
            "scored_attempt_count": scored,
            "descriptive_accuracy": round(score / scored, 4) if scored else None,
            "weighted_accuracy": mastery["summary"]["weighted_accuracy"],
            "reading_attempt_count": reading_attempts,
            "calibration_anchor_count": anchors,
            "dictation_plan_size": vocabulary_due["count"],
            "vocabulary_due_total": vocabulary_due_total,
        },
        "automation": [
            {
                "key": channel["channel_key"],
                "name": channel["display_name"],
                "status": channel["status"],
                "does": channel["responsibility"],
                "context_endpoint": channel["context_endpoint"],
            }
            for channel in workflow["channels"]
        ],
        "next_actions": next_actions,
        "data_health": {
            "status": "ready" if critical_issues == 0 else "attention",
            "critical_issue_count": critical_issues,
            "definition": "首页只执行关键事实轻量检查；完整 20 项审计由 /api/overview 和 data check 提供。",
        },
        "detail_endpoints": {
            "performance": "/api/performance/sessions",
            "mastery": "/api/mastery",
            "weekly_report": "/api/reports/weekly",
            "dictation_plan": "/api/dictation/plan",
        },
    }


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
        result["system_notice"] = workflow_summary(conn)["system_notice"]
        result["question_bank"] = question_bank_summary(question_bank)["counts"]
        result["operating_mode"] = {
            "name": "low_friction_v1",
            "principle": "后台精确、前台减负。Agent 默认执行查询和写入，不要求用户在网站重复维护。",
            "user_default": "只向用户说明当前状态、唯一下一步和需要其确认的异常。",
            "home_summary": "/api/home",
        }
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
            "session_performance": "/api/performance/sessions?domain={optional_domain}",
            "reading_passage_performance": "/api/reading/passages/{passage_id}/performance?session_id={optional_session_id}",
            "reading_error_taxonomy": "/api/reading/error-types",
            "record_reading_diagnostics": "/api/reading/diagnostics",
            "weekly_report": "/api/reports/weekly",
            "trend_report": "/api/reports/trends",
        }
        result["agent_trigger_rules"] = [
            {
                "when": "用户提供课堂、阅读、听写或测试结果时",
                "action": "Agent 直接调用对应接口完成查询、批改或写入；不要把常规录入工作推回给用户操作网站。",
            },
            {
                "when": "完成一次课堂、语法填空、阅读、听写或其他练习后",
                "action": "调用 record_classroom_attempts 写入逐题作答；这些就是真实成绩，不要等线下测试。",
            },
            {
                "when": "阅读题作答完成或需要复盘时",
                "action": "先调用 reading_passage_performance 读取整篇考点、得分和已有错因；有原始答案时才可提交错因。",
            },
            {
                "when": "Agent 自动分析出阅读错因时",
                "action": "使用 record_reading_diagnostics，error_source=model_suggested 且 verification_status=suggested；不得自动升级。",
            },
            {
                "when": "answer_capture_status=not_captured",
                "action": "仅保留对错和样本证据，不根据标准答案反推学生错因。",
            },
            {
                "when": "生成课件、复习计划或选题前",
                "action": "先读 session_performance、weekly_report 和知识点掌握；重复统计不再由模型现算。",
            },
        ]
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
            "operating_mode": {
                "name": "low_friction_v1",
                "principle": "后台保留完整证据与审计，前台默认仅显示当前状态、下一步和自动化健康。",
                "home_summary": "/api/home",
            },
        }
    raise ValueError("audience must be engineering, courseware, or dictation")


def reports_bundle(conn, student_id: str, *, week_start: str | None, trend_start: str, trend_end: str) -> dict[str, Any]:
    return {
        "weekly": weekly_report(conn, student_id, week_start=week_start),
        "trends": trend_report(conn, student_id, start=trend_start, end=trend_end),
        "weighted_mastery": weighted_mastery_report(conn, student_id),
    }
