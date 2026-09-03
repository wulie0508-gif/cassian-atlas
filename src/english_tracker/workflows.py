from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any

from .ingest import import_attempt_diagnostics, import_attempts, import_session
from .util import payload_hash


def _iso_from_date(value: str | None) -> str:
    if not value:
        return datetime.now().astimezone().isoformat(timespec="seconds")
    text = str(value).strip()
    if "T" not in text:
        return text + "T09:00:00+08:00"
    return datetime.fromisoformat(text.replace("Z", "+00:00")).isoformat(timespec="seconds")


def _answer_equal(student: str, standard: str) -> bool:
    def normalize(value: str) -> str:
        return " ".join(value.casefold().strip().split())

    accepted = [part for part in standard.replace("；", ";").split(";") if part.strip()]
    return normalize(student) in {normalize(value) for value in accepted or [standard]}


def _stable_ids(kind: str, payload: dict[str, Any]) -> tuple[str, str, str]:
    digest = payload_hash(payload).upper()
    session_id = str(payload.get("session_id") or f"SES-{digest[:20]}").strip()
    event_id = str(payload.get("event_id") or f"EVT-{digest[20:40]}").strip()
    idempotency_key = str(
        payload.get("idempotency_key") or f"opentutor:{kind}:{digest.casefold()}:v1"
    ).strip()
    return session_id, event_id, idempotency_key


def record_assessment(
    conn: sqlite3.Connection,
    payload: dict[str, Any],
    *,
    backup_path: str | None = None,
) -> dict[str, Any]:
    student_id = str(payload.get("student_id") or "").strip()
    if not student_id:
        raise ValueError("student_id is required")
    session_id, event_id, idempotency_key = _stable_ids("assessment", payload)
    started_at = _iso_from_date(payload.get("date") or payload.get("started_at"))
    raw_score = float(payload["raw_score"]) if payload.get("raw_score") not in {None, ""} else None
    max_score = float(payload["max_score"]) if payload.get("max_score") not in {None, ""} else None
    assessment_kind = str(payload.get("assessment_kind") or "topic_quiz")
    source_thread = str(payload.get("source_thread") or "courseware")
    result = import_session(
        conn,
        {
            "event_id": event_id,
            "idempotency_key": idempotency_key,
            "source_thread": source_thread,
            "student_id": student_id,
            "session": {
                "session_id": session_id,
                "session_type": assessment_kind,
                "title": payload.get("title") or "Assessment",
                "started_at": started_at,
                "ended_at": payload.get("ended_at"),
                "timezone": payload.get("timezone") or "Asia/Shanghai",
                "note": payload.get("note"),
            },
            "assessment": {
                "assessment_kind": assessment_kind,
                "reporting_series": payload.get("reporting_series") or assessment_kind,
                "delivery_mode": payload.get("delivery_mode") or "offline_closed",
                "raw_score": raw_score,
                "max_score": max_score,
                "duration_seconds": int(payload["duration_seconds"])
                if payload.get("duration_seconds") not in {None, ""}
                else None,
                "blank_count": int(payload["blank_count"])
                if payload.get("blank_count") not in {None, ""}
                else None,
                "validation_status": payload.get("validation_status") or "verified",
            },
        },
        backup_path=backup_path,
    )
    return {
        "status": result["status"],
        "student_id": student_id,
        "session_id": session_id,
        "event_id": event_id,
        "idempotency_key": idempotency_key,
        "result": result,
    }


def record_reading_diagnostics(
    conn: sqlite3.Connection,
    payload: dict[str, Any],
    *,
    backup_path: str | None = None,
) -> dict[str, Any]:
    student_id = str(payload.get("student_id") or "").strip()
    if not student_id:
        raise ValueError("student_id is required")
    normalized = dict(payload)
    digest = payload_hash(normalized).upper()
    normalized.setdefault("event_id", f"EVT-{digest[:20]}")
    normalized.setdefault(
        "idempotency_key", f"opentutor:reading-diagnostics:{digest.casefold()}:v1"
    )
    normalized.setdefault("source_thread", "courseware")
    result = import_attempt_diagnostics(conn, normalized, backup_path=backup_path)
    return {
        "status": result["status"],
        "student_id": student_id,
        "event_id": normalized["event_id"],
        "idempotency_key": normalized["idempotency_key"],
        "result": result,
    }


