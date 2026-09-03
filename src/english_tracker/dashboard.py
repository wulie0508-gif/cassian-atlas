from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import UTC, date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .analytics import due_reviews, export_context
from .db import connect
from .library import library_summary
from .metrics import trend_report, weekly_report
from .orchestration import agent_dashboard
from .performance import session_performance
from .quality import run_quality_checks
from .util import utc_now
from .weights import evidence_weight, weighted_mastery_report
from .workspace import require_student_enrollment


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


def workflow_summary(conn, *, student_id: str | None = None) -> dict[str, Any]:
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
    if student_id:
        attempt_count = conn.execute(
            "SELECT COUNT(*) FROM attempts WHERE student_id=? AND record_status='active'",
            (student_id,),
        ).fetchone()[0]
    else:
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
        "learning_sessions": conn.execute(
            "SELECT COUNT(*) FROM learning_sessions WHERE student_id=? AND record_status='active'",
            (student_id,),
        ).fetchone()[0],
        "attempts": conn.execute(
            "SELECT COUNT(*) FROM attempts WHERE student_id=? AND record_status='active'",
            (student_id,),
        ).fetchone()[0],
        "review_tasks": conn.execute(
            "SELECT COUNT(*) FROM review_tasks WHERE student_id=? AND status='open'",
            (student_id,),
        ).fetchone()[0],
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
    workflow = workflow_summary(conn, student_id=student_id)
    agents = agent_dashboard(conn, student_id=student_id, limit=6)
    critical_issues = conn.execute(
        """
        SELECT COUNT(*)
        FROM attempts a
        WHERE a.student_id=? AND a.record_status='active'
          AND NOT EXISTS (
            SELECT 1 FROM evaluations e
            WHERE e.attempt_id=a.attempt_id AND e.is_current=1
          )
        """,
        (student_id,),
    ).fetchone()[0]

    next_actions = [
        {
            "priority": 1,
            "owner": "你只需",
            "title": "把需求交给任意一个项目对话",
            "detail": "中枢会判断任务并只调用必要的专用技能；Agent 自动写入统一数据库和运行台账，网站无需重复录入。",
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
        "student_id": student_id,
        "mode": "low_friction_v1",
        "headline": "告诉中枢要做什么，其余自动分发",
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
        "agent_system": {
            "router": agents["router"],
            "specialist_count": len(agents["capabilities"]),
            "active_run_count": agents["summary"]["active"],
            "needs_input_count": agents["summary"]["needs_input"],
            "recent_runs": agents["recent_runs"][:3],
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
            "definition": "首页只执行关键事实轻量检查；完整审计由 /api/overview 和 data check 提供。",
        },
        "detail_endpoints": {
            "performance": "/api/performance/sessions",
            "mastery": "/api/mastery",
            "weekly_report": "/api/reports/weekly",
            "dictation_plan": "/api/dictation/plan",
            "agent_dashboard": "/api/agent/dashboard",
        },
    }


_ROOT_KNOWLEDGE_CODES = {"vocabulary", "grammar", "reading", "translation", "writing"}


def _teacher_timezone(name: str):
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        if name == "Asia/Shanghai":
            return timezone(timedelta(hours=8))
        if name == "UTC":
            return UTC
        raise ValueError(f"Timezone data is unavailable for: {name}")


def _teacher_dt(value: str | None, default_tz) -> datetime | None:
    if value is None or not str(value).strip():
        return None
    raw = str(value).strip()
    if len(raw) == 10:
        return datetime.combine(date.fromisoformat(raw), time.min, tzinfo=default_tz)
    raw = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    result = datetime.fromisoformat(raw)
    if result.tzinfo is None:
        result = result.replace(tzinfo=default_tz)
    return result


def _teacher_as_of(value: str | None, default_tz) -> datetime:
    if not value:
        return datetime.now(default_tz)
    raw = str(value).strip()
    if len(raw) == 10:
        return datetime.combine(date.fromisoformat(raw), time.max, tzinfo=default_tz)
    parsed = _teacher_dt(raw, default_tz)
    if parsed is None:  # pragma: no cover - guarded by the non-empty branch
        raise ValueError("as_of is required")
    return parsed.astimezone(default_tz)


def _teacher_score(row: dict[str, Any]) -> float | None:
    if row.get("score") is not None and row.get("max_score") not in (None, 0):
        return max(0.0, min(1.0, float(row["score"]) / float(row["max_score"])))
    return {"correct": 1.0, "partial": 0.5, "wrong": 0.0}.get(row.get("result"))


def _teacher_session_kind(row: dict[str, Any]) -> tuple[str, str]:
    if row.get("assessment_kind"):
        kind = str(row["assessment_kind"])
        return kind, str(row.get("reporting_series") or kind)
    normalized = str(row.get("session_type") or "").strip().lower()
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


def _coverage(numerator: int, denominator: int) -> dict[str, Any]:
    return {
        "numerator": int(numerator),
        "denominator": int(denominator),
        "rate": round(numerator / denominator, 4) if denominator else None,
    }


def _teacher_attempt_rows(
    conn,
    *,
    student_id: str,
    subject_code: str,
    as_of_dt: datetime,
    student_tz,
) -> list[dict[str, Any]]:
    rows = [
        dict(row)
        for row in conn.execute(
            """
            SELECT a.attempt_id,a.item_id,a.session_id,a.attempted_at,
                   a.answer_capture_status,a.attempt_phase,a.validation_status,
                   a.student_answer,e.result,e.score,e.max_score,
                   ci.domain,ci.item_type,ci.difficulty_label,
                   ls.title,ls.session_type,ls.started_at,ls.ended_at,
                   sa.assessment_kind,sa.reporting_series,sa.delivery_mode,
                   sa.duration_seconds,sa.raw_score reported_score,
                   sa.max_score reported_max_score
            FROM attempts a
            JOIN evaluations e ON e.attempt_id=a.attempt_id AND e.is_current=1
            JOIN content_items ci ON ci.item_id=a.item_id AND ci.record_status='active'
            JOIN learning_sessions ls
              ON ls.session_id=a.session_id AND ls.student_id=a.student_id
             AND ls.record_status='active'
            LEFT JOIN session_assessments sa ON sa.session_id=ls.session_id
            WHERE a.student_id=? AND a.record_status='active' AND ci.subject_code=?
            ORDER BY a.attempted_at,a.attempt_id
            """,
            (student_id, subject_code),
        )
    ]
    return [
        row
        for row in rows
        if (_teacher_dt(row["attempted_at"], student_tz) or datetime.min.replace(tzinfo=UTC))
        <= as_of_dt
    ]


def _teacher_session_points(
    conn,
    rows: list[dict[str, Any]],
    *,
    weight_cache: dict[tuple[Any, ...], dict[str, Any]],
) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[row["session_id"]].append(row)

    def cached_weight(row: dict[str, Any], *, difficulty: str | None = None) -> dict[str, Any]:
        kind, _ = _teacher_session_kind(row)
        delivery = str(row.get("delivery_mode") or "unspecified")
        key = (
            kind,
            delivery,
            difficulty,
            row.get("validation_status"),
            row.get("answer_capture_status"),
        )
        if key not in weight_cache:
            weight_cache[key] = evidence_weight(
                conn,
                assessment_kind=kind,
                delivery_mode=delivery,
                difficulty=difficulty,
                verification_status=row.get("validation_status"),
                answer_capture_status=row.get("answer_capture_status"),
            )
        return weight_cache[key]

    points: list[dict[str, Any]] = []
    for group in grouped.values():
        head = group[0]
        kind, series = _teacher_session_kind(head)
        scored = [(row, _teacher_score(row)) for row in group]
        scored = [(row, score) for row, score in scored if score is not None]
        base_weight = cached_weight(
            {
                **head,
                "validation_status": None,
                "answer_capture_status": None,
            }
        )
        points.append(
            {
                "session_id": head["session_id"],
                "title": head["title"],
                "started_at": head["started_at"],
                "ended_at": head.get("ended_at"),
                "last_attempt_at": max(row["attempted_at"] for row in group),
                "assessment_kind": kind,
                "reporting_series": series,
                "delivery_mode": str(head.get("delivery_mode") or "unspecified"),
                "attempt_count": len(group),
                "distinct_item_count": len({row["item_id"] for row in group}),
                "scored_attempt_count": len(scored),
                "score": round(sum(score for _, score in scored), 3),
                "max_score": len(scored),
                "accuracy": round(sum(score for _, score in scored) / len(scored), 4)
                if scored
                else None,
                "blank_count": sum(
                    row["answer_capture_status"] == "captured_blank" for row in group
                ),
                "duration_seconds": head.get("duration_seconds"),
                "reported_score": head.get("reported_score"),
                "reported_max_score": head.get("reported_max_score"),
                "is_calibration_anchor": bool(base_weight["is_calibration_anchor"]),
            }
        )
    points.sort(key=lambda row: (row["last_attempt_at"], row["session_id"]))
    return points


def _teacher_knowledge_rows(
    conn,
    *,
    student_id: str,
    subject_code: str,
    as_of_dt: datetime,
    student_tz,
    weight_cache: dict[tuple[Any, ...], dict[str, Any]],
) -> tuple[list[dict[str, Any]], set[str], set[str], set[str]]:
    rows = [
        dict(row)
        for row in conn.execute(
            """
            SELECT a.attempt_id,a.item_id,a.attempted_at,a.answer_capture_status,
                   e.result,e.score,e.max_score,ci.difficulty_label,
                   COALESCE(sa.assessment_kind,'lesson') assessment_kind,
                   COALESCE(sa.delivery_mode,'unspecified') delivery_mode,
                   ikm.verification_status,kp.code,kp.name_cn,kp.domain
            FROM attempts a
            JOIN evaluations e ON e.attempt_id=a.attempt_id AND e.is_current=1
            JOIN content_items ci ON ci.item_id=a.item_id AND ci.record_status='active'
            JOIN learning_sessions ls
              ON ls.session_id=a.session_id AND ls.student_id=a.student_id
             AND ls.record_status='active'
            JOIN item_knowledge_map ikm
              ON ikm.item_id=a.item_id AND ikm.verification_status<>'rejected'
            JOIN knowledge_points kp ON kp.knowledge_point_id=ikm.knowledge_point_id
            LEFT JOIN session_assessments sa ON sa.session_id=a.session_id
            WHERE a.student_id=? AND a.record_status='active' AND ci.subject_code=?
            ORDER BY a.attempted_at,a.attempt_id,kp.code
            """,
            (student_id, subject_code),
        )
    ]
    rows = [
        row
        for row in rows
        if (_teacher_dt(row["attempted_at"], student_tz) or datetime.min.replace(tzinfo=UTC))
        <= as_of_dt
    ]
    any_mapping = {row["attempt_id"] for row in rows}
    confirmed_mapping = {
        row["attempt_id"]
        for row in rows
        if row["verification_status"] in {"verified", "source_checked"}
    }
    specific_mapping = {
        row["attempt_id"] for row in rows if row["code"] not in _ROOT_KNOWLEDGE_CODES
    }
    grouped: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for row in rows:
        if row["code"] in _ROOT_KNOWLEDGE_CODES:
            continue
        grouped[row["code"]].setdefault(row["attempt_id"], row)

    output: list[dict[str, Any]] = []
    for code, attempts_by_id in grouped.items():
        group = list(attempts_by_id.values())
        scored: list[tuple[dict[str, Any], float, float]] = []
        for row in group:
            score = _teacher_score(row)
            if score is None:
                continue
            key = (
                row["assessment_kind"],
                row["delivery_mode"],
                row.get("difficulty_label"),
                row.get("verification_status"),
                row.get("answer_capture_status"),
            )
            if key not in weight_cache:
                weight_cache[key] = evidence_weight(
                    conn,
                    assessment_kind=row["assessment_kind"],
                    delivery_mode=row["delivery_mode"],
                    difficulty=row.get("difficulty_label"),
                    verification_status=row.get("verification_status"),
                    answer_capture_status=row.get("answer_capture_status"),
                )
            scored.append((row, score, float(weight_cache[key]["evidence_weight"])))
        if not scored:
            continue
        attempt_count = len(scored)
        distinct_items = len({row["item_id"] for row, _, _ in scored})
        error_count = sum(score < 1 for _, score, _ in scored)
        weighted_sample = sum(weight for _, _, weight in scored)
        weighted_success = sum(score * weight for _, score, weight in scored)
        weighted_accuracy = weighted_success / weighted_sample if weighted_sample else None
        mastery_rate = (
            (1 + weighted_success) / (2 + weighted_sample) if weighted_sample else 0.5
        )
        if attempt_count < 2 or distinct_items < 2:
            confidence = "tentative" if error_count else "insufficient_evidence"
            confidence_cn = "暂定薄弱点" if error_count else "证据不足"
        elif weighted_sample < 4:
            confidence, confidence_cn = "emerging", "初步证据"
        elif weighted_sample < 8:
            confidence, confidence_cn = "moderate", "中等可信"
        else:
            confidence, confidence_cn = "established", "较高可信"
        output.append(
            {
                "knowledge_point": code,
                "name_cn": scored[0][0]["name_cn"],
                "domain": scored[0][0]["domain"],
                "mastery_rate": round(mastery_rate, 4),
                "weighted_accuracy": round(weighted_accuracy, 4)
                if weighted_accuracy is not None
                else None,
                "distinct_item_count": distinct_items,
                "attempt_count": attempt_count,
                "error_count": error_count,
                "weighted_sample_size": round(weighted_sample, 3),
                "confidence": confidence,
                "confidence_cn": confidence_cn,
                "latest_attempt_at": max(row["attempted_at"] for row, _, _ in scored),
                "mapping_evidence_status": "confirmed"
                if any(
                    row["verification_status"] in {"verified", "source_checked"}
                    for row, _, _ in scored
                )
                else "suggested_only",
            }
        )
    output.sort(
        key=lambda row: (
            row["mastery_rate"],
            -row["error_count"],
            -row["weighted_sample_size"],
            row["knowledge_point"],
        )
    )
    return output, any_mapping, confirmed_mapping, specific_mapping


def teacher_dashboard(
    conn,
    student_id: str,
    *,
    subject_code: str = "english",
    as_of: str | None = None,
) -> dict[str, Any]:
    """Return a compact, subject-scoped teacher decision surface.

    This endpoint intentionally recomputes only bounded learner aggregates.  It
    never invokes the full project quality audit or mixes unrelated assessment
    series into a single trend.
    """
    student_id, subject_code = require_student_enrollment(
        conn, student_id, subject_code
    )
    student = conn.execute(
        "SELECT timezone FROM students WHERE student_id=? AND active=1",
        (student_id,),
    ).fetchone()
    student_tz = _teacher_timezone(student["timezone"])
    as_of_dt = _teacher_as_of(as_of, student_tz)
    attempts = _teacher_attempt_rows(
        conn,
        student_id=student_id,
        subject_code=subject_code,
        as_of_dt=as_of_dt,
        student_tz=student_tz,
    )
    scored_attempts = [row for row in attempts if _teacher_score(row) is not None]
    weight_cache: dict[tuple[Any, ...], dict[str, Any]] = {}
    session_points = _teacher_session_points(
        conn, attempts, weight_cache=weight_cache
    )
    session_points.sort(
        key=lambda row: (
            _teacher_dt(row["last_attempt_at"], student_tz)
            or datetime.min.replace(tzinfo=UTC),
            row["session_id"],
        )
    )

    last_session = session_points[-1] if session_points else None
    last_scored = max(
        scored_attempts,
        key=lambda row: _teacher_dt(row["attempted_at"], student_tz)
        or datetime.min.replace(tzinfo=UTC),
        default=None,
    )
    last_scored_dt = (
        _teacher_dt(last_scored["attempted_at"], student_tz).astimezone(student_tz)
        if last_scored
        else None
    )
    days_since = (
        (as_of_dt.date() - last_scored_dt.date()).days if last_scored_dt else None
    )
    if days_since is None:
        freshness_status, freshness_label = "no_data", "暂无可评分作答"
    elif days_since < 0:
        freshness_status, freshness_label = "future_data", "存在晚于观察日的数据"
    elif days_since <= 3:
        freshness_status, freshness_label = "fresh", "数据较新"
    elif days_since <= 7:
        freshness_status, freshness_label = "aging", "建议近期补充作答"
    else:
        freshness_status, freshness_label = "stale", "数据已超过一周"
    freshness = {
        "last_session_with_attempts": (
            {
                key: last_session[key]
                for key in (
                    "session_id",
                    "title",
                    "started_at",
                    "last_attempt_at",
                    "attempt_count",
                    "scored_attempt_count",
                )
            }
            if last_session
            else None
        ),
        "last_scored_attempt": (
            {
                "attempt_id": last_scored["attempt_id"],
                "session_id": last_scored["session_id"],
                "item_id": last_scored["item_id"],
                "attempted_at": last_scored["attempted_at"],
                "result": last_scored["result"],
            }
            if last_scored
            else None
        ),
        "days_since_last_scored_attempt": days_since,
        "status": freshness_status,
        "status_cn": freshness_label,
        "thresholds_days": {"fresh_max": 3, "aging_max": 7},
    }

    comparable_candidates = [
        point for point in session_points if point["accuracy"] is not None
    ]
    if comparable_candidates:
        newest = comparable_candidates[-1]
        selected_kind = newest["assessment_kind"]
        selected_series = newest["reporting_series"]
        comparable_points = [
            point
            for point in comparable_candidates
            if point["assessment_kind"] == selected_kind
            and point["reporting_series"] == selected_series
        ][-6:]
        latest = comparable_points[-1]
        previous = comparable_points[-2] if len(comparable_points) >= 2 else None
        change = (
            round(latest["accuracy"] - previous["accuracy"], 4)
            if previous is not None
            else None
        )
        comparable_performance = {
            "series_key": f"{selected_kind}|{selected_series}",
            "assessment_kind": selected_kind,
            "reporting_series": selected_series,
            "points": comparable_points,
            "latest": latest,
            "previous": previous,
            "change": change,
            "sample": {
                "session_count": len(comparable_points),
                "attempt_count": sum(
                    point["attempt_count"] for point in comparable_points
                ),
                "scored_attempt_count": sum(
                    point["scored_attempt_count"] for point in comparable_points
                ),
            },
        }
    else:
        comparable_performance = {
            "series_key": None,
            "assessment_kind": None,
            "reporting_series": None,
            "points": [],
            "latest": None,
            "previous": None,
            "change": None,
            "sample": {
                "session_count": 0,
                "attempt_count": 0,
                "scored_attempt_count": 0,
            },
        }

    knowledge_rows, mapped_attempts, confirmed_attempts, specific_attempts = (
        _teacher_knowledge_rows(
            conn,
            student_id=student_id,
            subject_code=subject_code,
            as_of_dt=as_of_dt,
            student_tz=student_tz,
            weight_cache=weight_cache,
        )
    )
    teaching_priorities = [
        row
        for row in knowledge_rows
        if row["confidence"] in {"moderate", "established"}
        and row["error_count"] > 0
    ][:5]
    confirmation_signals = [
        row
        for row in knowledge_rows
        if row["confidence"] not in {"moderate", "established"}
        and row["error_count"] > 0
    ][:5]

    open_reviews = [
        dict(row)
        for row in conn.execute(
            """
            SELECT rt.review_task_id,rt.item_id,rt.due_at,rt.priority,ci.domain
            FROM review_tasks rt
            JOIN content_items ci ON ci.item_id=rt.item_id AND ci.record_status='active'
            WHERE rt.student_id=? AND rt.status='open' AND ci.subject_code=?
            """,
            (student_id, subject_code),
        )
    ]
    due_reviews = [
        row
        for row in open_reviews
        if (_teacher_dt(row["due_at"], student_tz) or datetime.max.replace(tzinfo=UTC))
        <= as_of_dt
    ]
    due_by_domain: dict[str, int] = defaultdict(int)
    for row in due_reviews:
        due_by_domain[str(row["domain"])] += 1
    earliest_due = min(
        due_reviews,
        key=lambda row: _teacher_dt(row["due_at"], student_tz)
        or datetime.max.replace(tzinfo=UTC),
        default=None,
    )

    history_by_item: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in scored_attempts:
        history_by_item[row["item_id"]].append(row)
    recovery_events: list[dict[str, Any]] = []
    for item_rows in history_by_item.values():
        item_rows.sort(
            key=lambda row: (
                _teacher_dt(row["attempted_at"], student_tz)
                or datetime.min.replace(tzinfo=UTC),
                row["attempt_id"],
            )
        )
        prior_error = False
        for row in item_rows:
            if row["attempt_phase"] == "review" and prior_error:
                attempted = _teacher_dt(row["attempted_at"], student_tz).astimezone(
                    student_tz
                )
                week_start = attempted.date() - timedelta(days=attempted.weekday())
                recovery_events.append(
                    {
                        "week_start": week_start,
                        "result": row["result"],
                    }
                )
            prior_error = prior_error or row["result"] in {"wrong", "partial"}
    recovery_by_week: dict[date, list[dict[str, Any]]] = defaultdict(list)
    for event in recovery_events:
        recovery_by_week[event["week_start"]].append(event)
    if recovery_by_week:
        recovery_week = max(recovery_by_week)
        recovery_group = recovery_by_week[recovery_week]
        recovered = sum(event["result"] == "correct" for event in recovery_group)
        latest_recovery = {
            "period": {
                "week_start": recovery_week.isoformat(),
                "week_end_exclusive": (recovery_week + timedelta(days=7)).isoformat(),
            },
            "eligible_retest_count": len(recovery_group),
            "recovered_count": recovered,
            "rate": round(recovered / len(recovery_group), 4),
        }
    else:
        latest_recovery = {
            "period": None,
            "eligible_retest_count": 0,
            "recovered_count": 0,
            "rate": None,
        }
    review_health = {
        "open_due_total": len(due_reviews),
        "open_due_by_domain": [
            {"domain": domain, "count": count}
            for domain, count in sorted(
                due_by_domain.items(), key=lambda item: (-item[1], item[0])
            )
        ],
        "earliest_due_at": earliest_due["due_at"] if earliest_due else None,
        "latest_retest_recovery": latest_recovery,
    }

    offline_weight = offline_success = practice_weight = practice_success = 0.0
    for row in scored_attempts:
        kind, _ = _teacher_session_kind(row)
        delivery = str(row.get("delivery_mode") or "unspecified")
        key = (
            kind,
            delivery,
            row.get("difficulty_label"),
            row.get("validation_status"),
            row.get("answer_capture_status"),
        )
        if key not in weight_cache:
            weight_cache[key] = evidence_weight(
                conn,
                assessment_kind=kind,
                delivery_mode=delivery,
                difficulty=row.get("difficulty_label"),
                verification_status=row.get("validation_status"),
                answer_capture_status=row.get("answer_capture_status"),
            )
        weight = weight_cache[key]
        normalized = _teacher_score(row)
        if weight["is_calibration_anchor"]:
            offline_weight += weight["evidence_weight"]
            offline_success += normalized * weight["evidence_weight"]
        else:
            practice_weight += weight["evidence_weight"]
            practice_success += normalized * weight["evidence_weight"]
    anchors = [point for point in session_points if point["is_calibration_anchor"]]
    latest_anchor = anchors[-1] if anchors else None
    offline_accuracy = offline_success / offline_weight if offline_weight else None
    practice_accuracy = practice_success / practice_weight if practice_weight else None
    calibration = {
        "status": "available" if anchors else "missing",
        "anchor_count": len(anchors),
        "latest_anchor": latest_anchor,
        "offline_accuracy": round(offline_accuracy, 4)
        if offline_accuracy is not None
        else None,
        "practice_accuracy": round(practice_accuracy, 4)
        if practice_accuracy is not None
        else None,
        "gap": round(offline_accuracy - practice_accuracy, 4)
        if offline_accuracy is not None and practice_accuracy is not None
        else None,
        "offline_weighted_sample": round(offline_weight, 3),
        "practice_weighted_sample": round(practice_weight, 3),
    }

    all_attempt_records = [
        dict(row)
        for row in conn.execute(
            """
            SELECT a.attempt_id,a.attempted_at,a.record_status
            FROM attempts a
            JOIN content_items ci ON ci.item_id=a.item_id
            WHERE a.student_id=? AND ci.subject_code=?
            """,
            (student_id, subject_code),
        )
    ]
    all_attempt_records = [
        row
        for row in all_attempt_records
        if (_teacher_dt(row["attempted_at"], student_tz) or datetime.min.replace(tzinfo=UTC))
        <= as_of_dt
    ]
    error_attempt_ids = {
        row["attempt_id"]
        for row in attempts
        if row["result"] in {"wrong", "partial"}
    }
    diagnosed_attempt_ids = {
        row[0]
        for row in conn.execute(
            """
            SELECT DISTINCT aem.attempt_id
            FROM attempt_error_map aem
            JOIN attempts a ON a.attempt_id=aem.attempt_id AND a.record_status='active'
            JOIN content_items ci ON ci.item_id=a.item_id AND ci.record_status='active'
            WHERE a.student_id=? AND ci.subject_code=? AND aem.record_status='active'
            """,
            (student_id, subject_code),
        )
        if row[0] in error_attempt_ids
    }
    timed_sessions = sum(
        point["duration_seconds"] is not None
        or bool(point.get("ended_at") and point.get("started_at"))
        for point in session_points
    )
    data_coverage = {
        "active_attempts": _coverage(len(attempts), len(all_attempt_records)),
        "scored_attempts": _coverage(len(scored_attempts), len(attempts)),
        "answer_capture": _coverage(
            sum(
                row["answer_capture_status"] in {"captured", "captured_blank"}
                for row in attempts
            ),
            len(attempts),
        ),
        "knowledge_map": _coverage(len(mapped_attempts), len(attempts)),
        "specific_knowledge_map": _coverage(len(specific_attempts), len(attempts)),
        "confirmed_knowledge_map": _coverage(
            len(confirmed_attempts), len(attempts)
        ),
        "error_diagnosis": _coverage(
            len(diagnosed_attempt_ids), len(error_attempt_ids)
        ),
        "timing": _coverage(timed_sessions, len(session_points)),
    }

    run_counts = {
        row["status"]: int(row["count"])
        for row in conn.execute(
            """
            SELECT status,COUNT(*) count FROM agent_runs
            WHERE student_id=? AND subject_code=? GROUP BY status
            """,
            (student_id, subject_code),
        )
    }
    generation_counts = {
        row["status"]: int(row["count"])
        for row in conn.execute(
            """
            SELECT status,COUNT(*) count FROM artifact_generation_runs
            WHERE student_id=? AND subject_code=? GROUP BY status
            """,
            (student_id, subject_code),
        )
    }
    latest_run_row = conn.execute(
        """
        SELECT run_id,title,status,primary_capability,summary,updated_at
        FROM agent_runs WHERE student_id=? AND subject_code=?
        ORDER BY updated_at DESC,run_id DESC LIMIT 1
        """,
        (student_id, subject_code),
    ).fetchone()
    agent_summary = {
        "active": run_counts.get("planned", 0) + run_counts.get("in_progress", 0),
        "needs_input": run_counts.get("needs_input", 0),
        "completed": run_counts.get("completed", 0),
        "failed": run_counts.get("failed", 0),
        "generation_active": generation_counts.get("planned", 0)
        + generation_counts.get("in_progress", 0),
        "generation_completed": generation_counts.get("completed", 0),
        "generation_failed": generation_counts.get("failed", 0),
        "latest_run": dict(latest_run_row) if latest_run_row else None,
    }

    if not scored_attempts:
        next_action = {
            "action": "collect_baseline",
            "title": "先完成一次可评分练习",
            "reason": "当前学科还没有可用于判断教学重点的逐题结果。",
            "codex_prompt": f"请为 {student_id} 的 {subject_code} 准备一次短时基础摸底，并在完成后记录逐题结果。",
        }
    elif teaching_priorities:
        priority = teaching_priorities[0]
        label = priority["name_cn"] or priority["knowledge_point"]
        due_note = (
            f"，同时有 {len(due_reviews)} 项到期复测"
            if due_reviews
            else ""
        )
        next_action = {
            "action": "teach_stable_weakness",
            "title": f"优先处理：{label}",
            "reason": (
                f"该知识点基于 {priority['distinct_item_count']} 道不同题、"
                f"{priority['attempt_count']} 次作答，当前加权正确率为 "
                f"{priority['weighted_accuracy'] * 100:.1f}%{due_note}。"
            ),
            "codex_prompt": (
                f"请为 {student_id} 的 {subject_code} 课准备一组围绕“{label}”的讲解与复测，"
                "优先使用已核验题目，并记录逐题结果。"
            ),
        }
    elif confirmation_signals:
        signal = confirmation_signals[0]
        label = signal["name_cn"] or signal["knowledge_point"]
        next_action = {
            "action": "confirm_tentative_signal",
            "title": f"先复测确认：{label}",
            "reason": (
                f"目前只有 {signal['distinct_item_count']} 道不同题、"
                f"{signal['attempt_count']} 次作答，证据不足以判定为稳定薄弱点。"
            ),
            "codex_prompt": (
                f"请为 {student_id} 的 {subject_code} 选择少量关于“{label}”的新题做诊断复测，"
                "不要把暂定信号直接当作已确认薄弱点。"
            ),
        }
    elif due_reviews:
        next_action = {
            "action": "clear_due_reviews",
            "title": "先清理到期复测",
            "reason": f"当前共有 {len(due_reviews)} 项到期但尚未完成的复测。",
            "codex_prompt": f"请为 {student_id} 的 {subject_code} 调取当前到期复测，按优先级组成一次短测并记录结果。",
        }
    elif not anchors:
        next_action = {
            "action": "collect_calibration",
            "title": "安排一次线下闭卷校准",
            "reason": "已有日常练习证据，但尚无受控环境成绩用于判断迁移效果。",
            "codex_prompt": f"请为 {student_id} 的 {subject_code} 准备一次线下闭卷校准测，保持题型和评分口径明确。",
        }
    else:
        next_action = {
            "action": "continue_and_observe",
            "title": "保持当前节奏并继续观察",
            "reason": "当前没有稳定薄弱点、到期复测或明显证据缺口。",
            "codex_prompt": f"请根据 {student_id} 的 {subject_code} 最新可比成绩准备下一次常规练习，并继续记录逐题结果。",
        }

    return {
        "generated_at": utc_now(),
        "as_of": as_of_dt.isoformat(),
        "student_id": student_id,
        "subject_code": subject_code,
        "freshness": freshness,
        "comparable_performance": comparable_performance,
        "teaching_priorities": teaching_priorities,
        "confirmation_signals": confirmation_signals,
        "review_health": review_health,
        "calibration": calibration,
        "recent_sessions": list(reversed(session_points))[:5],
        "data_coverage": data_coverage,
        "agent_summary": agent_summary,
        "next_action": next_action,
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
        "workflow": workflow_summary(conn, student_id=student_id),
        "quality": {
            "trust_status": quality["trust_status"],
            "checks_passed": quality["summary"]["passed"],
            "checks_total": quality["summary"]["total_checks"],
        },
    }


def context_for(conn, audience: str, *, student_id: str, question_bank: str | Path) -> dict[str, Any]:
    if audience in {"courseware", "dictation"}:
        result = export_context(conn, student_id, audience)
        result["system_notice"] = workflow_summary(
            conn, student_id=student_id
        )["system_notice"]
        result["question_bank"] = question_bank_summary(question_bank)["counts"]
        result["operating_mode"] = {
            "name": "low_friction_v1",
            "principle": "后台精确、前台减负。Agent 默认执行查询和写入，不要求用户在网站重复维护。",
            "user_default": "只向用户说明当前状态、唯一下一步和需要其确认的异常。",
            "home_summary": "/api/home",
        }
        result["web_endpoints"] = {
            "app_config": "/api/app-config",
            "students": "/api/students",
            "subject_overview": "/api/subject-overview?subject_code={subject_code}",
            "question_search": "/api/questions",
            "knowledge_search": "/api/knowledge/search",
            "material_search": "/api/library/search",
            "staged_question_search": "/api/library/candidates",
            "record_assessment": "/api/assessments",
            "record_session": "/api/sessions",
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
            "agent_route": "/api/agent/route",
            "agent_runs": "/api/agent/runs",
            "agent_dashboard": "/api/agent/dashboard",
        }
        result["agent_trigger_rules"] = [
            {
                "when": "任务同时涉及查询、诊断、选题、写入或看板同步时",
                "action": "先调用 agent_route；仅执行返回的 specialist steps，不要先加载整套工程上下文。",
            },
            {
                "when": "写入任何新题目或学习活动时",
                "action": "显式使用当前 student_id；非英语内容还要设置 item.subject_code，防止跨学生或跨学科串库。",
            },
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
            "agent_system": agent_dashboard(conn, student_id=student_id),
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
