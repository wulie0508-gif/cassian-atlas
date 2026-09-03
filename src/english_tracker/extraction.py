from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from contextlib import contextmanager
from difflib import SequenceMatcher
from typing import Any

from .ingest import IngestConflict, import_attempts
from .util import canonical_json, payload_hash, stable_id, utc_now


CONTRACT_VERSION = "extraction-v1"
COMPARISON_POLICY_VERSION = "transcription-compare-v1"
RISK_LEVELS = {"R0", "R1", "R2", "R3", "R4"}
SOURCE_THREADS = {"engineering", "dictation", "courseware", "manual", "migration"}
PROVIDER_STATUSES = {"succeeded", "failed", "unconfigured", "timeout", "rate_limited"}
PROVIDERS = {"codex", "doubao", "deterministic"}
CAPTURE_STATUSES = {
    "captured",
    "captured_blank",
    "not_captured",
    "needs_check",
    "blocked_image_quality",
    "blocked_alignment",
}
DECISION_ACTIONS = {
    "pending_review",
    "needs_check",
    "human_confirmed",
    "human_corrected",
    "confirmed_blank",
    "not_captured",
    "rejected_alignment",
}
COMMITTABLE_ACTIONS = {"human_confirmed", "human_corrected", "confirmed_blank"}
TERMINAL_REVIEW_ACTIONS = COMMITTABLE_ACTIONS | {"not_captured", "rejected_alignment"}
FORMAL_RESULTS = {"correct", "partial", "wrong"}


class ExtractionConflict(RuntimeError):
    """Raised when an extraction request conflicts with durable state."""


def _is_sqlite_lock_error(exc: sqlite3.OperationalError) -> bool:
    code = getattr(exc, "sqlite_errorcode", None)
    if isinstance(code, int) and (code & 0xFF) in {
        sqlite3.SQLITE_BUSY,
        sqlite3.SQLITE_LOCKED,
    }:
        return True
    message = str(exc).lower()
    return "database is locked" in message or "database table is locked" in message


@contextmanager
def _extraction_write_transaction(conn: sqlite3.Connection):
    """Serialize extraction mutations while remaining safe inside caller transactions."""

    if conn.in_transaction:
        savepoint = "opentutor_extraction_write"
        conn.execute(f"SAVEPOINT {savepoint}")
        try:
            yield
        except sqlite3.OperationalError as exc:
            conn.execute(f"ROLLBACK TO {savepoint}")
            conn.execute(f"RELEASE {savepoint}")
            if _is_sqlite_lock_error(exc):
                raise ExtractionConflict(
                    "Concurrent extraction write is in progress; retry with the same idempotency_key"
                ) from exc
            raise
        except BaseException:
            conn.execute(f"ROLLBACK TO {savepoint}")
            conn.execute(f"RELEASE {savepoint}")
            raise
        else:
            conn.execute(f"RELEASE {savepoint}")
        return

    try:
        # A reserved write lock makes the idempotency lookup and the insert one
        # indivisible operation across independent CLI/HTTP connections.
        conn.execute("BEGIN IMMEDIATE")
        yield
        conn.commit()
    except sqlite3.OperationalError as exc:
        conn.rollback()
        if _is_sqlite_lock_error(exc):
            raise ExtractionConflict(
                "Concurrent extraction write is in progress; retry with the same idempotency_key"
            ) from exc
        raise
    except BaseException:
        conn.rollback()
        raise


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return dict(value)


def _array(value: Any, label: str, *, nonempty: bool = False) -> list[Any]:
    if not isinstance(value, list) or (nonempty and not value):
        suffix = " non-empty" if nonempty else ""
        raise ValueError(f"{label} must be a{suffix} array")
    return list(value)


def _text(value: Any, label: str, *, required: bool = True) -> str:
    result = str(value or "").strip()
    if required and not result:
        raise ValueError(f"{label} is required")
    return result


def _json(value: str | None, default: Any) -> Any:
    if value is None:
        return default
    return json.loads(value)


def _sha256(value: Any, label: str) -> str:
    result = _text(value, label).lower()
    if not re.fullmatch(r"[0-9a-f]{64}", result):
        raise ValueError(f"{label} must be a 64-character SHA-256 hex digest")
    return result


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


def _batch(
    conn: sqlite3.Connection,
    extraction_batch_id: str,
    *,
    student_id: str,
) -> sqlite3.Row:
    row = conn.execute(
        """
        SELECT * FROM extraction_batches
        WHERE extraction_batch_id=? AND student_id=?
        """,
        (extraction_batch_id, student_id),
    ).fetchone()
    if not row:
        raise ValueError(
            f"Unknown extraction_batch_id for student {student_id}: {extraction_batch_id}"
        )
    return row


