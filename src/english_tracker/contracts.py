from __future__ import annotations

from typing import Any


class ContractError(ValueError):
    pass


THREADS = {"engineering", "dictation", "courseware", "manual", "migration"}
RESULTS = {"correct", "partial", "wrong", "needs_check"}
CAPTURE_STATUSES = {"captured", "captured_blank", "not_captured", "unknown_legacy"}


def _require_object(payload: Any, label: str) -> dict:
    if not isinstance(payload, dict):
        raise ContractError(f"{label} must be a JSON object")
    return payload


def _required(obj: dict, fields: list[str], label: str) -> None:
    missing = [field for field in fields if field not in obj or obj[field] in (None, "")]
    if missing:
        raise ContractError(f"{label} missing required fields: {', '.join(missing)}")


def validate_envelope(payload: Any) -> dict:
    obj = _require_object(payload, "payload")
    _required(obj, ["event_id", "idempotency_key", "source_thread", "student_id"], "payload")
    if obj["source_thread"] not in THREADS:
        raise ContractError(f"source_thread must be one of {sorted(THREADS)}")
    return obj


def validate_session_payload(payload: Any) -> dict:
    obj = validate_envelope(payload)
    session = _require_object(obj.get("session"), "session")
    _required(session, ["session_id", "session_type", "title", "started_at"], "session")
    if "observations" in obj and not isinstance(obj["observations"], list):
        raise ContractError("observations must be an array")
    if "progress" in obj and not isinstance(obj["progress"], list):
        raise ContractError("progress must be an array")
    return obj


def validate_attempts_payload(payload: Any) -> dict:
    obj = validate_envelope(payload)
    _required(obj, ["session_id", "attempts"], "payload")
    if not isinstance(obj["attempts"], list) or not obj["attempts"]:
        raise ContractError("attempts must be a non-empty array")
    seen: set[str] = set()
    for index, attempt in enumerate(obj["attempts"]):
        attempt = _require_object(attempt, f"attempts[{index}]")
        _required(attempt, ["event_id", "attempted_at", "evaluation", "answer_capture_status"], f"attempts[{index}]")
        if attempt["event_id"] in seen:
            raise ContractError(f"duplicate attempt event_id in payload: {attempt['event_id']}")
        seen.add(attempt["event_id"])
        evaluation = _require_object(attempt["evaluation"], f"attempts[{index}].evaluation")
        _required(evaluation, ["result"], f"attempts[{index}].evaluation")
        if evaluation["result"] not in RESULTS:
            raise ContractError(f"invalid evaluation result: {evaluation['result']}")
        if attempt["answer_capture_status"] not in CAPTURE_STATUSES:
            raise ContractError(f"invalid answer_capture_status: {attempt['answer_capture_status']}")
        if "item_id" not in attempt and "item" not in attempt:
            raise ContractError(f"attempts[{index}] requires item_id or item")
    return obj


def validate_progress_payload(payload: Any) -> dict:
    obj = validate_envelope(payload)
    _required(obj, ["session_id", "progress"], "payload")
    if not isinstance(obj["progress"], list) or not obj["progress"]:
        raise ContractError("progress must be a non-empty array")
    return obj

