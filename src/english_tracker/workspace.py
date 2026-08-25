from __future__ import annotations

import re
import sqlite3
from datetime import date
from math import isfinite
from typing import Any

from .util import utc_now


SUPPORTED_LOCALES = [
    {"code": "zh-CN", "name": "中文", "name_en": "Chinese"},
    {"code": "en", "name": "English", "name_en": "English"},
]

PROFILE_FIELDS = (
    "grade_level",
    "exam_system",
    "target_exam_date",
    "target_score",
    "weekly_hours",
    "course_stage",
    "teacher_notes",
)

_PROFILE_TEXT_LIMITS = {
    "grade_level": 80,
    "exam_system": 120,
    "course_stage": 120,
    "teacher_notes": 4000,
}


def _normalize_student_id(student_id: Any) -> str:
    return str(student_id or "").strip().upper()


def _require_student(
    conn: sqlite3.Connection,
    student_id: str,
    *,
    active_only: bool = True,
) -> str:
    student_id = _normalize_student_id(student_id)
    active_clause = " AND active=1" if active_only else ""
    row = conn.execute(
        f"SELECT student_id FROM students WHERE student_id=?{active_clause}",
        (student_id,),
    ).fetchone()
    if not row:
        state = "Unknown or inactive" if active_only else "Unknown"
        raise ValueError(f"{state} student_id: {student_id}")
    return student_id


def require_student_enrollment(
    conn: sqlite3.Connection,
    student_id: str,
    subject_code: str,
) -> tuple[str, str]:
    """Validate an active learner, subject, and enrollment for routed work."""
    student_id = _require_student(conn, student_id)
    subject_code = str(subject_code or "").strip().lower()
    if not conn.execute(
        "SELECT 1 FROM subjects WHERE subject_code=? AND active=1",
        (subject_code,),
    ).fetchone():
        raise ValueError(f"Unknown or inactive subject_code: {subject_code}")
    if not conn.execute(
        """
        SELECT 1 FROM student_subjects
        WHERE student_id=? AND subject_code=? AND active=1
        """,
        (student_id, subject_code),
    ).fetchone():
        raise ValueError(f"Student {student_id} is not enrolled in subject: {subject_code}")
    return student_id, subject_code


def _optional_text(value: Any, field: str) -> str | None:
    if value is None:
        return None
    result = str(value).strip()
    if not result:
        return None
    limit = _PROFILE_TEXT_LIMITS[field]
    if len(result) > limit:
        raise ValueError(f"{field} must be {limit} characters or fewer")
    return result


def _optional_number(value: Any, field: str) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a non-negative number")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a non-negative number") from exc
    if not isfinite(result) or result < 0:
        raise ValueError(f"{field} must be a non-negative number")
    return result


def _profile_updates(payload: dict[str, Any], *, partial: bool) -> dict[str, Any]:
    nested = payload.get("profile")
    if nested is not None and not isinstance(nested, dict):
        raise ValueError("profile must be an object")
    source = dict(nested or {})
    for field in PROFILE_FIELDS:
        if field in payload:
            source[field] = payload[field]
    result: dict[str, Any] = {}
    for field in PROFILE_FIELDS:
        if partial and field not in source:
            continue
        value = source.get(field)
        if field in _PROFILE_TEXT_LIMITS:
            result[field] = _optional_text(value, field)
        elif field == "target_exam_date":
            if value is None or str(value).strip() == "":
                result[field] = None
            else:
                result[field] = str(value).strip()
                try:
                    date.fromisoformat(result[field])
                except ValueError as exc:
                    raise ValueError("target_exam_date must use YYYY-MM-DD") from exc
        else:
            result[field] = _optional_number(value, field)
    unknown = sorted(set(source) - set(PROFILE_FIELDS))
    if unknown:
        raise ValueError(f"Unknown profile fields: {', '.join(unknown)}")
    return result