def _current_decision(
    conn: sqlite3.Connection,
    extraction_batch_id: str,
    extraction_item_id: str,
) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT d.*
        FROM extraction_confirmation_decisions d
        WHERE d.extraction_batch_id=? AND d.extraction_item_id=?
        ORDER BY d.revision_no DESC
        LIMIT 1
        """,
        (extraction_batch_id, extraction_item_id),
    ).fetchone()


def _normalize_for_compare(text: str | None, policy: dict[str, Any]) -> str:
    result = unicodedata.normalize("NFC", text or "")
    if policy.get("trim", True):
        result = result.strip()
    if policy.get("collapse_whitespace", False):
        result = re.sub(r"\s+", " ", result)
    if policy.get("case_sensitive", True) is False:
        result = result.casefold()
    if policy.get("ignore_terminal_punctuation", False):
        result = re.sub(r"[.!?。！？]+$", "", result).rstrip()
    return result


def _diff_spans(left: str, right: str) -> list[dict[str, Any]]:
    spans: list[dict[str, Any]] = []
    for tag, left_start, left_end, right_start, right_end in SequenceMatcher(
        None, left, right
    ).get_opcodes():
        if tag == "equal":
            continue
        spans.append(
            {
                "kind": tag,
                "left": {"start": left_start, "end": left_end, "text": left[left_start:left_end]},
                "right": {
                    "start": right_start,
                    "end": right_end,
                    "text": right[right_start:right_end],
                },
            }
        )
    return spans


def _active_provider_results(rows: list[sqlite3.Row]) -> list[sqlite3.Row]:
    latest: dict[str, sqlite3.Row] = {}
    for row in rows:
        provider = str(row["provider"])
        if provider not in latest:
            latest[provider] = row
    order = {"deterministic": 0, "codex": 1, "doubao": 2}
    return sorted(latest.values(), key=lambda row: (order.get(str(row["provider"]), 9), str(row["provider"])))


def _provider_rows_for_item(
    conn: sqlite3.Connection,
    extraction_batch_id: str,
    extraction_item_id: str,
) -> list[sqlite3.Row]:
    """Return provider history in the one canonical current-result order."""

    return list(
        conn.execute(
            """
            SELECT p.*,p.rowid AS storage_append_order
            FROM extraction_provider_results p
            WHERE p.extraction_batch_id=? AND p.extraction_item_id=?
            ORDER BY p.rowid DESC
            """,
            (extraction_batch_id, extraction_item_id),
        )
    )


def _comparison(
    item: sqlite3.Row,
    provider_rows: list[sqlite3.Row],
) -> dict[str, Any]:
    active = _active_provider_results(provider_rows)
    successful = [row for row in active if row["result_status"] == "succeeded"]
    failed = [row for row in active if row["result_status"] != "succeeded"]
    if item["second_model_required"]:
        successful = [row for row in successful if row["provider"] in {"codex", "doubao"}]
    template = _json(item["attempt_template_json"], {})
    policy = _object(template.get("transcription_comparison") or {}, "transcription_comparison")
    result: dict[str, Any] = {
        "classification": "missing_candidate",
        "successful_provider_count": len({row["provider"] for row in successful}),
        "failed_provider_count": len(failed),
        "second_model_required": bool(item["second_model_required"]),
        "second_model_ready": (
            not bool(item["second_model_required"])
            or len({row["provider"] for row in successful}) >= 2
        ),
        "diff_spans": [],
    }
    if not successful:
        return result
    if len(successful) == 1:
        row = successful[0]
        result["classification"] = (
            "blocked_second_model"
            if item["second_model_required"]
            else "single_candidate"
        )
        result["prefill_provider_result_id"] = row["provider_result_id"]
        result["prefill_text"] = row["raw_transcription"]
        return result
    left, right = successful[0], successful[1]
    left_status = str(left["capture_status"] or "")
    right_status = str(right["capture_status"] or "")
    if "blocked_alignment" in {left_status, right_status}:
        classification = "alignment_conflict"
    elif (left_status == "captured_blank") != (right_status == "captured_blank") or (
        left_status == "not_captured"
    ) != (right_status == "not_captured"):
        classification = "blank_conflict"
    elif left_status in {"needs_check", "blocked_image_quality"} or right_status in {
        "needs_check",
        "blocked_image_quality",
    }:
        classification = "uncertain"
    else:
        left_raw = str(left["raw_transcription"] or "")
        right_raw = str(right["raw_transcription"] or "")
        if left_raw == right_raw:
            classification = "exact_match"
        elif _normalize_for_compare(left_raw, policy) == _normalize_for_compare(right_raw, policy):
            classification = "ignorable_difference"
        else:
            classification = "content_conflict"
            result["diff_spans"] = _diff_spans(left_raw, right_raw)
    result["classification"] = classification
    if classification in {"exact_match", "ignorable_difference"}:
        result["prefill_provider_result_id"] = left["provider_result_id"]
        result["prefill_text"] = left["raw_transcription"]
    return result


def _normalize_create_payload(payload: dict[str, Any]) -> dict[str, Any]:
    student_id = _text(payload.get("student_id"), "student_id").upper()
    subject_code = _text(payload.get("subject_code") or "english", "subject_code").lower()
    session_id = _text(payload.get("session_id"), "session_id")
    source_thread = _text(payload.get("source_thread") or "courseware", "source_thread")
    if source_thread not in SOURCE_THREADS:
        raise ValueError(f"source_thread must be one of {sorted(SOURCE_THREADS)}")
    assets = _array(payload.get("source_images") or payload.get("assets"), "source_images", nonempty=True)
    items = _array(payload.get("items"), "items", nonempty=True)
    request_base = {
        "contract_version": _text(payload.get("contract_version") or CONTRACT_VERSION, "contract_version"),
        "student_id": student_id,
        "subject_code": subject_code,
        "session_id": session_id,
        "title": _text(payload.get("title") or "Answer extraction", "title"),
        "source_thread": source_thread,
        "comparison_policy_version": _text(
            payload.get("comparison_policy_version") or COMPARISON_POLICY_VERSION,
            "comparison_policy_version",
        ),
        "source_images": assets,
        "items": items,
    }
    provisional_hash = payload_hash(request_base)
    idempotency_key = _text(
        payload.get("idempotency_key") or f"opentutor:extraction:{provisional_hash}:v1",
        "idempotency_key",
    )
    extraction_batch_id = _text(
        payload.get("extraction_batch_id") or stable_id("XBAT", idempotency_key),
        "extraction_batch_id",
    )
    normalized_assets: list[dict[str, Any]] = []
    known_assets: set[str] = set()
    for index, source in enumerate(assets, start=1):
        source = _object(source, f"source_images[{index - 1}]")
        source_key = _text(source.get("source_id") or source.get("extraction_asset_id") or index, "source_id")
        extraction_asset_id = _text(
            source.get("extraction_asset_id") or stable_id("XAST", extraction_batch_id, source_key),
            "extraction_asset_id",
        )
        if extraction_asset_id in known_assets:
            raise ValueError(f"duplicate extraction_asset_id: {extraction_asset_id}")
        known_assets.add(extraction_asset_id)
        source_uri = _text(source.get("source_uri") or source.get("private_path"), "source_uri")
        normalized_assets.append(
            {
                "extraction_asset_id": extraction_asset_id,
                "source_uri": source_uri,
                "sha256": _sha256(source.get("sha256"), "source_images.sha256"),
                "media_type": _text(source.get("media_type") or "application/octet-stream", "media_type"),
                "byte_size": int(source["byte_size"]) if source.get("byte_size") is not None else None,
                "page_number": int(source["page_number"]) if source.get("page_number") is not None else None,
            }
        )
    normalized_items: list[dict[str, Any]] = []
    item_ids: set[str] = set()
    ordinals: set[int] = set()
    for index, raw_item in enumerate(items, start=1):
        raw_item = _object(raw_item, f"items[{index - 1}]")
        ordinal = int(raw_item.get("ordinal") or index)
        if ordinal <= 0 or ordinal in ordinals:
            raise ValueError(f"items[{index - 1}].ordinal must be unique and positive")
        ordinals.add(ordinal)
        item_key = _text(raw_item.get("item_key") or raw_item.get("question_ref") or ordinal, "item_key")
        extraction_item_id = _text(
            raw_item.get("extraction_item_id") or stable_id("XITM", extraction_batch_id, item_key),
            "extraction_item_id",
        )
        if extraction_item_id in item_ids:
            raise ValueError(f"duplicate extraction_item_id: {extraction_item_id}")
        item_ids.add(extraction_item_id)
        risk_level = _text(raw_item.get("risk_level") or "R1", "risk_level").upper()
        if risk_level not in RISK_LEVELS:
            raise ValueError(f"risk_level must be one of {sorted(RISK_LEVELS)}")
        requested_second_model = raw_item.get("second_model_required")
        if requested_second_model is not None and not isinstance(requested_second_model, bool):
            raise ValueError("second_model_required must be a boolean when supplied")
        if risk_level in {"R1", "R2", "R3"} and requested_second_model is False:
            raise ValueError(
                f"{risk_level} items cannot disable the required second model during calibration"
            )
        second_model_required = risk_level in {"R1", "R2", "R3"} or requested_second_model is True
        second_model_reason = _text(raw_item.get("second_model_reason"), "second_model_reason", required=False) or None
        if second_model_required and not second_model_reason:
            second_model_reason = "risk_policy"
        asset_id = raw_item.get("extraction_asset_id") or raw_item.get("source_id")
        if asset_id is not None:
            asset_id = _text(asset_id, "extraction_asset_id")
            if asset_id not in known_assets:
                mapped = stable_id("XAST", extraction_batch_id, asset_id)
                if mapped in known_assets:
                    asset_id = mapped
                else:
                    raise ValueError(f"Unknown extraction_asset_id: {asset_id}")
        template = _object(raw_item.get("attempt_template"), "attempt_template")
        if not template.get("attempted_at"):
            raise ValueError("attempt_template.attempted_at is required")
        if not template.get("item_id") and not isinstance(template.get("item"), dict):
            raise ValueError("attempt_template requires item_id or object item")
        forbidden = {"student_answer", "answer_capture_status", "evaluation", "error_types"}
        present = sorted(key for key in forbidden if key in template)
        if present:
            raise ValueError(
                "attempt_template cannot contain pre-confirmation fact fields: " + ", ".join(present)
            )
        grading_contract = template.get("grading_contract")
        if not isinstance(grading_contract, dict):
            raise ValueError("attempt_template.grading_contract is required")
        normalized_items.append(
            {
                "extraction_item_id": extraction_item_id,
                "extraction_asset_id": asset_id,
                "ordinal": ordinal,
                "question_ref": _text(raw_item.get("question_ref") or item_key, "question_ref"),
                "question_type": _text(raw_item.get("question_type") or "unknown", "question_type"),
                "risk_level": risk_level,
                "second_model_required": second_model_required,
                "second_model_reason": second_model_reason,
                "evidence_locator": _object(raw_item.get("evidence_locator") or {}, "evidence_locator"),
                "attempt_template": template,
            }
        )
    normalized = {
        **request_base,
        "idempotency_key": idempotency_key,
        "extraction_batch_id": extraction_batch_id,
        "source_images": normalized_assets,
        "items": normalized_items,
    }
    normalized["request_sha256"] = payload_hash(
        {key: value for key, value in normalized.items() if key != "request_sha256"}
    )
    return normalized


def create_extraction_batch(
    conn: sqlite3.Connection,
    payload: dict[str, Any],
) -> dict[str, Any]:
    with _extraction_write_transaction(conn):
        return _create_extraction_batch_locked(conn, payload)


def _create_extraction_batch_locked(
    conn: sqlite3.Connection,
    payload: dict[str, Any],
) -> dict[str, Any]:
    normalized = _normalize_create_payload(_object(payload, "payload"))
    _require_enrollment(conn, normalized["student_id"], normalized["subject_code"])
    session = conn.execute(
        """
        SELECT student_id FROM learning_sessions
        WHERE session_id=? AND record_status='active'
        """,
        (normalized["session_id"],),
    ).fetchone()
    if not session or session["student_id"] != normalized["student_id"]:
        raise ValueError("session_id must be active and belong to the same student")
    existing = conn.execute(
        "SELECT * FROM extraction_batches WHERE idempotency_key=?",
        (normalized["idempotency_key"],),
    ).fetchone()
    if existing:
        if existing["student_id"] != normalized["student_id"]:
            raise ExtractionConflict("idempotency_key already belongs to another student")
        if existing["request_sha256"] != normalized["request_sha256"]:
            raise ExtractionConflict(
                "idempotency_key already belongs to a different extraction request"
            )
        return {
            "status": "duplicate",
            "batch": extraction_batch_detail(
                conn,
                existing["extraction_batch_id"],
                student_id=normalized["student_id"],
            ),
        }
    now = utc_now()
    with _extraction_write_transaction(conn):
        conn.execute(
            """
            INSERT INTO extraction_batches(
              extraction_batch_id,idempotency_key,request_sha256,contract_version,
              student_id,subject_code,session_id,title,source_thread,status,
              expected_item_count,review_version,comparison_policy_version,
              created_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,'draft',?,1,?,?,?)
            """,
            (
                normalized["extraction_batch_id"],
                normalized["idempotency_key"],
                normalized["request_sha256"],
                normalized["contract_version"],
                normalized["student_id"],
                normalized["subject_code"],
                normalized["session_id"],
                normalized["title"],
                normalized["source_thread"],
                len(normalized["items"]),
                normalized["comparison_policy_version"],
                now,
                now,
            ),
        )
        for source in normalized["source_images"]:
            conn.execute(
                """
                INSERT INTO extraction_assets(
                  extraction_asset_id,extraction_batch_id,source_uri,sha256,
                  media_type,byte_size,page_number,created_at
                ) VALUES (?,?,?,?,?,?,?,?)
                """,
                (
                    source["extraction_asset_id"],
                    normalized["extraction_batch_id"],
                    source["source_uri"],
                    source["sha256"],
                    source["media_type"],
                    source["byte_size"],
                    source["page_number"],
                    now,
                ),
            )
        for item in normalized["items"]:
            template_json = canonical_json(item["attempt_template"])
            conn.execute(
                """
                INSERT INTO extraction_items(
                  extraction_item_id,extraction_batch_id,extraction_asset_id,ordinal,
                  question_ref,question_type,risk_level,second_model_required,
                  second_model_reason,evidence_locator_json,attempt_template_json,
                  template_sha256,created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    item["extraction_item_id"],
                    normalized["extraction_batch_id"],
                    item["extraction_asset_id"],
                    item["ordinal"],
                    item["question_ref"],
                    item["question_type"],
                    item["risk_level"],
                    int(item["second_model_required"]),
                    item["second_model_reason"],
                    canonical_json(item["evidence_locator"]),
                    template_json,
                    payload_hash(item["attempt_template"]),
                    now,
                ),
            )
    return {
        "status": "created",
        "batch": extraction_batch_detail(
            conn,
            normalized["extraction_batch_id"],
            student_id=normalized["student_id"],
        ),
    }


