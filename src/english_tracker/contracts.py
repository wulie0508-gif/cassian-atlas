from __future__ import annotations

from typing import Any


class ContractError(ValueError):
    pass


THREADS = {"engineering", "dictation", "courseware", "manual", "migration"}
RESULTS = {"correct", "partial", "wrong", "needs_check"}
CAPTURE_STATUSES = {"captured", "captured_blank", "not_captured", "unknown_legacy"}
ASSESSMENT_KINDS = {"lesson", "topic_quiz", "biweekly_mixed_test", "full_exam", "dictation", "homework", "other"}
DELIVERY_MODES = {"offline_closed", "offline_open", "online", "home", "unspecified"}
ERROR_SOURCES = {"student_answer", "teacher_observation", "manual", "legacy", "rule", "model_suggested"}
ERROR_VERIFICATION_STATUSES = {"suggested", "source_checked", "verified", "needs_check", "rejected", "unverified"}


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
    if "assessment" in obj:
        assessment = _require_object(obj["assessment"], "assessment")
        _required(assessment, ["assessment_kind", "reporting_series"], "assessment")
        if assessment["assessment_kind"] not in ASSESSMENT_KINDS:
            raise ContractError(f"assessment_kind must be one of {sorted(ASSESSMENT_KINDS)}")
        if assessment.get("delivery_mode", "unspecified") not in DELIVERY_MODES:
            raise ContractError(f"delivery_mode must be one of {sorted(DELIVERY_MODES)}")
        if assessment.get("raw_score") is not None and assessment.get("max_score") is None:
            raise ContractError("assessment.max_score is required when raw_score is present")
        if assessment.get("max_score") is not None and float(assessment["max_score"]) <= 0:
            raise ContractError("assessment.max_score must be positive")
        if assessment.get("raw_score") is not None and not 0 <= float(assessment["raw_score"]) <= float(assessment["max_score"]):
            raise ContractError("assessment.raw_score must be between zero and max_score")
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
        if attempt["answer_capture_status"] == "not_captured" and attempt.get("student_answer") is not None:
            raise ContractError("not_captured cannot carry a student_answer")
        if attempt["answer_capture_status"] == "not_captured" and attempt.get("error_types"):
            raise ContractError(
                "Specific error_types cannot be recorded when the original answer was not captured; "
                "retain only the evaluation result and answer_capture_status=not_captured"
            )
        if "item_id" not in attempt and "item" not in attempt:
            raise ContractError(f"attempts[{index}] requires item_id or item")
    return obj