def record_dictation(
    conn: sqlite3.Connection,
    payload: dict[str, Any],
    *,
    backup_path: str | None = None,
) -> dict[str, Any]:
    student_id = str(payload.get("student_id") or "").strip()
    if not student_id:
        raise ValueError("student_id is required")
    items = payload.get("items")
    if not isinstance(items, list) or not items:
        raise ValueError("items must be a non-empty array")

    session_id, session_event, base_idempotency = _stable_ids("dictation", payload)
    operation_digest = payload_hash(payload).upper()
    started_at = _iso_from_date(payload.get("date") or payload.get("started_at"))
    attempts: list[dict[str, Any]] = []
    correct = 0
    for index, item in enumerate(items, start=1):
        item_id = str(item.get("item_id") or "").strip()
        if not item_id:
            raise ValueError(f"dictation item {index} requires item_id")
        source = conn.execute(
            "SELECT answer_snapshot FROM content_items WHERE item_id=? AND record_status='active'",
            (item_id,),
        ).fetchone()
        if not source:
            raise ValueError(f"Unknown active content item: {item_id}")
        answer = item.get("student_answer")
        answer_text = "" if answer is None else str(answer)
        standard = source["answer_snapshot"] or ""
        is_correct = _answer_equal(answer_text, standard)
        correct += int(is_correct)
        attempts.append(
            {
                "event_id": str(
                    item.get("event_id")
                    or f"ATT-{payload_hash({'operation': operation_digest, 'index': index, 'item_id': item_id})[:20].upper()}"
                ),
                "item_id": item_id,
                "attempted_at": item.get("attempted_at") or started_at,
                "student_answer": answer_text,
                "standard_answer": standard,
                "answer_capture_status": "captured_blank" if answer_text == "" else "captured",
                "attempt_phase": item.get("attempt_phase") or "review",
                "response_mode": "active_recall",
                "validation_status": item.get("validation_status") or "verified",
                "evaluation": {
                    "result": "correct" if is_correct else "wrong",
                    "score": 1 if is_correct else 0,
                    "max_score": 1,
                    "evaluated_by": "local_exact_match",
                },
                "error_types": item.get("error_types") or [],
            }
        )

    source_thread = str(payload.get("source_thread") or "dictation")
    session_result = import_session(
        conn,
        {
            "event_id": session_event,
            "idempotency_key": base_idempotency + ":session",
            "source_thread": source_thread,
            "student_id": student_id,
            "session": {
                "session_id": session_id,
                "session_type": "dictation",
                "title": payload.get("title") or "Dictation",
                "started_at": started_at,
                "timezone": payload.get("timezone") or "Asia/Shanghai",
                "note": payload.get("note"),
            },
            "assessment": {
                "assessment_kind": "dictation",
                "reporting_series": payload.get("reporting_series") or "weekly_dictation",
                "delivery_mode": payload.get("delivery_mode") or "offline_closed",
                "raw_score": correct,
                "max_score": len(attempts),
                "blank_count": sum(
                    attempt["answer_capture_status"] == "captured_blank" for attempt in attempts
                ),
                "validation_status": payload.get("validation_status") or "verified",
            },
        },
        backup_path=backup_path,
    )
    attempts_result = import_attempts(
        conn,
        {
            "event_id": str(payload.get("attempts_event_id") or f"EVT-{operation_digest[40:60]}"),
            "idempotency_key": base_idempotency + ":attempts",
            "source_thread": source_thread,
            "student_id": student_id,
            "session_id": session_id,
            "attempts": attempts,
        },
        backup_path=backup_path,
    )
    return {
        "status": "created" if session_result["status"] == "applied" else session_result["status"],
        "student_id": student_id,
        "session_id": session_id,
        "correct": correct,
        "total": len(attempts),
        "session_result": session_result,
        "attempts_result": attempts_result,
    }
