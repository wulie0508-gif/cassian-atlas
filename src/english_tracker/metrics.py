from __future__ import annotations

from collections import defaultdict
from datetime import UTC, date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def _timezone(name: str):
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        fixed_offsets = {"Asia/Shanghai": timezone(timedelta(hours=8))}
        if name in fixed_offsets:
            return fixed_offsets[name]
        raise ValueError(f"Timezone data is unavailable for: {name}")


def _dt(value: str, default_tz=UTC) -> datetime:
    raw = value[:-1] + "+00:00" if value.endswith("Z") else value
    result = datetime.fromisoformat(raw)
    if result.tzinfo is None:
        result = result.replace(tzinfo=default_tz)
    return result


def _week_bounds(week_start: str | None = None, as_of: str | None = None, timezone: str = "UTC") -> tuple[datetime, datetime]:
    tz = _timezone(timezone)
    if week_start:
        start_date = date.fromisoformat(week_start)
    else:
        current = _dt(as_of, tz).astimezone(tz) if as_of else datetime.now(tz)
        start_date = current.date() - timedelta(days=current.weekday())
    start = datetime.combine(start_date, time.min, tzinfo=tz)
    return start, start + timedelta(days=7)


def _assessment_kind(row) -> tuple[str, str]:
    if row["assessment_kind"]:
        return row["assessment_kind"], row["reporting_series"]
    session_type = (row["session_type"] or "other").lower()
    inferred = {
        "class": "lesson",
        "lesson": "lesson",
        "dictation": "dictation",
        "homework": "homework",
    }.get(session_type, "other")
    return inferred, session_type


def _score(result: str, score: float | None, max_score: float | None) -> float | None:
    if score is not None and max_score not in (None, 0):
        return float(score) / float(max_score)
    return {"correct": 1.0, "partial": 0.5, "wrong": 0.0}.get(result)


def _attempt_rows(conn, student_id: str) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute(
            """
            SELECT a.attempt_id, a.item_id, a.session_id, a.attempted_at,
                   a.answer_capture_status, a.attempt_phase,
                   e.result, e.score, e.max_score,
                   ls.session_type, sa.assessment_kind, sa.reporting_series
            FROM attempts a
            JOIN evaluations e ON e.attempt_id=a.attempt_id AND e.is_current=1
            JOIN learning_sessions ls ON ls.session_id=a.session_id AND ls.record_status='active'
            LEFT JOIN session_assessments sa ON sa.session_id=ls.session_id
            WHERE a.student_id=? AND a.record_status='active'
            """,
            (student_id,),
        )
    ]


def _knowledge_accuracy(conn, attempt_ids: list[str]) -> list[dict[str, Any]]:
    if not attempt_ids:
        return []
    placeholders = ",".join("?" for _ in attempt_ids)
    rows = conn.execute(
        f"""
        SELECT kp.code, kp.name_cn, a.attempt_id, a.item_id,
               e.result, e.score, e.max_score,
               ikm.mapping_source, ikm.verification_status
        FROM attempts a
        JOIN evaluations e ON e.attempt_id=a.attempt_id AND e.is_current=1
        JOIN item_knowledge_map ikm ON ikm.item_id=a.item_id
        JOIN knowledge_points kp ON kp.knowledge_point_id=ikm.knowledge_point_id
        WHERE a.attempt_id IN ({placeholders})
          AND ikm.verification_status<>'rejected'
        """,
        attempt_ids,
    )
    groups: dict[str, dict[str, Any]] = {}
    for row in rows:
        group = groups.setdefault(row["code"], {"code": row["code"], "name_cn": row["name_cn"], "attempt_ids": set(), "item_ids": set(), "scores": [], "errors": 0, "confirmed_mapping_attempts": 0, "suggested_mapping_attempts": 0})
        if row["attempt_id"] in group["attempt_ids"]:
            continue
        group["attempt_ids"].add(row["attempt_id"])
        group["item_ids"].add(row["item_id"])
        score = _score(row["result"], row["score"], row["max_score"])
        if score is not None:
            group["scores"].append(score)
        group["errors"] += int(row["result"] in {"wrong", "partial"})
        if row["verification_status"] in {"source_checked", "verified"}:
            group["confirmed_mapping_attempts"] += 1
        else:
            group["suggested_mapping_attempts"] += 1
    output = []
    for group in groups.values():
        attempts = len(group.pop("attempt_ids"))
        distinct_items = len(group.pop("item_ids"))
        scores = group.pop("scores")
        errors = group.pop("errors")
        output.append(
            {
                **group,
                "accuracy": round(sum(scores) / len(scores), 4) if scores else None,
                "scored_attempt_count": len(scores),
                "attempt_count": attempts,
                "distinct_item_count": distinct_items,
                "error_count": errors,
                "weakness_evidence": "tentative" if errors == 1 else ("repeated" if errors > 1 else "none"),
                "sample_size": attempts,
                "mapping_evidence_status": "confirmed" if group["confirmed_mapping_attempts"] else "suggested_only",
            }
        )
    output.sort(key=lambda row: (row["accuracy"] is None, row["accuracy"] if row["accuracy"] is not None else 2, -row["sample_size"], row["code"]))
    return output


