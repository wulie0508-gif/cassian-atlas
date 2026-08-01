from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any, Iterable

from .grammar_catalog import CONFIRMED_STATUSES, current_snapshot_id


def _parse_datetime(value: str | None) -> datetime:
    if not value:
        return datetime.now(UTC)
    raw = value[:-1] + "+00:00" if value.endswith("Z") else value
    result = datetime.fromisoformat(raw)
    if result.tzinfo is None:
        result = result.replace(tzinfo=UTC)
    return result


def _recent_error_weights(conn, student_id: str, snapshot_id: str, start: datetime, end: datetime) -> tuple[dict[str, float], list[dict[str, Any]]]:
    rows = list(
        conn.execute(
            """
            SELECT kp.code, a.attempt_id, a.item_id, a.attempted_at,
                   qkm.confidence, qkm.verification_status
            FROM attempts a
            JOIN evaluations e ON e.attempt_id=a.attempt_id AND e.is_current=1
            JOIN external_references er
              ON er.item_id=a.item_id
             AND er.namespace='shanghai_question_bank'
             AND er.reference_type='question_id'
            JOIN question_knowledge_map qkm
              ON qkm.source_snapshot_id=? AND qkm.question_id=er.external_id
             AND qkm.verification_status<>'rejected'
            JOIN knowledge_points kp ON kp.knowledge_point_id=qkm.knowledge_point_id
            WHERE a.student_id=? AND a.record_status='active'
              AND e.result IN ('wrong', 'partial')
            """,
            (snapshot_id, student_id),
        )
    )
    evidence: dict[str, dict[str, Any]] = {}
    weights: dict[str, float] = defaultdict(float)
    for row in rows:
        attempted = _parse_datetime(row["attempted_at"])
        if not start <= attempted < end:
            continue
        point = evidence.setdefault(row["code"], {"code": row["code"], "attempt_ids": set(), "item_ids": set(), "latest_error_at": None})
        point["attempt_ids"].add(row["attempt_id"])
        point["item_ids"].add(row["item_id"])
        point["latest_error_at"] = max(point["latest_error_at"] or row["attempted_at"], row["attempted_at"])
        age_days = max(0.0, (end - attempted).total_seconds() / 86400)
        recency = 1.0 / (1.0 + age_days / 7.0)
        status_factor = 1.0 if row["verification_status"] in CONFIRMED_STATUSES else 0.55
        weights[row["code"]] += 2.0 * recency * float(row["confidence"]) * status_factor
    output = []
    for point in evidence.values():
        error_count = len(point.pop("attempt_ids"))
        item_count = len(point.pop("item_ids"))
        output.append(
            {
                **point,
                "error_count": error_count,
                "distinct_item_count": item_count,
                "evidence_label": "tentative" if error_count == 1 or item_count == 1 else "repeated",
                "selection_weight_added": round(weights[point["code"]], 4),
            }
        )
    output.sort(key=lambda row: (-row["selection_weight_added"], row["code"]))
    return dict(weights), output