def _student_result(conn: sqlite3.Connection, row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    result = dict(row)
    result["active"] = bool(result["active"])
    result["profile"] = {field: result.pop(field) for field in PROFILE_FIELDS}
    enrollments = [
        dict(enrollment)
        for enrollment in conn.execute(
            """
            SELECT sub.subject_code,sub.name_en,sub.name_cn,sub.adapter_status,
                   ss.active,ss.enrolled_at
            FROM student_subjects ss
            JOIN subjects sub ON sub.subject_code=ss.subject_code
            WHERE ss.student_id=?
            ORDER BY sub.sort_order,sub.subject_code
            """,
            (result["student_id"],),
        )
    ]
    for enrollment in enrollments:
        enrollment["active"] = bool(enrollment["active"])
    result["enrollments"] = enrollments
    # Preserve the original API shape consumed by the existing dashboard.
    result["subjects"] = [
        {key: value for key, value in enrollment.items() if key not in {"active", "enrolled_at"}}
        for enrollment in enrollments
        if enrollment["active"]
    ]
    return result


def app_config(conn: sqlite3.Connection) -> dict[str, Any]:
    subjects = [
        dict(row)
        for row in conn.execute(
            """
            SELECT subject_code,name_en,name_cn,adapter_status
            FROM subjects WHERE active=1 ORDER BY sort_order,subject_code
            """
        )
    ]
    return {
        "product": {
            "name": "OpenTutor Ledger",
            "tagline_en": "Local-first learning evidence for humans and agents.",
            "tagline_cn": "面向教师与 Agent 的本地学习证据系统。",
        },
        "locales": SUPPORTED_LOCALES,
        "subjects": subjects,
        "privacy": {
            "local_first": True,
            "repository_contains_learning_data": False,
            "repository_contains_question_bank": False,
        },
    }


def student_summaries(
    conn: sqlite3.Connection,
    *,
    include_inactive: bool = False,
) -> dict[str, Any]:
    where = "" if include_inactive else "WHERE s.active=1"
    rows = list(
        conn.execute(
            f"""
            SELECT s.student_id,s.display_name,s.timezone,s.target_retention,s.active,
                   s.grade_level,s.exam_system,s.target_exam_date,s.target_score,
                   s.weekly_hours,s.course_stage,s.teacher_notes,s.created_at,s.updated_at,
                   COUNT(DISTINCT CASE WHEN ls.record_status='active' THEN ls.session_id END) session_count,
                   COUNT(DISTINCT CASE WHEN a.record_status='active' THEN a.attempt_id END) attempt_count,
                   MAX(CASE WHEN ls.record_status='active' THEN ls.started_at END) last_activity_at
            FROM students s
            LEFT JOIN learning_sessions ls ON ls.student_id=s.student_id
            LEFT JOIN attempts a ON a.student_id=s.student_id AND a.session_id=ls.session_id
            {where}
            GROUP BY s.student_id
            ORDER BY s.active DESC,COALESCE(last_activity_at,s.created_at) DESC,s.student_id
            """
        )
    )
    items = [_student_result(conn, row) for row in rows]
    return {"count": len(items), "items": items, "generated_at": utc_now()}


def student_detail(
    conn: sqlite3.Connection,
    student_id: str,
    *,
    include_inactive: bool = True,
) -> dict[str, Any]:
    student_id = _require_student(conn, student_id, active_only=not include_inactive)
    row = conn.execute(
        """
        SELECT s.student_id,s.display_name,s.timezone,s.target_retention,s.active,
               s.grade_level,s.exam_system,s.target_exam_date,s.target_score,
               s.weekly_hours,s.course_stage,s.teacher_notes,s.created_at,s.updated_at,
               (SELECT COUNT(*) FROM learning_sessions ls
                WHERE ls.student_id=s.student_id AND ls.record_status='active') session_count,
               (SELECT COUNT(*) FROM attempts a
                WHERE a.student_id=s.student_id AND a.record_status='active') attempt_count,
               (SELECT MAX(ls.started_at) FROM learning_sessions ls
                WHERE ls.student_id=s.student_id AND ls.record_status='active') last_activity_at
        FROM students s WHERE s.student_id=?
        """,
        (student_id,),
    ).fetchone()
    return _student_result(conn, row)


def create_student(conn: sqlite3.Connection, payload: dict[str, Any]) -> dict[str, Any]:
    display_name = str(payload.get("display_name") or "").strip()
    if not display_name:
        raise ValueError("display_name is required")
    if len(display_name) > 80:
        raise ValueError("display_name must be 80 characters or fewer")
    timezone = str(payload.get("timezone") or "Asia/Shanghai").strip()
    if not re.fullmatch(r"[A-Za-z_]+(?:/[A-Za-z0-9_+.-]+)+|UTC", timezone):
        raise ValueError("timezone must be an IANA timezone name such as Asia/Shanghai")
    subject_codes = payload.get("subject_codes") or ["english"]
    if not isinstance(subject_codes, list) or not subject_codes:
        raise ValueError("subject_codes must be a non-empty array")
    subject_codes = list(dict.fromkeys(str(code).strip().lower() for code in subject_codes))
    placeholders = ",".join("?" for _ in subject_codes)
    known = {
        row[0]
        for row in conn.execute(
            f"SELECT subject_code FROM subjects WHERE active=1 AND subject_code IN ({placeholders})",
            subject_codes,
        )
    }
    unknown = [code for code in subject_codes if code not in known]
    if unknown:
        raise ValueError(f"Unknown subject_codes: {', '.join(unknown)}")
    student_id = str(payload.get("student_id") or "").strip().upper()
    if not student_id:
        raise ValueError("student_id is required")
    if not re.fullmatch(r"STU-[A-Z0-9-]{3,60}", student_id):
        raise ValueError("student_id must start with STU- and contain only letters, numbers, or hyphens")
    if conn.execute("SELECT 1 FROM students WHERE student_id=?", (student_id,)).fetchone():
        raise ValueError(f"student_id already exists: {student_id}")
    profile = _profile_updates(payload, partial=False)
    try:
        target_retention = float(payload.get("target_retention", 0.90))
    except (TypeError, ValueError) as exc:
        raise ValueError("target_retention must be between 0 and 1") from exc
    if not 0 < target_retention < 1:
        raise ValueError("target_retention must be between 0 and 1")
    now = utc_now()
    with conn:
        conn.execute(
            """
            INSERT INTO students(
              student_id,display_name,timezone,target_retention,active,created_at,updated_at,
              grade_level,exam_system,target_exam_date,target_score,weekly_hours,course_stage,teacher_notes
            ) VALUES (?,?,?,?,1,?,?,?,?,?,?,?,?,?)
            """,
            (
                student_id, display_name, timezone, target_retention, now, now,
                *(profile[field] for field in PROFILE_FIELDS),
            ),
        )
        conn.executemany(
            """
            INSERT INTO student_subjects(student_id,subject_code,active,enrolled_at)
            VALUES (?,?,1,?)
            """,
            [(student_id, code, now) for code in subject_codes],
        )
    result = student_detail(conn, student_id)
    result["subject_codes"] = [row["subject_code"] for row in result["subjects"]]
    result["status"] = "created"
    return result


def update_student(conn: sqlite3.Connection, student_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    student_id = _require_student(conn, student_id, active_only=False)
    updates: dict[str, Any] = {}
    if "display_name" in payload:
        display_name = str(payload.get("display_name") or "").strip()
        if not display_name:
            raise ValueError("display_name cannot be empty")
        if len(display_name) > 80:
            raise ValueError("display_name must be 80 characters or fewer")
        updates["display_name"] = display_name
    if "timezone" in payload:
        timezone = str(payload.get("timezone") or "").strip()
        if not re.fullmatch(r"[A-Za-z_]+(?:/[A-Za-z0-9_+.-]+)+|UTC", timezone):
            raise ValueError("timezone must be an IANA timezone name such as Asia/Shanghai")
        updates["timezone"] = timezone
    if "target_retention" in payload:
        try:
            target_retention = float(payload["target_retention"])
        except (TypeError, ValueError) as exc:
            raise ValueError("target_retention must be between 0 and 1") from exc
        if not 0 < target_retention < 1:
            raise ValueError("target_retention must be between 0 and 1")
        updates["target_retention"] = target_retention
    updates.update(_profile_updates(payload, partial=True))
    if not updates:
        raise ValueError("At least one student field must be provided")
    updates["updated_at"] = utc_now()
    assignments = ",".join(f"{field}=?" for field in updates)
    with conn:
        conn.execute(
            f"UPDATE students SET {assignments} WHERE student_id=?",
            [*updates.values(), student_id],
        )
    result = student_detail(conn, student_id)
    result["status"] = "updated"
    return result


def enroll_student(
    conn: sqlite3.Connection,
    student_id: str,
    subject_codes: list[str],
) -> dict[str, Any]:
    student_id = _require_student(conn, student_id)
    if not isinstance(subject_codes, list) or not subject_codes:
        raise ValueError("subject_codes must be a non-empty array")
    normalized = list(dict.fromkeys(str(code).strip().lower() for code in subject_codes))
    if any(not code for code in normalized):
        raise ValueError("subject_codes cannot contain empty values")
    placeholders = ",".join("?" for _ in normalized)
    known = {
        row[0]
        for row in conn.execute(
            f"SELECT subject_code FROM subjects WHERE active=1 AND subject_code IN ({placeholders})",
            normalized,
        )
    }
    unknown = [code for code in normalized if code not in known]
    if unknown:
        raise ValueError(f"Unknown subject_codes: {', '.join(unknown)}")
    now = utc_now()
    with conn:
        conn.executemany(
            """
            INSERT INTO student_subjects(student_id,subject_code,active,enrolled_at)
            VALUES (?,?,1,?)
            ON CONFLICT(student_id,subject_code) DO UPDATE SET
              active=1,
              enrolled_at=excluded.enrolled_at
            """,
            [(student_id, code, now) for code in normalized],
        )
        conn.execute("UPDATE students SET updated_at=? WHERE student_id=?", (now, student_id))
    result = student_detail(conn, student_id)
    result["status"] = "enrolled"
    result["enrolled_subject_codes"] = normalized
    return result


def deactivate_student(conn: sqlite3.Connection, student_id: str) -> dict[str, Any]:
    student_id = _require_student(conn, student_id, active_only=False)
    was_active = bool(
        conn.execute("SELECT active FROM students WHERE student_id=?", (student_id,)).fetchone()[0]
    )
    if was_active:
        with conn:
            conn.execute(
                "UPDATE students SET active=0,updated_at=? WHERE student_id=?",
                (utc_now(), student_id),
            )
    result = student_detail(conn, student_id)
    result["status"] = "deactivated" if was_active else "already_inactive"
    return result


def subject_overview(conn: sqlite3.Connection, student_id: str, subject_code: str) -> dict[str, Any]:
    student_id, subject_code = require_student_enrollment(conn, student_id, subject_code)
    subject = conn.execute(
        "SELECT subject_code,name_en,name_cn,adapter_status FROM subjects WHERE subject_code=? AND active=1",
        (subject_code,),
    ).fetchone()
    summary = dict(
        conn.execute(
            """
            SELECT COUNT(DISTINCT a.attempt_id) attempt_count,
                   COUNT(DISTINCT a.session_id) session_count,
                   COUNT(DISTINCT a.item_id) item_count,
                   SUM(CASE WHEN e.result='correct' THEN 1.0 WHEN e.result='partial' THEN 0.5 ELSE 0 END) score,
                   SUM(CASE WHEN e.result IN ('correct','partial','wrong') THEN 1 ELSE 0 END) scored_count,
                   MAX(a.attempted_at) last_activity_at
            FROM attempts a
            JOIN content_items ci ON ci.item_id=a.item_id AND ci.record_status='active'
            JOIN evaluations e ON e.attempt_id=a.attempt_id AND e.is_current=1
            WHERE a.student_id=? AND a.record_status='active' AND ci.subject_code=?
            """,
            (student_id, subject_code),
        ).fetchone()
    )
    scored = int(summary["scored_count"] or 0)
    summary["accuracy"] = round(float(summary["score"] or 0) / scored, 4) if scored else None
    summary["attempt_count"] = int(summary["attempt_count"] or 0)
    summary["session_count"] = int(summary["session_count"] or 0)
    summary["item_count"] = int(summary["item_count"] or 0)
    summary["scored_count"] = scored
    return {
        "student_id": student_id,
        "subject": dict(subject),
        "summary": summary,
        "capabilities": {
            "generic_learning_records": True,
            "generic_attempt_ingest": True,
            "specialized_adapter": subject["adapter_status"] == "ready",
            "english_question_bank": subject_code == "english",
        },
        "generated_at": utc_now(),
    }