def weekly_report(conn, student_id: str, *, week_start: str | None = None, as_of: str | None = None) -> dict[str, Any]:
    student = conn.execute("SELECT timezone FROM students WHERE student_id=? AND active=1", (student_id,)).fetchone()
    if not student:
        raise ValueError(f"Unknown or inactive student_id: {student_id}")
    tz = _timezone(student["timezone"])
    start, end = _week_bounds(week_start, as_of, student["timezone"])
    all_attempts = _attempt_rows(conn, student_id)
    attempts = [row for row in all_attempts if start <= _dt(row["attempted_at"], tz).astimezone(tz) < end]
    for row in attempts:
        row["kind"], row["series"] = _assessment_kind(row)
        row["normalized_score"] = _score(row["result"], row["score"], row["max_score"])

    assessment_groups: dict[tuple[str, str], list[float]] = defaultdict(list)
    for row in attempts:
        if row["normalized_score"] is not None:
            assessment_groups[(row["kind"], row["series"])].append(row["normalized_score"])
    assessment_accuracy = [
        {
            "assessment_kind": kind,
            "reporting_series": series,
            "accuracy": round(sum(scores) / len(scores), 4),
            "scored_attempt_count": len(scores),
        }
        for (kind, series), scores in sorted(assessment_groups.items())
    ]
    topic_accuracy = [
        {key: value for key, value in row.items() if key != "assessment_kind"}
        for row in assessment_accuracy
        if row["assessment_kind"] == "topic_quiz"
    ]

    capture_denominator = sum(row["answer_capture_status"] in {"captured", "captured_blank"} for row in attempts)
    blanks = sum(row["answer_capture_status"] == "captured_blank" for row in attempts)
    not_captured = sum(row["answer_capture_status"] == "not_captured" for row in attempts)

    sessions = [
        dict(row)
        for row in conn.execute(
            """
            SELECT ls.session_id, ls.session_type, ls.started_at, ls.ended_at,
                   sa.assessment_kind, sa.reporting_series, sa.delivery_mode,
                   sa.duration_seconds
            FROM learning_sessions ls
            LEFT JOIN session_assessments sa ON sa.session_id=ls.session_id
            WHERE ls.student_id=? AND ls.record_status='active'
            """,
            (student_id,),
        )
        if start <= _dt(row["started_at"], tz).astimezone(tz) < end
    ]
    duration_by_kind: dict[str, dict[str, int]] = defaultdict(lambda: {"duration_seconds": 0, "session_count": 0, "measured_session_count": 0})
    for row in sessions:
        kind, _ = _assessment_kind(row)
        duration = row["duration_seconds"]
        if duration is None and row["ended_at"]:
            duration = max(0, int((_dt(row["ended_at"]) - _dt(row["started_at"])).total_seconds()))
        duration_by_kind[kind]["session_count"] += 1
        if duration is not None:
            duration_by_kind[kind]["duration_seconds"] += int(duration)
            duration_by_kind[kind]["measured_session_count"] += 1

    eligible_reviews = 0
    recovered_reviews = 0
    history_by_item: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in sorted(all_attempts, key=lambda item: (_dt(item["attempted_at"]), item["attempt_id"])):
        history_by_item[row["item_id"]].append(row)
    week_attempt_ids = {row["attempt_id"] for row in attempts}
    for item_rows in history_by_item.values():
        prior_error = False
        for row in item_rows:
            if row["attempt_id"] in week_attempt_ids and row["attempt_phase"] == "review" and prior_error:
                eligible_reviews += 1
                recovered_reviews += int(row["result"] == "correct")
            prior_error = prior_error or row["result"] in {"wrong", "partial"}

    scored = [row["normalized_score"] for row in attempts if row["normalized_score"] is not None]
    return {
        "student_id": student_id,
        "period": {"week_start": start.date().isoformat(), "week_end_exclusive": end.date().isoformat()},
        "attempts": {"total": len(attempts), "scored": len(scored), "descriptive_all_activity_accuracy": round(sum(scored) / len(scored), 4) if scored else None},
        "assessment_accuracy": assessment_accuracy,
        "topic_accuracy": topic_accuracy,
        "completion_time": [
            {
                "assessment_kind": kind,
                "duration_seconds": values["duration_seconds"] if values["measured_session_count"] else None,
                "session_count": values["session_count"],
                "measured_session_count": values["measured_session_count"],
            }
            for kind, values in sorted(duration_by_kind.items())
        ],
        "blank_rate": {
            "blank_count": blanks,
            "captured_answer_opportunities": capture_denominator,
            "rate": round(blanks / capture_denominator, 4) if capture_denominator else None,
            "not_captured_count": not_captured,
            "definition": "captured_blank / (captured + captured_blank); not_captured is excluded and reported separately.",
        },
        "retest_recovery": {
            "recovered_count": recovered_reviews,
            "eligible_retest_count": eligible_reviews,
            "rate": round(recovered_reviews / eligible_reviews, 4) if eligible_reviews else None,
        },
        "knowledge_point_accuracy": _knowledge_accuracy(conn, [row["attempt_id"] for row in attempts]),
        "series_policy": "Topic quizzes, biweekly mixed tests, and full exams are separate assessment kinds. Raw scores are not combined here.",
    }


