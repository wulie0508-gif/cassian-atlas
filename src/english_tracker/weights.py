from __future__ import annotations

from collections import defaultdict
from typing import Any

from .util import utc_now


POLICY_VERSION = "evidence-v1"


def _score(result: str, score: float | None, max_score: float | None) -> float | None:
    if result == "correct":
        return 1.0
    if result == "wrong":
        return 0.0
    if result == "partial":
        if score is not None and max_score:
            return max(0.0, min(1.0, float(score) / float(max_score)))
        return 0.5
    return None


def _assessment_weight(conn, assessment_kind: str, delivery_mode: str) -> tuple[float, bool, str]:
    row = conn.execute(
        """
        SELECT evidence_weight,is_calibration_anchor,rationale
        FROM assessment_weight_policies
        WHERE assessment_kind=? AND delivery_mode=? AND policy_version=?
        """,
        (assessment_kind, delivery_mode, POLICY_VERSION),
    ).fetchone()
    if not row:
        return 0.75, False, "未匹配到策略，使用保守默认权重。"
    return float(row["evidence_weight"]), bool(row["is_calibration_anchor"]), row["rationale"]


def _modifier(conn, dimension: str, value: str | None, fallback: float = 1.0) -> float:
    if not value:
        return fallback
    row = conn.execute(
        """
        SELECT multiplier FROM question_weight_rules
        WHERE dimension=? AND match_value=? AND policy_version=?
        """,
        (dimension, value, POLICY_VERSION),
    ).fetchone()
    return float(row[0]) if row else fallback


def evidence_weight(
    conn,
    *,
    assessment_kind: str,
    delivery_mode: str,
    difficulty: str | None,
    verification_status: str | None,
    answer_capture_status: str | None,
) -> dict[str, Any]:
    base, anchor, rationale = _assessment_weight(conn, assessment_kind, delivery_mode)
    difficulty_multiplier = _modifier(conn, "difficulty", difficulty)
    verification_multiplier = _modifier(conn, "verification_status", verification_status)
    capture_multiplier = _modifier(conn, "answer_capture_status", answer_capture_status)
    total = max(0.25, min(2.25, base * difficulty_multiplier * verification_multiplier * capture_multiplier))
    return {
        "policy_version": POLICY_VERSION,
        "assessment_weight": round(base, 4),
        "difficulty_multiplier": round(difficulty_multiplier, 4),
        "verification_multiplier": round(verification_multiplier, 4),
        "capture_multiplier": round(capture_multiplier, 4),
        "evidence_weight": round(total, 4),
        "is_calibration_anchor": anchor,
        "rationale": rationale,
    }


def weight_policy_report(conn) -> dict[str, Any]:
    policies = [
        dict(row)
        for row in conn.execute(
            """
            SELECT assessment_kind,delivery_mode,evidence_weight,is_calibration_anchor,rationale
            FROM assessment_weight_policies
            WHERE policy_version=?
            ORDER BY evidence_weight DESC,assessment_kind,delivery_mode
            """,
            (POLICY_VERSION,),
        )
    ]
    rules = [
        dict(row)
        for row in conn.execute(
            """
            SELECT dimension,match_value,multiplier,rationale
            FROM question_weight_rules
            WHERE policy_version=? ORDER BY dimension,multiplier DESC
            """,
            (POLICY_VERSION,),
        )
    ]
    return {
        "policy_version": POLICY_VERSION,
        "formula": "assessment_weight × difficulty_multiplier × verification_multiplier × answer_capture_multiplier",
        "bounds": {"minimum": 0.25, "maximum": 2.25},
        "principle": "线下闭卷整卷和双周混合测承担校准作用；平时练习用于高频诊断，但不能压过受控线下证据。",
        "assessment_policies": policies,
        "question_rules": rules,
    }


def _confidence(attempts: int, distinct_items: int, weighted_sample: float, error_count: int) -> tuple[str, str]:
    if attempts < 2 or distinct_items < 2:
        return ("tentative", "暂定薄弱点") if error_count else ("insufficient_evidence", "证据不足")
    if weighted_sample < 4:
        return "emerging", "初步证据"
    if weighted_sample < 8:
        return "moderate", "中等可信"
    return "established", "较高可信"