def _provider_result_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "provider_result_id": row["provider_result_id"],
        "provider": row["provider"],
        "model_version": row["model_version"],
        "prompt_version": row["prompt_version"],
        "request_sha256": row["request_sha256"],
        "result_status": row["result_status"],
        "raw_transcription": row["raw_transcription"],
        "normalized_transcription": row["normalized_transcription"],
        "capture_status": row["capture_status"],
        "uncertain_spans": _json(row["uncertain_spans_json"], []),
        "candidate_alternatives": _json(row["candidate_alternatives_json"], []),
        "confidence": row["confidence"],
        "evidence_locator": _json(row["evidence_locator_json"], {}),
        "response_sha256": row["response_sha256"],
        "error_summary": row["error_summary"],
        "completed_at": row["completed_at"],
        "created_at": row["created_at"],
    }


def submit_provider_results(
    conn: sqlite3.Connection,
    extraction_batch_id: str,
    payload: dict[str, Any],
    *,
    student_id: str,
) -> dict[str, Any]:
    with _extraction_write_transaction(conn):
        return _submit_provider_results_locked(
            conn,
            extraction_batch_id,
            payload,
            student_id=student_id,
        )


def _submit_provider_results_locked(
    conn: sqlite3.Connection,
    extraction_batch_id: str,
    payload: dict[str, Any],
    *,
    student_id: str,
) -> dict[str, Any]:
    payload = _object(payload, "payload")
    student_id = _text(student_id, "student_id").upper()
    batch = _batch(conn, extraction_batch_id, student_id=student_id)
    if batch["status"] in {"committed", "cancelled", "failed"}:
        raise ExtractionConflict(f"Extraction batch is terminal: {batch['status']}")
    submission_key = _text(payload.get("idempotency_key"), "idempotency_key")
    provider = _text(payload.get("provider"), "provider").lower()
    if provider not in PROVIDERS:
        raise ValueError(f"provider must be one of {sorted(PROVIDERS)}")
    model_version = _text(payload.get("model_version") or "unspecified", "model_version")
    prompt_version = _text(payload.get("prompt_version") or "unspecified", "prompt_version")
    completed_at = _text(payload.get("completed_at") or utc_now(), "completed_at")
    raw_results = _array(payload.get("results"), "results", nonempty=True)
    normalized_results: list[dict[str, Any]] = []
    seen_items: set[str] = set()
    for index, value in enumerate(raw_results):
        result = _object(value, f"results[{index}]")
        extraction_item_id = _text(
            result.get("extraction_item_id") or result.get("item_id"),
            "extraction_item_id",
        )
        if extraction_item_id in seen_items:
            raise ValueError(f"duplicate extraction_item_id in results: {extraction_item_id}")
        seen_items.add(extraction_item_id)
        item = conn.execute(
            """
            SELECT * FROM extraction_items
            WHERE extraction_batch_id=? AND extraction_item_id=?
            """,
            (extraction_batch_id, extraction_item_id),
        ).fetchone()
        if not item:
            raise ValueError(f"Unknown extraction_item_id in batch: {extraction_item_id}")
        current = _current_decision(conn, extraction_batch_id, extraction_item_id)
        if current and current["action"] in TERMINAL_REVIEW_ACTIONS:
            raise ExtractionConflict(
                f"Item {extraction_item_id} already has a terminal human decision; create a new batch for new model evidence"
            )
        status = _text(result.get("result_status") or "succeeded", "result_status")
        if status not in PROVIDER_STATUSES:
            raise ValueError(f"result_status must be one of {sorted(PROVIDER_STATUSES)}")
        capture_status = result.get("capture_status")
        if capture_status is not None:
            capture_status = _text(capture_status, "capture_status")
            if capture_status not in CAPTURE_STATUSES:
                raise ValueError(f"capture_status must be one of {sorted(CAPTURE_STATUSES)}")
        raw_transcription = result.get("raw_transcription")
        normalized_transcription = result.get("normalized_transcription")
        if raw_transcription is not None:
            raw_transcription = str(raw_transcription)
        if normalized_transcription is not None:
            normalized_transcription = str(normalized_transcription)
        if status == "succeeded":
            if not capture_status:
                raise ValueError("succeeded provider results require capture_status")
            if capture_status == "captured" and raw_transcription is None:
                raise ValueError("captured provider results require raw_transcription")
            if capture_status == "captured_blank" and (raw_transcription or "") != "":
                raise ValueError("captured_blank provider results require an empty transcription")
        elif not _text(result.get("error_summary"), "error_summary", required=False):
            raise ValueError("unsuccessful provider results require error_summary")
        confidence = result.get("confidence")
        if confidence is not None:
            confidence = float(confidence)
            if not 0 <= confidence <= 1:
                raise ValueError("confidence must be between 0 and 1")
        raw_output = result.get("raw_output")
        response_sha256 = result.get("response_sha256")
        if response_sha256 is None and raw_output is not None:
            response_sha256 = payload_hash(raw_output)
        if response_sha256 is not None:
            response_sha256 = _sha256(response_sha256, "response_sha256")
        request_sha256 = _sha256(
            result.get("request_sha256") or payload.get("request_sha256"),
            "request_sha256",
        )
        row_key = f"{submission_key}:{extraction_batch_id}:{extraction_item_id}"
        normalized_results.append(
            {
                "provider_result_id": _text(
                    result.get("provider_result_id") or stable_id("XPR", row_key),
                    "provider_result_id",
                ),
                "idempotency_key": row_key,
                "extraction_item_id": extraction_item_id,
                "request_sha256": request_sha256,
                "result_status": status,
                "raw_transcription": raw_transcription,
                "normalized_transcription": normalized_transcription,
                "capture_status": capture_status,
                "uncertain_spans": _array(result.get("uncertain_spans") or [], "uncertain_spans"),
                "candidate_alternatives": _array(
                    result.get("candidate_alternatives") or [], "candidate_alternatives"
                ),
                "confidence": confidence,
                "evidence_locator": _object(result.get("evidence_locator") or {}, "evidence_locator"),
                "raw_output": raw_output,
                "response_sha256": response_sha256,
                "error_summary": (
                    _text(result.get("error_summary"), "error_summary", required=False) or None
                ),
            }
        )
    normalized_submission = {
        "student_id": student_id,
        "extraction_batch_id": extraction_batch_id,
        "idempotency_key": submission_key,
        "provider": provider,
        "model_version": model_version,
        "prompt_version": prompt_version,
        # A server-generated completion time is metadata, not part of the
        # caller's idempotent request. This keeps retries stable across seconds.
        "completed_at": completed_at if payload.get("completed_at") else None,
        "results": normalized_results,
    }
    submission_sha256 = payload_hash(normalized_submission)
    existing = conn.execute(
        """
        SELECT extraction_batch_id,submission_sha256
        FROM extraction_provider_results
        WHERE submission_idempotency_key=?
        LIMIT 1
        """,
        (submission_key,),
    ).fetchone()
    if existing:
        if existing["extraction_batch_id"] != extraction_batch_id:
            raise ExtractionConflict(
                "provider submission idempotency_key already belongs to another batch"
            )
        if existing["submission_sha256"] != submission_sha256:
            raise ExtractionConflict(
                "provider submission idempotency_key has a different payload"
            )
        return {
            "status": "duplicate",
            "extraction_batch_id": extraction_batch_id,
            "review": extraction_review(conn, extraction_batch_id, student_id=student_id),
        }
    now = utc_now()
    with _extraction_write_transaction(conn):
        for result in normalized_results:
            conn.execute(
                """
                INSERT INTO extraction_provider_results(
                  provider_result_id,idempotency_key,submission_idempotency_key,
                  submission_sha256,extraction_batch_id,extraction_item_id,
                  provider,model_version,prompt_version,request_sha256,result_status,
                  raw_transcription,normalized_transcription,capture_status,
                  uncertain_spans_json,candidate_alternatives_json,confidence,
                  evidence_locator_json,raw_output_json,response_sha256,error_summary,
                  completed_at,created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    result["provider_result_id"],
                    result["idempotency_key"],
                    submission_key,
                    submission_sha256,
                    extraction_batch_id,
                    result["extraction_item_id"],
                    provider,
                    model_version,
                    prompt_version,
                    result["request_sha256"],
                    result["result_status"],
                    result["raw_transcription"],
                    result["normalized_transcription"],
                    result["capture_status"],
                    canonical_json(result["uncertain_spans"]),
                    canonical_json(result["candidate_alternatives"]),
                    result["confidence"],
                    canonical_json(result["evidence_locator"]),
                    canonical_json(result["raw_output"]) if result["raw_output"] is not None else None,
                    result["response_sha256"],
                    result["error_summary"],
                    completed_at,
                    now,
                ),
            )
        conn.execute(
            """
            UPDATE extraction_batches
            SET status='pending_review',review_version=review_version+1,updated_at=?
            WHERE extraction_batch_id=?
            """,
            (now, extraction_batch_id),
        )
    return {
        "status": "created",
        "extraction_batch_id": extraction_batch_id,
        "provider_results_inserted": len(normalized_results),
        "review": extraction_review(conn, extraction_batch_id, student_id=student_id),
    }


def _decision_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "confirmation_decision_id": row["confirmation_decision_id"],
        "revision_no": row["revision_no"],
        "review_version": row["review_version"],
        "action": row["action"],
        "confirmed_text": row["confirmed_text"],
        "selected_provider_result_id": row["selected_provider_result_id"],
        "evaluation": _json(row["evaluation_json"], None),
        "actor": row["actor"],
        "reason": row["reason"],
        "decided_at": row["decided_at"],
    }


def _item_rows(conn: sqlite3.Connection, extraction_batch_id: str) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """
            SELECT i.*,a.source_uri,a.sha256 AS source_sha256,a.media_type,a.page_number
            FROM extraction_items i
            LEFT JOIN extraction_assets a
              ON a.extraction_batch_id=i.extraction_batch_id
             AND a.extraction_asset_id=i.extraction_asset_id
            WHERE i.extraction_batch_id=?
            ORDER BY i.ordinal,i.extraction_item_id
            """,
            (extraction_batch_id,),
        )
    )


def extraction_review(
    conn: sqlite3.Connection,
    extraction_batch_id: str,
    *,
    student_id: str,
) -> dict[str, Any]:
    student_id = _text(student_id, "student_id").upper()
    batch = _batch(conn, extraction_batch_id, student_id=student_id)
    items: list[dict[str, Any]] = []
    for item in _item_rows(conn, extraction_batch_id):
        provider_rows = _provider_rows_for_item(
            conn,
            extraction_batch_id,
            item["extraction_item_id"],
        )
        comparison = _comparison(item, provider_rows)
        decision = _current_decision(conn, extraction_batch_id, item["extraction_item_id"])
        decision_value = _decision_dict(decision)
        terminal = bool(decision and decision["action"] in TERMINAL_REVIEW_ACTIONS)
        ordinary = comparison["classification"] in {
            "single_candidate",
            "exact_match",
            "ignorable_difference",
        } and comparison["second_model_ready"]
        items.append(
            {
                "extraction_item_id": item["extraction_item_id"],
                "ordinal": item["ordinal"],
                "question_ref": item["question_ref"],
                "question_type": item["question_type"],
                "risk_level": item["risk_level"],
                "second_model_required": bool(item["second_model_required"]),
                "second_model_reason": item["second_model_reason"],
                "evidence": {
                    "source_uri": item["source_uri"],
                    "source_sha256": item["source_sha256"],
                    "media_type": item["media_type"],
                    "page_number": item["page_number"],
                    "locator": _json(item["evidence_locator_json"], {}),
                },
                "provider_results": [_provider_result_dict(row) for row in provider_rows],
                "comparison": comparison,
                "review_group": "ordinary" if ordinary else "attention",
                "decision": decision_value,
                "terminally_reviewed": terminal,
            }
        )
    counts = {
        "total": len(items),
        "ordinary": sum(item["review_group"] == "ordinary" for item in items),
        "attention": sum(item["review_group"] == "attention" for item in items),
        "pending": sum(not item["terminally_reviewed"] for item in items),
        "human_confirmed": sum(
            (item["decision"] or {}).get("action") == "human_confirmed" for item in items
        ),
        "human_corrected": sum(
            (item["decision"] or {}).get("action") == "human_corrected" for item in items
        ),
        "confirmed_blank": sum(
            (item["decision"] or {}).get("action") == "confirmed_blank" for item in items
        ),
        "not_captured": sum(
            (item["decision"] or {}).get("action") == "not_captured" for item in items
        ),
        "rejected_alignment": sum(
            (item["decision"] or {}).get("action") == "rejected_alignment" for item in items
        ),
        "provider_failures": sum(
            result["result_status"] != "succeeded"
            for item in items
            for result in item["provider_results"]
        ),
    }
    readiness = _all_item_readiness(
        conn, extraction_batch_id, raise_on_error=False
    )
    return {
        "contract_version": batch["contract_version"],
        "comparison_policy_version": batch["comparison_policy_version"],
        "extraction_batch_id": extraction_batch_id,
        "student_id": student_id,
        "subject_code": batch["subject_code"],
        "session_id": batch["session_id"],
        "status": batch["status"],
        "review_version": batch["review_version"],
        "counts": counts,
        "can_commit": (
            counts["pending"] == 0
            and bool(readiness)
            and all(result["ready"] for result in readiness)
            and any(result["committable"] for result in readiness)
        ),
        "standard_answers_hidden": True,
        "items": items,
    }


def _validate_evaluation(value: Any) -> dict[str, Any]:
    evaluation = _object(value, "evaluation")
    result = _text(evaluation.get("result"), "evaluation.result")
    if result not in FORMAL_RESULTS:
        raise ValueError(f"evaluation.result must be one of {sorted(FORMAL_RESULTS)}")
    score = evaluation.get("score")
    max_score = evaluation.get("max_score")
    if score is not None:
        score = float(score)
    if max_score is not None:
        max_score = float(max_score)
        if max_score <= 0:
            raise ValueError("evaluation.max_score must be positive")
    if score is not None and max_score is not None and not 0 <= score <= max_score:
        raise ValueError("evaluation.score must be between zero and max_score")
    return {
        "result": result,
        "score": score,
        "max_score": max_score,
        "evaluated_by": _text(
            evaluation.get("evaluated_by") or "teacher", "evaluation.evaluated_by"
        ),
        "is_human_corrected": bool(evaluation.get("is_human_corrected", False)),
        "note": _text(evaluation.get("note"), "evaluation.note", required=False) or None,
    }


def _selected_provider_result(
    conn: sqlite3.Connection,
    extraction_batch_id: str,
    extraction_item_id: str,
    provider_result_id: str,
) -> sqlite3.Row:
    row = conn.execute(
        """
        SELECT * FROM extraction_provider_results
        WHERE provider_result_id=? AND extraction_batch_id=? AND extraction_item_id=?
        """,
        (provider_result_id, extraction_batch_id, extraction_item_id),
    ).fetchone()
    if not row:
        raise ValueError(
            f"selected_provider_result_id does not belong to item {extraction_item_id}"
        )
    if row["result_status"] != "succeeded":
        raise ValueError("selected_provider_result_id must reference a succeeded result")
    return row


def _grade_text(
    template: dict[str, Any],
    confirmed_text: str,
) -> dict[str, Any]:
    contract = _object(template.get("grading_contract"), "grading_contract")
    mode = _text(contract.get("mode"), "grading_contract.mode")
    if mode not in {"deterministic_exact", "exact"}:
        raise ValueError("teacher-confirmed evaluation is required for this grading contract")
    acceptable = contract.get("acceptable_answers")
    if acceptable is None:
        standard_answer = template.get("standard_answer")
        if standard_answer is None and isinstance(template.get("item"), dict):
            standard_answer = template["item"].get("answer_snapshot")
        acceptable = [standard_answer] if standard_answer is not None else []
    acceptable = _array(acceptable, "grading_contract.acceptable_answers", nonempty=True)
    policy = {
        "trim": bool(contract.get("trim", True)),
        "collapse_whitespace": bool(contract.get("collapse_whitespace", False)),
        "case_sensitive": bool(contract.get("case_sensitive", True)),
        "ignore_terminal_punctuation": bool(
            contract.get("ignore_terminal_punctuation", False)
        ),
    }
    observed = _normalize_for_compare(confirmed_text, policy)
    expected = {_normalize_for_compare(str(answer), policy) for answer in acceptable}
    correct = observed in expected
    max_score = float(contract.get("max_score", 1))
    if max_score <= 0:
        raise ValueError("grading_contract.max_score must be positive")
    return {
        "result": "correct" if correct else "wrong",
        "score": max_score if correct else 0.0,
        "max_score": max_score,
        "evaluated_by": "deterministic",
        "is_human_corrected": False,
        "note": _text(contract.get("note"), "grading_contract.note", required=False) or None,
    }


def _effective_evaluation(
    item: sqlite3.Row,
    decision: sqlite3.Row,
) -> dict[str, Any]:
    if decision["action"] not in COMMITTABLE_ACTIONS:
        raise ValueError("Only committable human decisions can be evaluated")
    explicit = _json(decision["evaluation_json"], None)
    if explicit is not None:
        return _validate_evaluation(explicit)
    template = _json(item["attempt_template_json"], {})
    return _grade_text(template, str(decision["confirmed_text"] or ""))


def _item_commit_readiness(
    conn: sqlite3.Connection,
    extraction_batch_id: str,
    extraction_item_id: str,
    *,
    raise_on_error: bool,
) -> dict[str, Any]:
    item = conn.execute(
        """
        SELECT * FROM extraction_items
        WHERE extraction_batch_id=? AND extraction_item_id=?
        """,
        (extraction_batch_id, extraction_item_id),
    ).fetchone()
    if not item:
        message = f"Unknown extraction item: {extraction_item_id}"
        if raise_on_error:
            raise ValueError(message)
        return {"ready": False, "reason": message, "committable": False}
    decision = _current_decision(conn, extraction_batch_id, extraction_item_id)
    if not decision or decision["action"] not in TERMINAL_REVIEW_ACTIONS:
        message = f"Item {extraction_item_id} does not have a terminal human decision"
        if raise_on_error:
            raise ExtractionConflict(message)
        return {"ready": False, "reason": message, "committable": False}
    if decision["action"] in {"not_captured", "rejected_alignment"}:
        return {
            "ready": True,
            "reason": None,
            "committable": False,
            "decision": decision,
            "item": item,
        }
    active_provider_rows = _active_provider_results(
        _provider_rows_for_item(conn, extraction_batch_id, extraction_item_id)
    )
    succeeded_providers = {
        str(row["provider"])
        for row in active_provider_rows
        if row["result_status"] == "succeeded"
    }
    if item["second_model_required"] and not {"codex", "doubao"}.issubset(succeeded_providers):
        message = f"Item {extraction_item_id} requires successful independent Codex and Doubao results"
        if raise_on_error:
            raise ExtractionConflict(message)
        return {"ready": False, "reason": message, "committable": True}
    try:
        evaluation = _effective_evaluation(item, decision)
    except (ValueError, KeyError) as exc:
        if raise_on_error:
            raise
        return {"ready": False, "reason": str(exc), "committable": True}
    return {
        "ready": True,
        "reason": None,
        "committable": True,
        "decision": decision,
        "item": item,
        "evaluation": evaluation,
    }


def _all_item_readiness(
    conn: sqlite3.Connection,
    extraction_batch_id: str,
    *,
    raise_on_error: bool,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for row in conn.execute(
        """
        SELECT extraction_item_id FROM extraction_items
        WHERE extraction_batch_id=? ORDER BY ordinal,extraction_item_id
        """,
        (extraction_batch_id,),
    ):
        results.append(
            _item_commit_readiness(
                conn,
                extraction_batch_id,
                row["extraction_item_id"],
                raise_on_error=raise_on_error,
            )
        )
    return results


def submit_human_decisions(
    conn: sqlite3.Connection,
    extraction_batch_id: str,
    payload: dict[str, Any],
    *,
    student_id: str,
) -> dict[str, Any]:
    with _extraction_write_transaction(conn):
        return _submit_human_decisions_locked(
            conn,
            extraction_batch_id,
            payload,
            student_id=student_id,
        )


def _submit_human_decisions_locked(
    conn: sqlite3.Connection,
    extraction_batch_id: str,
    payload: dict[str, Any],
    *,
    student_id: str,
) -> dict[str, Any]:
    payload = _object(payload, "payload")
    student_id = _text(student_id, "student_id").upper()
    batch = _batch(conn, extraction_batch_id, student_id=student_id)
    if batch["status"] in {"committed", "cancelled", "failed"}:
        raise ExtractionConflict(f"Extraction batch is terminal: {batch['status']}")
    submission_key = _text(payload.get("idempotency_key"), "idempotency_key")
    submission_sha256 = payload_hash(
        {
            "student_id": student_id,
            "extraction_batch_id": extraction_batch_id,
            "payload": payload,
        }
    )
    existing = conn.execute(
        """
        SELECT extraction_batch_id,submission_sha256
        FROM extraction_confirmation_decisions
        WHERE submission_idempotency_key=?
        LIMIT 1
        """,
        (submission_key,),
    ).fetchone()
    if existing:
        if existing["extraction_batch_id"] != extraction_batch_id:
            raise ExtractionConflict(
                "decision submission idempotency_key already belongs to another batch"
            )
        if existing["submission_sha256"] != submission_sha256:
            raise ExtractionConflict(
                "decision submission idempotency_key has a different payload"
            )
        return {
            "status": "duplicate",
            "extraction_batch_id": extraction_batch_id,
            "review": extraction_review(conn, extraction_batch_id, student_id=student_id),
        }
    expected_version = int(payload.get("expected_review_version") or 0)
    if expected_version != int(batch["review_version"]):
        raise ExtractionConflict(
            f"review_version changed: expected {expected_version}, current {batch['review_version']}"
        )
    actor = _text(payload.get("actor"), "actor")
    decisions = _array(payload.get("decisions") or [], "decisions")
    default_action = _text(
        payload.get("default_action"), "default_action", required=False
    ) or None
    if default_action not in {None, "accept_prefill"}:
        raise ValueError("default_action must be accept_prefill when supplied")
    explicit: dict[str, dict[str, Any]] = {}
    for index, value in enumerate(decisions):
        decision = _object(value, f"decisions[{index}]")
        item_id = _text(
            decision.get("extraction_item_id") or decision.get("item_id"),
            "extraction_item_id",
        )
        if item_id in explicit:
            raise ValueError(f"duplicate decision for extraction_item_id: {item_id}")
        explicit[item_id] = decision
    review = extraction_review(conn, extraction_batch_id, student_id=student_id)
    review_items = {item["extraction_item_id"]: item for item in review["items"]}
    unknown = sorted(set(explicit) - set(review_items))
    if unknown:
        raise ValueError("Unknown extraction_item_id(s): " + ", ".join(unknown))
    expanded: list[dict[str, Any]] = []
    for item_id, review_item in review_items.items():
        raw = explicit.get(item_id)
        if raw is None and default_action == "accept_prefill":
            prior = review_item.get("decision") or {}
            if prior.get("action") in TERMINAL_REVIEW_ACTIONS:
                continue
            comparison = review_item["comparison"]
            if review_item["review_group"] != "ordinary" or not comparison.get(
                "prefill_provider_result_id"
            ):
                raise ExtractionConflict(
                    f"Item {item_id} requires an explicit decision and cannot use default_action"
                )
            raw = {
                "action": "human_confirmed",
                "selected_provider_result_id": comparison["prefill_provider_result_id"],
            }
        if raw is None:
            continue
        action = _text(raw.get("action"), "decision.action")
        if action not in DECISION_ACTIONS:
            raise ValueError(f"decision.action must be one of {sorted(DECISION_ACTIONS)}")
        selected_id = _text(
            raw.get("selected_provider_result_id"),
            "selected_provider_result_id",
            required=False,
        ) or None
        selected = (
            _selected_provider_result(conn, extraction_batch_id, item_id, selected_id)
            if selected_id
            else None
        )
        confirmed_text = raw.get("confirmed_text")
        if confirmed_text is not None:
            confirmed_text = str(confirmed_text)
        if action == "human_confirmed":
            if selected is None:
                prefill_id = review_item["comparison"].get("prefill_provider_result_id")
                if not prefill_id:
                    raise ValueError(
                        f"human_confirmed requires selected_provider_result_id for item {item_id}"
                    )
                selected_id = prefill_id
                selected = _selected_provider_result(
                    conn, extraction_batch_id, item_id, selected_id
                )
            candidates = {
                str(selected["raw_transcription"] or ""),
                str(selected["normalized_transcription"] or ""),
            }
            if confirmed_text is None:
                confirmed_text = str(selected["raw_transcription"] or "")
            if confirmed_text not in candidates:
                raise ValueError(
                    "human_confirmed text must equal the selected model candidate; use human_corrected for edits"
                )
        elif action == "human_corrected":
            if confirmed_text is None:
                raise ValueError("human_corrected requires confirmed_text")
        elif action == "confirmed_blank":
            if confirmed_text not in {None, ""}:
                raise ValueError("confirmed_blank cannot carry non-empty text")
            confirmed_text = ""
        else:
            if confirmed_text is not None:
                raise ValueError(f"{action} cannot carry confirmed_text")
            if raw.get("evaluation") is not None:
                raise ValueError(f"{action} cannot carry an evaluation")
        evaluation = (
            _validate_evaluation(raw["evaluation"])
            if raw.get("evaluation") is not None
            else None
        )
        current = _current_decision(conn, extraction_batch_id, item_id)
        revision_no = int(current["revision_no"] if current else 0) + 1
        expanded.append(
            {
                "confirmation_decision_id": stable_id(
                    "XDEC", extraction_batch_id, item_id, revision_no, submission_key
                ),
                "extraction_item_id": item_id,
                "revision_no": revision_no,
                "action": action,
                "confirmed_text": confirmed_text,
                "selected_provider_result_id": selected_id,
                "evaluation": evaluation,
                "reason": _text(raw.get("reason"), "reason", required=False) or None,
            }
        )
    if not expanded:
        raise ValueError("decisions or default_action must produce at least one decision")
    now = utc_now()
    new_review_version = expected_version + 1
    with _extraction_write_transaction(conn):
        for decision in expanded:
            conn.execute(
                """
                INSERT INTO extraction_confirmation_decisions(
                  confirmation_decision_id,submission_idempotency_key,submission_sha256,
                  extraction_batch_id,extraction_item_id,revision_no,review_version,
                  action,confirmed_text,selected_provider_result_id,evaluation_json,
                  actor,reason,decided_at,created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    decision["confirmation_decision_id"],
                    submission_key,
                    submission_sha256,
                    extraction_batch_id,
                    decision["extraction_item_id"],
                    decision["revision_no"],
                    new_review_version,
                    decision["action"],
                    decision["confirmed_text"],
                    decision["selected_provider_result_id"],
                    canonical_json(decision["evaluation"])
                    if decision["evaluation"] is not None
                    else None,
                    actor,
                    decision["reason"],
                    now,
                    now,
                ),
            )
        readiness = _all_item_readiness(
            conn, extraction_batch_id, raise_on_error=False
        )
        next_status = (
            "ready_to_commit"
            if (
                readiness
                and all(result["ready"] for result in readiness)
                and any(result["committable"] for result in readiness)
            )
            else "pending_review"
        )
        conn.execute(
            """
            UPDATE extraction_batches
            SET status=?,review_version=?,updated_at=?
            WHERE extraction_batch_id=?
            """,
            (next_status, new_review_version, now, extraction_batch_id),
        )
    return {
        "status": "applied",
        "extraction_batch_id": extraction_batch_id,
        "decisions_inserted": len(expanded),
        "review": extraction_review(conn, extraction_batch_id, student_id=student_id),
    }


