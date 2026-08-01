from __future__ import annotations

import math
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

from .util import utc_now


DOMAIN_ROOTS = {"vocabulary", "grammar", "reading", "translation", "writing"}


def _as_of(value: str | None) -> datetime:
    if not value:
        return datetime.now(UTC)
    raw = value[:-1] + "+00:00" if value.endswith("Z") else value
    result = datetime.fromisoformat(raw)
    return result if result.tzinfo else result.replace(tzinfo=UTC)


def _score(row: dict) -> float | None:
    result = row["result"]
    if result == "correct":
        return 1.0
    if result == "wrong":
        return 0.0
    if result == "partial":
        if row.get("score") is not None and row.get("max_score"):
            return max(0.0, min(1.0, row["score"] / row["max_score"]))
        return 0.5
    return None


def _confidence_label(attempts: int, distinct_items: int, error_rate: float) -> tuple[str, str]:
    if attempts < 2 or distinct_items < 2:
        return (
            "tentative" if error_rate >= 0.5 else "insufficient_evidence",
            "暂定薄弱点" if error_rate >= 0.5 else "证据不足",
        )
    if attempts < 5:
        return ("emerging", "初步证据")
    if attempts < 10:
        return ("moderate", "中等可信")
    return ("established", "较高可信")


def _external_evidence(conn, item_ids: list[str]) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    for item_id in item_ids[:5]:
        item = conn.execute(
            "SELECT prompt_snapshot, answer_snapshot FROM content_items WHERE item_id=?",
            (item_id,),
        ).fetchone()
        refs = [
            dict(row)
            for row in conn.execute(
                """
                SELECT namespace,reference_type,external_id,external_parent_id
                FROM external_references WHERE item_id=?
                ORDER BY CASE WHEN namespace='shanghai_question_bank' THEN 0 ELSE 1 END, namespace
                """,
                (item_id,),
            )
        ]
        evidence.append(
            {
                "item_id": item_id,
                "prompt": (item["prompt_snapshot"] or "")[:180] if item else None,
                "answer": item["answer_snapshot"] if item else None,
                "external_references": refs,
            }
        )
    return evidence


def _attempt_rows(conn, student_id: str, as_of: datetime, start: datetime | None = None) -> list[dict]:
    sql = """
        SELECT
          a.attempt_id,a.item_id,a.attempted_at,a.attempt_phase,a.response_mode,
          e.result,e.score,e.max_score,
          ci.difficulty_weight,
          kp.code,kp.name_en,kp.name_cn,kp.domain,kp.parent_id
        FROM attempts a
        JOIN evaluations e ON e.attempt_id=a.attempt_id AND e.is_current=1
        JOIN content_items ci ON ci.item_id=a.item_id AND ci.record_status='active'
        JOIN item_knowledge_map ikm ON ikm.item_id=a.item_id AND ikm.verification_status<>'rejected'
        JOIN knowledge_points kp ON kp.knowledge_point_id=ikm.knowledge_point_id
        WHERE a.student_id=? AND a.record_status='active' AND a.attempted_at<=?
    """
    params: list[Any] = [student_id, as_of.isoformat()]
    if start:
        sql += " AND a.attempted_at>=?"
        params.append(start.isoformat())
    sql += " ORDER BY a.attempted_at,a.attempt_id"
    rows = [dict(row) for row in conn.execute(sql, params)]
    # Avoid duplicating domain-root evidence when a more specific knowledge point
    # exists for the same item.
    specific_items = {row["item_id"] for row in rows if row["code"] not in DOMAIN_ROOTS}
    return [row for row in rows if row["code"] not in DOMAIN_ROOTS or row["item_id"] not in specific_items]


