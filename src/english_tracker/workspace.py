from __future__ import annotations

import re
import sqlite3
from typing import Any

from .util import random_id, utc_now


SUPPORTED_LOCALES = [
    {"code": "zh-CN", "name": "中文", "name_en": "Chinese"},
    {"code": "en", "name": "English", "name_en": "English"},
]


def _require_student(conn: sqlite3.Connection, student_id: str) -> str:
    student_id = str(student_id or "").strip()
    row = conn.execute(
        "SELECT student_id FROM students WHERE student_id=? AND active=1",
        (student_id,),
    ).fetchone()
    if not row:
        raise ValueError(f"Unknown or inactive student_id: {student_id}")
    return student_id


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


def student_summaries(conn: sqlite3.Connection) -> dict[str, Any]:
    rows = [
        dict(row)
        for row in conn.execute(
            """
            SELECT s.student_id,s.display_name,s.timezone,s.target_retention,s.active,s.created_at,
                   COUNT(DISTINCT CASE WHEN ls.record_status='active' THEN ls.session_id END) session_count,
                   COUNT(DISTINCT CASE WHEN a.record_status='active' THEN a.attempt_id END) attempt_count,
                   MAX(CASE WHEN ls.record_status='active' THEN ls.started_at END) last_activity_at
            FROM students s
            LEFT JOIN learning_sessions ls ON ls.student_id=s.student_id
            LEFT JOIN attempts a ON a.student_id=s.student_id AND a.session_id=ls.session_id
            WHERE s.active=1
            GROUP BY s.student_id
            ORDER BY COALESCE(last_activity_at,s.created_at) DESC,s.student_id
            """
        )
    ]
    for row in rows:
        row["subjects"] = [
            dict(subject)
            for subject in conn.execute(
                """
                SELECT sub.subject_code,sub.name_en,sub.name_cn,sub.adapter_status
                FROM student_subjects ss
                JOIN subjects sub ON sub.subject_code=ss.subject_code AND sub.active=1
                WHERE ss.student_id=? AND ss.active=1
                ORDER BY sub.sort_order,sub.subject_code
                """,
                (row["student_id"],),
            )
        ]
    return {"count": len(rows), "items": rows, "generated_at": utc_now()}


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
    student_id = str(payload.get("student_id") or random_id("STU")).strip().upper()
    if not re.fullmatch(r"STU-[A-Z0-9-]{3,60}", student_id):
        raise ValueError("student_id must start with STU- and contain only letters, numbers, or hyphens")
    if conn.execute("SELECT 1 FROM students WHERE student_id=?", (student_id,)).fetchone():
        raise ValueError(f"student_id already exists: {student_id}")
    now = utc_now()
    with conn:
        conn.execute(
            """
            INSERT INTO students(student_id,display_name,timezone,target_retention,active,created_at,updated_at)
            VALUES (?,?,?,?,1,?,?)
            """,
            (student_id, display_name, timezone, float(payload.get("target_retention", 0.90)), now, now),
        )
        conn.executemany(
            """
            INSERT INTO student_subjects(student_id,subject_code,active,enrolled_at)
            VALUES (?,?,1,?)
            """,
            [(student_id, code, now) for code in subject_codes],
        )
    return {
        "student_id": student_id,
        "display_name": display_name,
        "timezone": timezone,
        "subject_codes": subject_codes,
        "created_at": now,
    }


def subject_overview(conn: sqlite3.Connection, student_id: str, subject_code: str) -> dict[str, Any]:
    student_id = _require_student(conn, student_id)
    subject = conn.execute(
        "SELECT subject_code,name_en,name_cn,adapter_status FROM subjects WHERE subject_code=? AND active=1",
        (subject_code,),
    ).fetchone()
    if not subject:
        raise ValueError(f"Unknown subject_code: {subject_code}")
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