def _attempt_from_readiness(
    extraction_batch_id: str,
    readiness: dict[str, Any],
) -> dict[str, Any]:
    item: sqlite3.Row = readiness["item"]
    decision: sqlite3.Row = readiness["decision"]
    template = dict(_json(item["attempt_template_json"], {}))
    template.pop("grading_contract", None)
    template.pop("transcription_comparison", None)
    template.pop("attempt_id", None)
    template.pop("event_id", None)
    template.pop("student_answer", None)
    template.pop("answer_capture_status", None)
    template.pop("evaluation", None)
    template.pop("error_types", None)
    event_id = stable_id(
        "XATT",
        extraction_batch_id,
        item["extraction_item_id"],
        decision["confirmation_decision_id"],
    )
    action = str(decision["action"])
    template.update(
        {
            "event_id": event_id,
            "student_answer": str(decision["confirmed_text"] or ""),
            "answer_capture_status": (
                "captured_blank" if action == "confirmed_blank" else "captured"
            ),
            "evaluation": readiness["evaluation"],
            "error_types": [],
        }
    )
    template.setdefault("validation_status", "verified")
    template.setdefault(
        "source_material_ref",
        f"extraction:{extraction_batch_id}:{item['extraction_item_id']}",
    )
    return template


def _commit_readback(
    conn: sqlite3.Connection,
    extraction_batch_id: str,
) -> dict[str, Any]:
    rows = [
        dict(row)
        for row in conn.execute(
            """
            SELECT l.extraction_item_id,l.confirmation_decision_id,l.attempt_id,
                   l.evaluation_id,l.ingest_event_id,l.committed_at,
                   a.event_id,a.answer_capture_status,a.student_answer,
                   e.result,e.score,e.max_score,e.evaluated_by
            FROM extraction_commit_links l
            JOIN attempts a ON a.attempt_id=l.attempt_id AND a.record_status='active'
            JOIN evaluations e ON e.evaluation_id=l.evaluation_id AND e.is_current=1
            WHERE l.extraction_batch_id=?
            ORDER BY l.extraction_item_id
            """,
            (extraction_batch_id,),
        )
    ]
    return {
        "count": len(rows),
        "payload_sha256": payload_hash(rows),
        "items": rows,
    }