def _aggregate_window(conn, student_id: str, as_of: datetime, days: int | None) -> list[dict[str, Any]]:
    start = as_of - timedelta(days=days) if days is not None else None
    rows = _attempt_rows(conn, student_id, as_of, start)
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        groups[row["code"]].append(row)
    result: list[dict[str, Any]] = []
    for code, group in groups.items():
        scored = [row for row in group if _score(row) is not None]
        if not scored:
            continue
        scores = [_score(row) for row in scored]
        errors = [row for row in scored if row["result"] in {"wrong", "partial"}]
        distinct_items = len({row["item_id"] for row in scored})
        first_by_item: dict[str, dict] = {}
        for row in scored:
            first_by_item.setdefault(row["item_id"], row)
        first_scores = [_score(row) for row in first_by_item.values()]
        recent_cutoff = as_of - timedelta(days=7)
        recent = [row for row in scored if _as_of(row["attempted_at"]) >= recent_cutoff]
        recent_scores = [_score(row) for row in recent]
        consecutive_errors = 0
        for row in reversed(scored):
            if row["result"] == "correct":
                break
            if row["result"] in {"wrong", "partial"}:
                consecutive_errors += 1
        reviews = [row for row in scored if row["attempt_phase"] == "review"]
        latest_review = reviews[-1]["result"] if reviews else None
        accuracy = sum(scores) / len(scores)
        error_rate = 1.0 - accuracy
        recency_days = min(
            ((_as_of(as_of.isoformat()) - _as_of(row["attempted_at"])).total_seconds() / 86400 for row in errors),
            default=365.0,
        )
        recency_factor = math.exp(-max(0.0, recency_days) / 30.0)
        sample_factor = min(1.0, math.sqrt(len(scored) / 5.0))
        streak_factor = min(1.0, consecutive_errors / 3.0)
        review_penalty = 1.0 if latest_review in {"wrong", "partial"} else (0.5 if latest_review is None else 0.0)
        difficulty_factor = min(1.5, sum(row["difficulty_weight"] for row in scored) / len(scored)) / 1.5
        recall_factor = sum(row["response_mode"] == "active_recall" for row in scored) / len(scored)
        weakness_score = 100 * sample_factor * (
            0.45 * error_rate
            + 0.20 * recency_factor
            + 0.15 * streak_factor
            + 0.10 * review_penalty
            + 0.05 * difficulty_factor
            + 0.05 * recall_factor
        )
        confidence_en, confidence_cn = _confidence_label(len(scored), distinct_items, error_rate)
        error_item_ids: list[str] = []
        for row in reversed(errors):
            if row["item_id"] not in error_item_ids:
                error_item_ids.append(row["item_id"])
        result.append(
            {
                "knowledge_point": code,
                "name_en": group[0]["name_en"],
                "name_cn": group[0]["name_cn"],
                "domain": group[0]["domain"],
                "error_count": len(errors),
                "attempt_count": len(scored),
                "distinct_item_count": distinct_items,
                "first_attempt_accuracy": round(sum(first_scores) / len(first_scores), 4),
                "overall_accuracy": round(accuracy, 4),
                "recent_7d_accuracy": round(sum(recent_scores) / len(recent_scores), 4) if recent_scores else None,
                "consecutive_errors": consecutive_errors,
                "last_error_at": max((row["attempted_at"] for row in errors), default=None),
                "latest_review_result": latest_review,
                "sample_size": len(scored),
                "confidence": confidence_en,
                "confidence_cn": confidence_cn,
                "weakness_score": round(weakness_score, 2),
                "evidence_items": _external_evidence(conn, error_item_ids),
            }
        )
    result.sort(key=lambda row: (-row["weakness_score"], -row["error_count"], row["knowledge_point"]))
    return result


def weakness_report(conn, student_id: str, *, as_of: str | None = None, days: int = 30) -> dict[str, Any]:
    as_of_dt = _as_of(as_of)
    return {
        "student_id": student_id,
        "generated_at": utc_now(),
        "as_of": as_of_dt.isoformat(),
        "method": {
            "version": "weakness-v1",
            "principle": "error rate + sample size + recency + error streak + review outcome + difficulty + response mode",
            "confidence_rule": "fewer than 2 attempts or 2 distinct items is never treated as a stable weakness",
        },
        "windows": {
            "7_days": _aggregate_window(conn, student_id, as_of_dt, 7),
            f"{days}_days": _aggregate_window(conn, student_id, as_of_dt, days),
            "all_time": _aggregate_window(conn, student_id, as_of_dt, None),
        },
    }


def due_reviews(conn, student_id: str, *, as_of: str | None = None, domain: str | None = None, limit: int = 100) -> dict[str, Any]:
    as_of_dt = _as_of(as_of)
    params: list[Any] = [student_id, as_of_dt.isoformat()]
    domain_clause = ""
    if domain:
        domain_clause = " AND ci.domain=?"
        params.append(domain)
    params.append(limit)
    rows = [
        dict(row)
        for row in conn.execute(
            f"""
            SELECT rt.review_task_id,rt.item_id,rt.reason_code,rt.due_at,rt.priority,
                   ci.domain,ci.item_type,ci.prompt_snapshot,ci.answer_snapshot,
                   rs.consecutive_errors,rs.last_result
            FROM review_tasks rt
            JOIN content_items ci ON ci.item_id=rt.item_id AND ci.record_status='active'
            LEFT JOIN review_state rs ON rs.student_id=rt.student_id AND rs.item_id=rt.item_id
            WHERE rt.student_id=? AND rt.status='open' AND rt.due_at<=? {domain_clause}
            ORDER BY rt.priority DESC,rt.due_at,rt.review_task_id
            LIMIT ?
            """,
            params,
        )
    ]
    for row in rows:
        row["external_references"] = [
            dict(ref)
            for ref in conn.execute(
                "SELECT namespace,reference_type,external_id,external_parent_id FROM external_references WHERE item_id=?",
                (row["item_id"],),
            )
        ]
    return {"student_id": student_id, "as_of": as_of_dt.isoformat(), "count": len(rows), "items": rows}