def validate_attempt_replacements_payload(payload: Any) -> dict:
    """Validate an audited, item-preserving replacement of existing attempts.

    The target learner, session, item, and original attempt metadata are resolved
    from the ledger.  Callers provide only the corrected evidence and a fresh
    identity for each replacement attempt.
    """
    obj = validate_envelope(payload)
    _required(obj, ["session_id", "replacements"], "payload")
    replacements = obj["replacements"]
    if not isinstance(replacements, list) or not replacements:
        raise ContractError("replacements must be a non-empty array")

    seen_old_attempts: set[str] = set()
    seen_events: set[str] = set()
    seen_attempt_ids: set[str] = set()
    for index, replacement in enumerate(replacements):
        label = f"replacements[{index}]"
        replacement = _require_object(replacement, label)
        _required(
            replacement,
            [
                "old_attempt_id",
                "event_id",
                "answer_capture_status",
                "validation_status",
                "evaluation",
            ],
            label,
        )
        old_attempt_id = str(replacement["old_attempt_id"]).strip()
        event_id = str(replacement["event_id"]).strip()
        if old_attempt_id in seen_old_attempts:
            raise ContractError(f"duplicate old_attempt_id in payload: {old_attempt_id}")
        if event_id in seen_events:
            raise ContractError(f"duplicate replacement event_id in payload: {event_id}")
        seen_old_attempts.add(old_attempt_id)
        seen_events.add(event_id)

        attempt_id = str(replacement.get("attempt_id") or "").strip()
        if attempt_id:
            if attempt_id == old_attempt_id:
                raise ContractError(f"{label}.attempt_id must differ from old_attempt_id")
            if attempt_id in seen_attempt_ids:
                raise ContractError(f"duplicate replacement attempt_id in payload: {attempt_id}")
            seen_attempt_ids.add(attempt_id)

        forbidden = sorted(
            field
            for field in (
                "student_id",
                "session_id",
                "item_id",
                "artifact_id",
                "supersedes_attempt_id",
                "attempted_at",
                "attempt_phase",
                "response_mode",
                "standard_answer",
                "source_material_ref",
            )
            if field in replacement
        )
        if forbidden:
            raise ContractError(
                f"{label} must inherit immutable target fields; remove: {', '.join(forbidden)}"
            )

        capture_status = replacement["answer_capture_status"]
        if capture_status not in CAPTURE_STATUSES:
            raise ContractError(f"invalid answer_capture_status: {capture_status}")
        answer = replacement.get("student_answer")
        if capture_status == "captured" and answer in (None, ""):
            raise ContractError(f"{label}.student_answer is required when answer_capture_status=captured")
        if capture_status == "captured_blank" and answer not in (None, ""):
            raise ContractError(f"{label}.captured_blank must carry an empty student_answer")
        if capture_status == "not_captured" and answer is not None:
            raise ContractError("not_captured cannot carry a student_answer")

        errors = replacement.get("error_types", [])
        if not isinstance(errors, list):
            raise ContractError(f"{label}.error_types must be an array")
        if capture_status == "not_captured" and errors:
            raise ContractError(
                "Specific error_types cannot be recorded when the original answer was not captured"
            )
        if replacement["validation_status"] not in {"verified", "source_checked"}:
            raise ContractError(
                f"{label}.validation_status must be verified or source_checked"
            )

        evaluation = _require_object(replacement["evaluation"], f"{label}.evaluation")
        _required(
            evaluation,
            ["result", "score", "max_score", "is_human_corrected"],
            f"{label}.evaluation",
        )
        if evaluation["is_human_corrected"] is not True:
            raise ContractError(f"{label}.evaluation.is_human_corrected must be true")
        if evaluation["result"] not in RESULTS:
            raise ContractError(f"invalid evaluation result: {evaluation['result']}")
        if evaluation["result"] == "correct" and errors:
            raise ContractError(f"{label}.correct evaluation cannot carry error_types")
        for error_index, error in enumerate(errors):
            if not isinstance(error, dict):
                continue
            source = error.get("error_source", "manual")
            verification = error.get("verification_status", "unverified")
            if source not in ERROR_SOURCES:
                raise ContractError(f"invalid error_source: {source}")
            if verification not in ERROR_VERIFICATION_STATUSES:
                raise ContractError(f"invalid diagnostic verification_status: {verification}")
            if source == "model_suggested" and verification != "suggested":
                raise ContractError(
                    f"{label}.error_types[{error_index}] model_suggested diagnostics must remain suggested"
                )
        try:
            score = float(evaluation["score"])
            max_score = float(evaluation["max_score"])
        except (TypeError, ValueError) as exc:
            raise ContractError(f"{label}.evaluation score fields must be numeric") from exc
        if max_score <= 0:
            raise ContractError(f"{label}.evaluation.max_score must be positive")
        if not 0 <= score <= max_score:
            raise ContractError(
                f"{label}.evaluation.score must be between zero and max_score"
            )
    return obj


def validate_progress_payload(payload: Any) -> dict:
    obj = validate_envelope(payload)
    _required(obj, ["session_id", "progress"], "payload")
    if not isinstance(obj["progress"], list) or not obj["progress"]:
        raise ContractError("progress must be a non-empty array")
    return obj


def validate_diagnostics_payload(payload: Any) -> dict:
    obj = validate_envelope(payload)
    diagnostics = obj.get("diagnostics")
    if not isinstance(diagnostics, list) or not diagnostics:
        raise ContractError("diagnostics must be a non-empty array")
    for index, diagnostic in enumerate(diagnostics):
        diagnostic = _require_object(diagnostic, f"diagnostics[{index}]")
        _required(diagnostic, ["attempt_id", "error_types"], f"diagnostics[{index}]")
        errors = diagnostic["error_types"]
        if not isinstance(errors, list) or not errors:
            raise ContractError(f"diagnostics[{index}].error_types must be a non-empty array")
        for error_index, error in enumerate(errors):
            error = _require_object(error, f"diagnostics[{index}].error_types[{error_index}]")
            if not error.get("code") and not error.get("raw_error_type"):
                raise ContractError("Every diagnostic error requires code or raw_error_type")
            if not str(error.get("rationale") or "").strip():
                raise ContractError("Every diagnostic error requires an evidence-based rationale")
            source = error.get("error_source", "model_suggested")
            verification = error.get("verification_status", "suggested" if source == "model_suggested" else "unverified")
            if source not in ERROR_SOURCES:
                raise ContractError(f"invalid error_source: {source}")
            if verification not in ERROR_VERIFICATION_STATUSES:
                raise ContractError(f"invalid diagnostic verification_status: {verification}")
            confidence = float(error.get("confidence", 1.0))
            if not 0 <= confidence <= 1:
                raise ContractError("diagnostic confidence must be between zero and one")
            if source == "model_suggested" and verification != "suggested":
                raise ContractError("model_suggested diagnostics must remain suggested")
    return obj