def commit_extraction_batch(
    conn: sqlite3.Connection,
    extraction_batch_id: str,
    payload: dict[str, Any],
    *,
    student_id: str,
    backup_path: str | None = None,
) -> dict[str, Any]:
    with _extraction_write_transaction(conn):
        return _commit_extraction_batch_locked(
            conn,
            extraction_batch_id,
            payload,
            student_id=student_id,
            backup_path=backup_path,
        )


def _commit_extraction_batch_locked(
    conn: sqlite3.Connection,
    extraction_batch_id: str,
    payload: dict[str, Any],
    *,
    student_id: str,
    backup_path: str | None = None,
) -> dict[str, Any]:
    payload = _object(payload, "payload")
    student_id = _text(student_id, "student_id").upper()
    batch = _batch(conn, extraction_batch_id, student_id=student_id)
    commit_key = _text(
        payload.get("idempotency_key")
        or f"opentutor:extraction:{extraction_batch_id}:commit:v1",
        "idempotency_key",
    )
    expected_review_version = int(payload.get("expected_review_version") or 0)
    actor = _text(payload.get("actor") or "teacher", "actor")
    commit_request = {
        "student_id": student_id,
        "extraction_batch_id": extraction_batch_id,
        "idempotency_key": commit_key,
        "expected_review_version": expected_review_version,
        "actor": actor,
    }
    commit_sha256 = payload_hash(commit_request)
    if batch["status"] == "committed":
        if batch["commit_idempotency_key"] != commit_key:
            raise ExtractionConflict("Extraction batch already committed with another idempotency_key")
        if batch["commit_request_sha256"] != commit_sha256:
            raise ExtractionConflict("Commit idempotency_key has a different payload")
        return {
            "status": "duplicate",
            "extraction_batch_id": extraction_batch_id,
            "ingest_event_id": batch["committed_ingest_event_id"],
            "readback": _commit_readback(conn, extraction_batch_id),
            "batch": extraction_batch_detail(
                conn, extraction_batch_id, student_id=student_id
            ),
        }
    if batch["status"] in {"cancelled", "failed"}:
        raise ExtractionConflict(f"Extraction batch is terminal: {batch['status']}")
    if expected_review_version != int(batch["review_version"]):
        raise ExtractionConflict(
            f"review_version changed: expected {expected_review_version}, current {batch['review_version']}"
        )
    readiness = _all_item_readiness(
        conn, extraction_batch_id, raise_on_error=True
    )
    if len(readiness) != int(batch["expected_item_count"]):
        raise ExtractionConflict("Extraction item coverage does not match expected_item_count")
    committable = [result for result in readiness if result["committable"]]
    if not committable:
        raise ExtractionConflict(
            "The reviewed batch contains no committable human-confirmed facts"
        )
    attempts = [
        _attempt_from_readiness(extraction_batch_id, result) for result in committable
    ]
    ingest_event_id = (
        stable_id("EVT-XTR", extraction_batch_id) if attempts else None
    )
    ingest_payload = (
        {
            "event_id": ingest_event_id,
            "idempotency_key": f"opentutor:extraction:{extraction_batch_id}:facts:v1",
            "source_thread": batch["source_thread"],
            "student_id": student_id,
            "session_id": batch["session_id"],
            "attempts": attempts,
        }
        if attempts
        else None
    )
    now = utc_now()
    ingest_result: dict[str, Any] | None = None
    with _extraction_write_transaction(conn):
        # Re-read inside the owning transaction so the review gate and fact
        # insertion cannot observe different batch states.
        locked = _batch(conn, extraction_batch_id, student_id=student_id)
        if locked["status"] == "committed":
            raise ExtractionConflict("Extraction batch was committed concurrently")
        if int(locked["review_version"]) != expected_review_version:
            raise ExtractionConflict("review_version changed before commit")
        if ingest_payload is not None:
            ingest_result = import_attempts(
                conn,
                ingest_payload,
                backup_path=backup_path,
                manage_transaction=False,
            )
            if ingest_result["status"] not in {"applied", "duplicate"}:
                raise ExtractionConflict("Confirmed attempt ingest did not apply")
        for result, attempt_payload in zip(committable, attempts, strict=True):
            attempt = conn.execute(
                """
                SELECT * FROM attempts
                WHERE event_id=? AND student_id=? AND session_id=? AND record_status='active'
                """,
                (attempt_payload["event_id"], student_id, batch["session_id"]),
            ).fetchone()
            if not attempt:
                raise ExtractionConflict(
                    f"Committed attempt could not be read back: {attempt_payload['event_id']}"
                )
            evaluation = conn.execute(
                """
                SELECT * FROM evaluations
                WHERE attempt_id=? AND is_current=1
                """,
                (attempt["attempt_id"],),
            ).fetchone()
            if not evaluation:
                raise ExtractionConflict(
                    f"Committed evaluation could not be read back: {attempt['attempt_id']}"
                )
            item: sqlite3.Row = result["item"]
            decision: sqlite3.Row = result["decision"]
            conn.execute(
                """
                INSERT INTO extraction_commit_links(
                  extraction_batch_id,extraction_item_id,confirmation_decision_id,
                  attempt_id,evaluation_id,ingest_event_id,committed_at
                ) VALUES (?,?,?,?,?,?,?)
                """,
                (
                    extraction_batch_id,
                    item["extraction_item_id"],
                    decision["confirmation_decision_id"],
                    attempt["attempt_id"],
                    evaluation["evaluation_id"],
                    ingest_event_id,
                    now,
                ),
            )
        readback = _commit_readback(conn, extraction_batch_id)
        if readback["count"] != len(committable):
            raise ExtractionConflict(
                "Confirmed fact readback count does not match committable decisions"
            )
        conn.execute(
            """
            UPDATE extraction_batches
            SET status='committed',commit_idempotency_key=?,commit_request_sha256=?,
                committed_ingest_event_id=?,committed_at=?,updated_at=?
            WHERE extraction_batch_id=?
            """,
            (
                commit_key,
                commit_sha256,
                ingest_event_id,
                now,
                now,
                extraction_batch_id,
            ),
        )
    final_readback = _commit_readback(conn, extraction_batch_id)
    return {
        "status": "applied",
        "extraction_batch_id": extraction_batch_id,
        "ingest_event_id": ingest_event_id,
        "attempts_inserted": len(committable),
        "excluded_items": len(readiness) - len(committable),
        "ingest_result": ingest_result,
        "readback": final_readback,
        "batch": extraction_batch_detail(
            conn, extraction_batch_id, student_id=student_id
        ),
    }