def weighted_mastery_report(conn, student_id: str) -> dict[str, Any]:
    rows = [
        dict(row)
        for row in conn.execute(
            """
            SELECT
              a.attempt_id,a.item_id,a.answer_capture_status,a.attempted_at,
              e.result,e.score,e.max_score,
              COALESCE(sa.assessment_kind,'lesson') assessment_kind,
              COALESCE(sa.delivery_mode,'unspecified') delivery_mode,
              ci.difficulty_label,
              ikm.verification_status mapping_verification_status,
              kp.code,kp.name_cn,kp.domain
            FROM attempts a
            JOIN evaluations e ON e.attempt_id=a.attempt_id AND e.is_current=1
            JOIN content_items ci ON ci.item_id=a.item_id AND ci.record_status='active'
            LEFT JOIN session_assessments sa ON sa.session_id=a.session_id
            JOIN item_knowledge_map ikm
              ON ikm.item_id=a.item_id AND ikm.verification_status<>'rejected'
            JOIN knowledge_points kp ON kp.knowledge_point_id=ikm.knowledge_point_id
            WHERE a.student_id=? AND a.record_status='active'
              AND kp.code NOT IN ('vocabulary','grammar','reading','translation','writing')
            ORDER BY a.attempted_at,a.attempt_id,kp.code
            """,
            (student_id,),
        )
    ]
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    global_seen: set[str] = set()
    global_weight = 0.0
    global_success = 0.0
    offline_weight = 0.0
    offline_success = 0.0
    practice_weight = 0.0
    practice_success = 0.0
    for row in rows:
        score = _score(row["result"], row["score"], row["max_score"])
        if score is None:
            continue
        weight = evidence_weight(
            conn,
            assessment_kind=row["assessment_kind"],
            delivery_mode=row["delivery_mode"],
            difficulty=row["difficulty_label"],
            verification_status=row["mapping_verification_status"],
            answer_capture_status=row["answer_capture_status"],
        )
        row["normalized_score"] = score
        row["weight_detail"] = weight
        row["weighted_success"] = score * weight["evidence_weight"]
        groups[row["code"]].append(row)
        if row["attempt_id"] not in global_seen:
            global_seen.add(row["attempt_id"])
            global_weight += weight["evidence_weight"]
            global_success += row["weighted_success"]
            if weight["is_calibration_anchor"]:
                offline_weight += weight["evidence_weight"]
                offline_success += row["weighted_success"]
            else:
                practice_weight += weight["evidence_weight"]
                practice_success += row["weighted_success"]

    knowledge: list[dict[str, Any]] = []
    for code, group in groups.items():
        weighted_sample = sum(row["weight_detail"]["evidence_weight"] for row in group)
        weighted_success = sum(row["weighted_success"] for row in group)
        attempts = len({row["attempt_id"] for row in group})
        distinct_items = len({row["item_id"] for row in group})
        error_count = len({row["attempt_id"] for row in group if row["normalized_score"] < 1})
        accuracy = weighted_success / weighted_sample if weighted_sample else None
        # Beta(1,1) prior keeps tiny samples from displaying false certainty.
        posterior = (1 + weighted_success) / (2 + weighted_sample) if weighted_sample else 0.5
        confidence, confidence_cn = _confidence(attempts, distinct_items, weighted_sample, error_count)
        knowledge.append(
            {
                "knowledge_point": code,
                "name_cn": group[0]["name_cn"],
                "domain": group[0]["domain"],
                "attempt_count": attempts,
                "distinct_item_count": distinct_items,
                "error_count": error_count,
                "weighted_sample_size": round(weighted_sample, 3),
                "weighted_accuracy": round(accuracy, 4) if accuracy is not None else None,
                "calibrated_mastery": round(posterior, 4),
                "confidence": confidence,
                "confidence_cn": confidence_cn,
                "latest_attempt_at": max(row["attempted_at"] for row in group),
            }
        )
    knowledge.sort(key=lambda row: (row["calibrated_mastery"], -row["weighted_sample_size"], row["knowledge_point"]))

    assessment_summaries = [
        dict(row)
        for row in conn.execute(
            """
            SELECT ls.session_id,ls.title,ls.started_at,sa.assessment_kind,sa.delivery_mode,
                   sa.reporting_series,sa.raw_score,sa.max_score,sa.duration_seconds,sa.blank_count,
                   awp.evidence_weight,awp.is_calibration_anchor
            FROM learning_sessions ls
            JOIN session_assessments sa ON sa.session_id=ls.session_id
            LEFT JOIN assessment_weight_policies awp
              ON awp.assessment_kind=sa.assessment_kind
             AND awp.delivery_mode=sa.delivery_mode
             AND awp.policy_version=?
            WHERE ls.student_id=? AND ls.record_status='active'
            ORDER BY ls.started_at
            """,
            (POLICY_VERSION, student_id),
        )
    ]
    for row in assessment_summaries:
        row["normalized_score"] = (
            round(float(row["raw_score"]) / float(row["max_score"]), 4)
            if row["raw_score"] is not None and row["max_score"]
            else None
        )

    return {
        "student_id": student_id,
        "generated_at": utc_now(),
        "policy": weight_policy_report(conn),
        "summary": {
            "attempt_count": len(global_seen),
            "weighted_sample_size": round(global_weight, 3),
            "weighted_accuracy": round(global_success / global_weight, 4) if global_weight else None,
            "offline_calibration_accuracy": round(offline_success / offline_weight, 4) if offline_weight else None,
            "practice_accuracy": round(practice_success / practice_weight, 4) if practice_weight else None,
            "calibration_gap": (
                round(offline_success / offline_weight - practice_success / practice_weight, 4)
                if offline_weight and practice_weight
                else None
            ),
            "calibration_anchor_sample": round(offline_weight, 3),
        },
        "knowledge_points": knowledge,
        "assessment_summaries": assessment_summaries,
    }