def weighted_set_cover(
    conn,
    target_codes: Iterable[str],
    *,
    student_id: str | None = None,
    recent_error_days: int = 30,
    max_passages: int = 5,
    as_of: str | None = None,
    snapshot_id: str | None = None,
) -> dict[str, Any]:
    snapshot_id = snapshot_id or current_snapshot_id(conn)
    target_codes = list(dict.fromkeys(target_codes))
    if not target_codes:
        raise ValueError("At least one target knowledge-point code is required.")
    if max_passages < 1:
        raise ValueError("max_passages must be positive.")
    known = {row["code"] for row in conn.execute("SELECT code FROM knowledge_points WHERE active=1")}
    unknown = sorted(set(target_codes) - known)
    if unknown:
        raise ValueError(f"Unknown knowledge-point codes: {', '.join(unknown)}")

    weights = {code: 3.0 for code in target_codes}
    recent_evidence: list[dict[str, Any]] = []
    end = _parse_datetime(as_of)
    start = end - timedelta(days=recent_error_days)
    if student_id:
        student = conn.execute("SELECT 1 FROM students WHERE student_id=? AND active=1", (student_id,)).fetchone()
        if not student:
            raise ValueError(f"Unknown or inactive student_id: {student_id}")
        error_weights, recent_evidence = _recent_error_weights(conn, student_id, snapshot_id, start, end)
        for code, weight in error_weights.items():
            if code in known:
                weights[code] = weights.get(code, 0.0) + weight

    point_rows = list(
        conn.execute(
            """
            SELECT p.passage_id, p.title, p.question_count, kp.code,
                   qkm.confidence, qkm.verification_status
            FROM grammar_passage_catalog p
            JOIN grammar_question_catalog q
              ON q.source_snapshot_id=p.source_snapshot_id AND q.passage_id=p.passage_id
            JOIN question_knowledge_map qkm
              ON qkm.source_snapshot_id=q.source_snapshot_id AND qkm.question_id=q.question_id
             AND qkm.verification_status<>'rejected'
            JOIN knowledge_points kp ON kp.knowledge_point_id=qkm.knowledge_point_id
            WHERE p.source_snapshot_id=? AND p.complete_passage=1
              AND p.verification_status IN ('source_checked', 'verified')
            """,
            (snapshot_id,),
        )
    )
    candidates: dict[str, dict[str, Any]] = {}
    for row in point_rows:
        if row["code"] not in weights:
            continue
        candidate = candidates.setdefault(
            row["passage_id"],
            {"passage_id": row["passage_id"], "title": row["title"], "question_count": row["question_count"], "quality": {}, "statuses": defaultdict(set)},
        )
        quality = 1.0 if row["verification_status"] in CONFIRMED_STATUSES else float(row["confidence"]) * 0.55
        candidate["quality"][row["code"]] = max(candidate["quality"].get(row["code"], 0.0), quality)
        candidate["statuses"][row["code"]].add(row["verification_status"])

    remaining = {code: 1.0 for code in weights}
    achieved = {code: 0.0 for code in weights}
    selected: list[dict[str, Any]] = []
    available = dict(candidates)
    while available and len(selected) < max_passages:
        best_id = None
        best_score = 0.0
        best_gain = 0.0
        for passage_id, candidate in available.items():
            gain = sum(weights[code] * max(0.0, candidate["quality"].get(code, 0.0) - achieved[code]) for code in weights)
            cost = 1.0 + 0.01 * candidate["question_count"]
            score = gain / cost
            if score > best_score or (score == best_score and gain > best_gain):
                best_id, best_score, best_gain = passage_id, score, gain
        if not best_id or best_gain <= 0:
            break
        candidate = available.pop(best_id)
        marginal = []
        for code in weights:
            new_quality = max(achieved[code], candidate["quality"].get(code, 0.0))
            delta = new_quality - achieved[code]
            if delta <= 0:
                continue
            achieved[code] = new_quality
            remaining[code] = max(0.0, 1.0 - achieved[code])
            marginal.append(
                {
                    "code": code,
                    "quality_added": round(delta, 3),
                    "mapping_statuses": sorted(candidate["statuses"][code]),
                }
            )
        selected.append(
            {
                "passage_id": candidate["passage_id"],
                "title": candidate["title"],
                "question_count": candidate["question_count"],
                "marginal_gain": round(best_gain, 4),
                "gain_per_passage_cost": round(best_score, 4),
                "coverage_added": marginal,
            }
        )
        if all(value <= 1e-9 for value in remaining.values()):
            break

    coverage = [
        {
            "code": code,
            "weight": round(weights[code], 4),
            "achieved_quality": round(min(1.0, achieved[code]), 3),
            "status": "confirmed" if achieved[code] >= 1.0 else ("suggested_only" if achieved[code] > 0 else "uncovered"),
        }
        for code in weights
    ]
    return {
        "algorithm": "weighted-greedy-set-cover-v1",
        "source_snapshot_id": snapshot_id,
        "candidate_rule": "Only complete source_checked passages; a passage is always selected as a whole.",
        "requested_knowledge_points": target_codes,
        "recent_error_window": {"student_id": student_id, "days": recent_error_days, "start": start.isoformat(), "end": end.isoformat()},
        "recent_error_evidence": recent_evidence,
        "selected_passages": selected,
        "knowledge_coverage": coverage,
        "uncovered": [row["code"] for row in coverage if row["status"] == "uncovered"],
        "suggested_only": [row["code"] for row in coverage if row["status"] == "suggested_only"],
        "mapping_policy": "source_checked/verified mappings receive full value; suggestions receive 55% value and remain suggestions.",
    }