def _date_windows(start: date, end_exclusive: date, days: int):
    cursor = start
    while cursor < end_exclusive:
        window_end = min(cursor + timedelta(days=days), end_exclusive)
        yield cursor, window_end
        cursor = window_end


def trend_report(conn, student_id: str, *, start: str, end: str) -> dict[str, Any]:
    student = conn.execute("SELECT timezone FROM students WHERE student_id=? AND active=1", (student_id,)).fetchone()
    if not student:
        raise ValueError(f"Unknown or inactive student_id: {student_id}")
    tz = _timezone(student["timezone"])
    start_date = date.fromisoformat(start)
    end_date = date.fromisoformat(end)
    if end_date < start_date:
        raise ValueError("end must be on or after start")
    end_exclusive = end_date + timedelta(days=1)
    weekly = []
    cursor = start_date - timedelta(days=start_date.weekday())
    while cursor < end_exclusive:
        weekly.append(weekly_report(conn, student_id, week_start=cursor.isoformat()))
        cursor += timedelta(days=7)

    assessment_rows = [
        dict(row)
        for row in conn.execute(
            """
            SELECT ls.session_id, ls.started_at, sa.assessment_kind,
                   sa.reporting_series, sa.delivery_mode, sa.raw_score,
                   sa.max_score, sa.duration_seconds
            FROM learning_sessions ls
            JOIN session_assessments sa ON sa.session_id=ls.session_id
            WHERE ls.student_id=? AND ls.record_status='active'
            ORDER BY ls.started_at, ls.session_id
            """,
            (student_id,),
        )
        if start_date <= _dt(row["started_at"], tz).astimezone(tz).date() <= end_date
    ]
    raw_series: dict[str, dict[str, Any]] = {}
    for row in assessment_rows:
        if row["raw_score"] is None:
            continue
        max_key = format(float(row["max_score"]), "g")
        key = f"{row['assessment_kind']}|{row['reporting_series']}|max={max_key}"
        series = raw_series.setdefault(
            key,
            {
                "series_key": key,
                "assessment_kind": row["assessment_kind"],
                "reporting_series": row["reporting_series"],
                "max_score": row["max_score"],
                "points": [],
            },
        )
        series["points"].append({"session_id": row["session_id"], "date": _dt(row["started_at"], tz).astimezone(tz).date().isoformat(), "raw_score": row["raw_score"]})

    def observed(kind: str, window_start: date, window_end: date) -> list[dict[str, Any]]:
        return [
            row
            for row in assessment_rows
            if row["assessment_kind"] == kind and window_start <= _dt(row["started_at"], tz).astimezone(tz).date() < window_end
            and row["delivery_mode"] == "offline_closed"
        ]

    biweekly = [
        {"start": a.isoformat(), "end_exclusive": b.isoformat(), "observed": len(observed("biweekly_mixed_test", a, b)), "met": bool(observed("biweekly_mixed_test", a, b))}
        for a, b in _date_windows(start_date, end_exclusive, 14)
    ]
    four_week = [
        {"start": a.isoformat(), "end_exclusive": b.isoformat(), "observed": len(observed("full_exam", a, b)), "met": bool(observed("full_exam", a, b))}
        for a, b in _date_windows(start_date, end_exclusive, 28)
    ]
    december_weekly = []
    december_policy_start = date(start_date.year if start_date.month >= 7 else start_date.year - 1, 12, 1)
    week_cursor = start_date - timedelta(days=start_date.weekday())
    while week_cursor < end_exclusive:
        week_end = week_cursor + timedelta(days=7)
        if week_end > december_policy_start:
            effective_start = max(week_cursor, start_date, december_policy_start)
            count = len(observed("full_exam", effective_start, min(week_end, end_exclusive)))
            december_weekly.append({"week_start": week_cursor.isoformat(), "observed": count, "met_minimum": count >= 1, "within_target_range": 1 <= count <= 2})
        week_cursor = week_end

    return {
        "student_id": student_id,
        "period": {"start": start_date.isoformat(), "end": end_date.isoformat()},
        "weekly_metrics": weekly,
        "raw_score_series": list(raw_series.values()),
        "raw_score_guardrail": "Every series is partitioned by assessment_kind, reporting_series, and max_score; unlike raw totals are never connected.",
        "schedule_compliance": {
            "biweekly_offline_closed_mixed_test": biweekly,
            "four_week_offline_closed_full_exam": four_week,
            "december_onward_weekly_full_exam": december_weekly,
        },
    }