def extraction_batch_detail(
    conn: sqlite3.Connection,
    extraction_batch_id: str,
    *,
    student_id: str,
) -> dict[str, Any]:
    student_id = _text(student_id, "student_id").upper()
    row = _batch(conn, extraction_batch_id, student_id=student_id)
    assets = [
        dict(asset)
        for asset in conn.execute(
            """
            SELECT extraction_asset_id,source_uri,sha256,media_type,byte_size,
                   page_number,created_at
            FROM extraction_assets
            WHERE extraction_batch_id=?
            ORDER BY page_number,extraction_asset_id
            """,
            (extraction_batch_id,),
        )
    ]
    links = [
        dict(link)
        for link in conn.execute(
            """
            SELECT extraction_item_id,confirmation_decision_id,attempt_id,
                   evaluation_id,ingest_event_id,committed_at
            FROM extraction_commit_links
            WHERE extraction_batch_id=? ORDER BY extraction_item_id
            """,
            (extraction_batch_id,),
        )
    ]
    return {
        "extraction_batch_id": row["extraction_batch_id"],
        "idempotency_key": row["idempotency_key"],
        "request_sha256": row["request_sha256"],
        "contract_version": row["contract_version"],
        "student_id": row["student_id"],
        "subject_code": row["subject_code"],
        "session_id": row["session_id"],
        "title": row["title"],
        "source_thread": row["source_thread"],
        "status": row["status"],
        "expected_item_count": row["expected_item_count"],
        "review_version": row["review_version"],
        "comparison_policy_version": row["comparison_policy_version"],
        "commit_idempotency_key": row["commit_idempotency_key"],
        "committed_ingest_event_id": row["committed_ingest_event_id"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "committed_at": row["committed_at"],
        "assets": assets,
        "commit_links": links,
        "review": extraction_review(
            conn, extraction_batch_id, student_id=student_id
        ),
    }
