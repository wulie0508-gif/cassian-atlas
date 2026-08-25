from __future__ import annotations

import json
import sqlite3
from typing import Any

from .util import canonical_json, payload_hash, random_id, utc_now


def _require_enrollment(
    conn: sqlite3.Connection,
    student_id: str,
    subject_code: str,
) -> None:
    row = conn.execute(
        """
        SELECT 1
        FROM student_subjects ss
        JOIN students s ON s.student_id=ss.student_id AND s.active=1
        JOIN subjects sub ON sub.subject_code=ss.subject_code AND sub.active=1
        WHERE ss.student_id=? AND ss.subject_code=? AND ss.active=1
        """,
        (student_id, subject_code),
    ).fetchone()
    if not row:
        raise ValueError(
            f"Student {student_id} is not actively enrolled in subject {subject_code}"
        )


def start_generation(conn: sqlite3.Connection, payload: dict[str, Any]) -> dict[str, Any]:
    student_id = str(payload.get("student_id") or "").strip()
    subject_code = str(payload.get("subject_code") or "english").strip().lower()
    title = str(payload.get("title") or "").strip()
    artifact_type = str(payload.get("artifact_type") or "courseware").strip()
    snapshot = payload.get("source_snapshot")
    if not student_id or not title or not isinstance(snapshot, dict):
        raise ValueError("student_id, title, and object source_snapshot are required")
    _require_enrollment(conn, student_id, subject_code)

    snapshot_json = canonical_json(snapshot)
    snapshot_sha = payload_hash(snapshot)
    request_hash = payload_hash(
        {
            "student_id": student_id,
            "subject_code": subject_code,
            "title": title,
            "artifact_type": artifact_type,
            "source_snapshot_sha256": snapshot_sha,
            "skill_name": payload.get("skill_name"),
            "skill_version": payload.get("skill_version"),
            "prompt_version": payload.get("prompt_version"),
            "model_name": payload.get("model_name"),
        }
    )
    idempotency_key = str(
        payload.get("idempotency_key") or f"opentutor:generation:{request_hash}:v1"
    ).strip()
    existing = conn.execute(
        "SELECT * FROM artifact_generation_runs WHERE idempotency_key=?",
        (idempotency_key,),
    ).fetchone()
    if existing:
        if existing["student_id"] != student_id:
            raise ValueError("idempotency_key already belongs to another student")
        if (
            existing["source_snapshot_sha256"] != snapshot_sha
            or existing["subject_code"] != subject_code
            or existing["artifact_type"] != artifact_type
            or existing["title"] != title
            or existing["skill_name"] != payload.get("skill_name")
            or existing["skill_version"] != payload.get("skill_version")
            or existing["prompt_version"] != payload.get("prompt_version")
            or existing["model_name"] != payload.get("model_name")
        ):
            raise ValueError("idempotency_key already belongs to a different generation request")
        return {"status": "duplicate", "generation": _generation_dict(existing)}

    generation_id = str(payload.get("generation_id") or random_id("GEN")).strip()
    now = utc_now()
    with conn:
        conn.execute(
            """
            INSERT INTO artifact_generation_runs(
              generation_id,idempotency_key,student_id,subject_code,artifact_type,title,status,
              source_snapshot_json,source_snapshot_sha256,skill_name,skill_version,prompt_version,
              model_name,created_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                generation_id,
                idempotency_key,
                student_id,
                subject_code,
                artifact_type,
                title,
                "planned",
                snapshot_json,
                snapshot_sha,
                payload.get("skill_name"),
                payload.get("skill_version"),
                payload.get("prompt_version"),
                payload.get("model_name"),
                now,
                now,
            ),
        )
    return {
        "status": "created",
        "generation": generation_detail(conn, generation_id, student_id=student_id),
    }


def update_generation(
    conn: sqlite3.Connection,
    generation_id: str,
    payload: dict[str, Any],
    *,
    student_id: str,
) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM artifact_generation_runs WHERE generation_id=? AND student_id=?",
        (generation_id, student_id),
    ).fetchone()
    if not row:
        raise ValueError(f"Unknown generation_id for student {student_id}: {generation_id}")
    status = str(payload.get("status") or "").strip()
    if not status:
        raise ValueError("status is required")
    if status not in {"in_progress", "completed", "failed", "cancelled"}:
        raise ValueError("status must be in_progress, completed, failed, or cancelled")
    if row["status"] == status and all(
        payload.get(key) in {None, row[key]}
        for key in ("output_artifact_id", "output_path", "output_sha256", "summary")
    ):
        return {"status": "duplicate", "generation": _generation_dict(row)}
    if row["status"] in {"completed", "failed", "cancelled"}:
        raise ValueError(f"Generation is already terminal: {row['status']}")
    if row["status"] == status and all(
        payload.get(key) in {None, row[key]}
        for key in (
            "output_artifact_id",
            "output_path",
            "output_sha256",
            "summary",
        )
    ):
        return {"status": "duplicate", "generation": _generation_dict(row)}
    supplied_snapshot_sha = str(payload.get("source_snapshot_sha256") or "").strip()
    if supplied_snapshot_sha and supplied_snapshot_sha != row["source_snapshot_sha256"]:
        raise ValueError("source snapshot changed during generation")
    if status == "completed":
        output_path = str(payload.get("output_path") or row["output_path"] or "").strip()
        output_sha256 = str(payload.get("output_sha256") or row["output_sha256"] or "").strip()
        if not output_path or not output_sha256:
            raise ValueError("completed generation requires output_path and output_sha256")
    output_artifact_id = payload.get("output_artifact_id")
    if output_artifact_id:
        artifact = conn.execute(
            "SELECT student_id FROM artifacts WHERE artifact_id=? AND record_status='active'",
            (output_artifact_id,),
        ).fetchone()
        if not artifact or artifact["student_id"] != student_id:
            raise ValueError("output_artifact_id must belong to the same student")
    now = utc_now()
    started_at = row["started_at"] or (now if status == "in_progress" else None)
    completed_at = now if status in {"completed", "failed", "cancelled"} else None
    with conn:
        conn.execute(
            """
            UPDATE artifact_generation_runs
            SET status=?,output_artifact_id=COALESCE(?,output_artifact_id),
                output_path=COALESCE(?,output_path),output_sha256=COALESCE(?,output_sha256),
                summary=COALESCE(?,summary),started_at=?,completed_at=?,updated_at=?
            WHERE generation_id=?
            """,
            (
                status,
                payload.get("output_artifact_id"),
                payload.get("output_path"),
                payload.get("output_sha256"),
                payload.get("summary"),
                started_at,
                completed_at,
                now,
                generation_id,
            ),
        )
    return {
        "status": "updated",
        "generation": generation_detail(conn, generation_id, student_id=student_id),
    }


def mark_completed_generations_stale(
    conn: sqlite3.Connection,
    *,
    student_id: str,
    reason: str = "new_learning_evidence",
) -> int:
    """Mark prior completed outputs stale inside the caller's transaction.

    Evidence ingestion owns the surrounding transaction so a failed import
    cannot leave generation freshness metadata committed on its own.
    """
    now = utc_now()
    cursor = conn.execute(
        """
        UPDATE artifact_generation_runs
        SET stale_reason=?,stale_at=?,updated_at=?
        WHERE student_id=? AND status='completed' AND stale_reason IS NULL
        """,
        (reason, now, now, student_id),
    )
    return int(cursor.rowcount)


def generation_detail(
    conn: sqlite3.Connection,
    generation_id: str,
    *,
    student_id: str,
) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT * FROM artifact_generation_runs
        WHERE generation_id=? AND student_id=?
        """,
        (generation_id, student_id),
    ).fetchone()
    if not row:
        raise ValueError(f"Unknown generation_id for student {student_id}: {generation_id}")
    return _generation_dict(row)


def list_generations(
    conn: sqlite3.Connection,
    *,
    student_id: str,
    limit: int = 30,
) -> dict[str, Any]:
    rows = conn.execute(
        """
        SELECT * FROM artifact_generation_runs
        WHERE student_id=? ORDER BY updated_at DESC,generation_id DESC LIMIT ?
        """,
        (student_id, max(1, min(int(limit), 100))),
    ).fetchall()
    return {"count": len(rows), "items": [_generation_dict(row) for row in rows]}


def _generation_dict(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    result["source_snapshot"] = json.loads(result.pop("source_snapshot_json"))
    return result