def session_acceptance_report(conn, session_id: str) -> dict[str, Any]:
    session = conn.execute("SELECT * FROM learning_sessions WHERE session_id=?", (session_id,)).fetchone()
    if not session:
        raise ValueError(f"Unknown session: {session_id}")
    attempts = [
        dict(row)
        for row in conn.execute(
            """
            SELECT a.attempt_id,a.item_id,a.event_id,a.attempted_at,a.answer_capture_status,
                   a.attempt_phase,e.result,e.score,e.max_score
            FROM attempts a JOIN evaluations e ON e.attempt_id=a.attempt_id AND e.is_current=1
            WHERE a.session_id=? AND a.record_status='active'
            ORDER BY a.attempted_at,a.attempt_id
            """,
            (session_id,),
        )
    ]
    scored = [row for row in attempts if _score(row) is not None]
    kp_errors = [
        dict(row)
        for row in conn.execute(
            """
            SELECT kp.code,kp.name_cn,COUNT(*) error_count,COUNT(DISTINCT a.item_id) item_count
            FROM attempts a
            JOIN evaluations e ON e.attempt_id=a.attempt_id AND e.is_current=1
            JOIN item_knowledge_map ikm ON ikm.item_id=a.item_id AND ikm.verification_status<>'rejected'
            JOIN knowledge_points kp ON kp.knowledge_point_id=ikm.knowledge_point_id
            WHERE a.session_id=? AND a.record_status='active'
              AND e.result IN ('wrong','partial') AND kp.code NOT IN ('vocabulary','grammar','reading','translation','writing')
            GROUP BY kp.code,kp.name_cn ORDER BY kp.code
            """,
            (session_id,),
        )
    ]
    for row in kp_errors:
        label_en, label_cn = _confidence_label(row["error_count"], row["item_count"], 1.0)
        row["confidence"] = label_en
        row["confidence_cn"] = label_cn
    observations = [dict(row) for row in conn.execute("SELECT observation_type,observation_text,evidence_level FROM session_observations WHERE session_id=? AND record_status='active'", (session_id,))]
    progress = [dict(row) for row in conn.execute("SELECT content_label,external_namespace,external_id,progress_status,completed_count,total_count,note FROM session_progress WHERE session_id=? AND record_status='active' ORDER BY progress_id", (session_id,))]
    review_count = conn.execute(
        """SELECT COUNT(*) FROM review_tasks rt JOIN attempts a ON a.attempt_id=rt.source_attempt_id
           WHERE a.session_id=? AND rt.status='open'""",
        (session_id,),
    ).fetchone()[0]
    return {
        "session_id": session_id,
        "student_id": session["student_id"],
        "attempts": len(scored),
        "correct": sum(row["result"] == "correct" for row in scored),
        "accuracy": round(sum(_score(row) for row in scored) / len(scored), 4) if scored else None,
        "knowledge_point_errors": kp_errors,
        "open_review_tasks_from_session": review_count,
        "observations": observations,
        "progress": progress,
    }


def export_context(conn, student_id: str, audience: str, *, as_of: str | None = None) -> dict[str, Any]:
    if audience not in {"courseware", "dictation"}:
        raise ValueError("audience must be courseware or dictation")
    weaknesses = weakness_report(conn, student_id, as_of=as_of, days=30)
    domain = "vocabulary" if audience == "dictation" else None
    due = due_reviews(conn, student_id, as_of=as_of, domain=domain, limit=60)
    result = {
        "contract_version": "1.0",
        "audience": audience,
        "student_id": student_id,
        "generated_at": utc_now(),
        "weaknesses_30d": weaknesses["windows"]["30_days"],
        "due_reviews": due["items"],
    }
    if audience == "courseware":
        result["selection_rules"] = {
            "question_bank_verification_status": ["verified", "source_checked"],
            "prefer_complete_passage": True,
            "prioritize": ["open review tasks", "established weaknesses", "tentative weaknesses for diagnostic retest"],
        }
    else:
        result["selection_rules"] = {
            "content_domain": "vocabulary",
            "prefer_response_modes": ["active_recall", "production"],
            "prioritize_error_types": ["active_recall_failure", "spelling_error", "fixed_phrase_missing", "near_synonym_substitution"],
        }
    return result
