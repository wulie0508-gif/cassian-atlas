from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from math import isfinite
from typing import Any
from urllib.parse import urlparse

from .util import canonical_json, payload_hash, stable_id, utc_now
from .workspace import require_student_enrollment


CONTRACT_VERSION = "feishu-base-operational-v1"
EXPECTED_TENANT = "Cassian Learning Lab | 学习工作室"
EXPECTED_APP = "Cassian Learning Ops"
EXPECTED_CLI_PROFILE = "cassian-learning-hub"
EXPECTED_IDENTITY = "user"

FRESHNESS_STATUSES = frozenset({"FRESH", "DELAYED", "STALE", "FAILED"})
FAILURE_CATEGORIES = frozenset(
    {
        "transport",
        "rate_limited",
        "authentication",
        "permission",
        "validation",
        "conflict",
        "remote_unavailable",
        "unknown",
    }
)

_SAFE_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:@-]*\Z")
_STUDENT_ID = re.compile(r"STU-[A-Z0-9-]{3,60}\Z")
_SUBJECT_CODE = re.compile(r"[a-z][a-z0-9_-]{0,31}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_WINDOWS_PATH = re.compile(r"(?:[A-Za-z]:[\\/]|\\\\)")
_FORBIDDEN_SCHEMES = ("file://", "private://")
_FORBIDDEN_KEY_PARTS = (
    "question",
    "prompt",
    "answer",
    "explanation",
    "rationale",
    "raw_response",
    "raw_output",
    "transcription",
    "ocr",
    "passage",
    "stem",
)


class ProjectionError(ValueError):
    """Base class for local projection contract failures."""


class ProjectionPrivacyError(ProjectionError):
    """Raised when content crosses the operational-only projection boundary."""


class ProjectionConflict(ProjectionError):
    """Raised when an idempotency key is reused for different work."""


class ProjectionStateError(ProjectionError):
    """Raised when a publisher result does not match the current outbox state."""


@dataclass(frozen=True)
class FieldSpec:
    name: str
    kind: str
    required: bool = True
    choices: frozenset[str] = frozenset()


@dataclass(frozen=True)
class ProjectionSpec:
    fields: tuple[FieldSpec, ...]
    key_fields: tuple[str, ...]


_COMMON_INPUT_FIELDS = (
    FieldSpec("metric_version", "identifier"),
    FieldSpec("freshness_status", "enum", choices=FRESHNESS_STATUSES),
    FieldSpec("sample_size", "nonnegative_int"),
)


PROJECTION_SPECS: dict[str, ProjectionSpec] = {
    "student_overview": ProjectionSpec(
        fields=_COMMON_INPUT_FIELDS
        + (
            FieldSpec("is_active", "bool"),
            FieldSpec("session_count", "nonnegative_int"),
            FieldSpec("attempt_count", "nonnegative_int"),
            FieldSpec("scored_attempt_count", "nonnegative_int"),
            FieldSpec("accuracy", "ratio", required=False),
            FieldSpec("review_due_count", "nonnegative_int"),
            FieldSpec("last_activity_at", "timestamp", required=False),
        ),
        key_fields=("metric_version",),
    ),
    "period_metrics": ProjectionSpec(
        fields=_COMMON_INPUT_FIELDS
        + (
            FieldSpec("period_start", "date"),
            FieldSpec("period_end", "date"),
            FieldSpec("assessment_kind", "identifier"),
            FieldSpec("reporting_series", "identifier"),
            FieldSpec("score_scale_max", "positive_number"),
            FieldSpec("attempt_count", "nonnegative_int"),
            FieldSpec("scored_attempt_count", "nonnegative_int"),
            FieldSpec("accuracy", "ratio", required=False),
            FieldSpec("average_score_rate", "ratio", required=False),
            FieldSpec("calibration_count", "nonnegative_int"),
        ),
        key_fields=(
            "metric_version",
            "period_start",
            "period_end",
            "assessment_kind",
            "reporting_series",
            "score_scale_max",
        ),
    ),
    "knowledge_performance": ProjectionSpec(
        fields=_COMMON_INPUT_FIELDS
        + (
            FieldSpec("knowledge_code", "identifier"),
            FieldSpec("attempt_count", "nonnegative_int"),
            FieldSpec("distinct_item_count", "nonnegative_int"),
            FieldSpec("weighted_accuracy", "ratio", required=False),
            FieldSpec(
                "mastery_status",
                "enum",
                choices=frozenset({"insufficient", "tentative", "developing", "secure"}),
            ),
            FieldSpec("last_evidence_at", "timestamp", required=False),
        ),
        key_fields=("metric_version", "knowledge_code"),
    ),
    "retest_summary": ProjectionSpec(
        fields=_COMMON_INPUT_FIELDS
        + (
            FieldSpec("window_start", "date"),
            FieldSpec("window_end", "date"),
            FieldSpec("due_count", "nonnegative_int"),
            FieldSpec("completed_count", "nonnegative_int"),
            FieldSpec("recovered_count", "nonnegative_int"),
            FieldSpec("still_incorrect_count", "nonnegative_int"),
            FieldSpec("overdue_count", "nonnegative_int"),
            FieldSpec("next_due_at", "timestamp", required=False),
        ),
        key_fields=("metric_version", "window_start", "window_end"),
    ),
    "data_quality": ProjectionSpec(
        fields=_COMMON_INPUT_FIELDS
        + (
            FieldSpec("check_scope", "identifier"),
            FieldSpec("total_check_count", "nonnegative_int"),
            FieldSpec("failed_check_count", "nonnegative_int"),
            FieldSpec("critical_failure_count", "nonnegative_int"),
            FieldSpec(
                "trust_status",
                "enum",
                choices=frozenset({"ready", "use_with_caution", "not_ready"}),
            ),
            FieldSpec("checked_at", "timestamp"),
        ),
        key_fields=("metric_version", "check_scope"),
    ),
    "generation_runs": ProjectionSpec(
        fields=_COMMON_INPUT_FIELDS
        + (
            FieldSpec("generation_id", "identifier"),
            FieldSpec("artifact_type", "identifier"),
            FieldSpec(
                "run_status",
                "enum",
                choices=frozenset({"planned", "in_progress", "completed", "failed", "cancelled"}),
            ),
            FieldSpec("is_stale", "bool"),
            FieldSpec("created_at", "timestamp"),
            FieldSpec("started_at", "timestamp", required=False),
            FieldSpec("completed_at", "timestamp", required=False),
        ),
        key_fields=("generation_id",),
    ),
    "teacher_policy_correction_inbox": ProjectionSpec(
        fields=_COMMON_INPUT_FIELDS
        + (
            FieldSpec("inbox_item_id", "identifier"),
            FieldSpec(
                "inbox_kind",
                "enum",
                choices=frozenset({"teacher_policy", "correction"}),
            ),
            FieldSpec(
                "review_status",
                "enum",
                choices=frozenset({"open", "in_review", "resolved", "dismissed"}),
            ),
            FieldSpec(
                "priority",
                "enum",
                choices=frozenset({"low", "medium", "high", "critical"}),
            ),
            FieldSpec("reason_code", "identifier"),
            FieldSpec(
                "source_entity_type",
                "enum",
                choices=frozenset(
                    {
                        "extraction_batch",
                        "attempt",
                        "evaluation",
                        "generation_run",
                        "quality_check",
                        "policy",
                    }
                ),
            ),
            FieldSpec("source_entity_id", "identifier"),
            FieldSpec("opened_at", "timestamp"),
            FieldSpec("due_at", "timestamp", required=False),
            FieldSpec("resolved_at", "timestamp", required=False),
        ),
        key_fields=("inbox_item_id",),
    ),
}


def _looks_like_path(value: str) -> bool:
    stripped = value.strip()
    lowered = stripped.casefold()
    return bool(
        _WINDOWS_PATH.search(stripped)
        or stripped.startswith("/")
        or any(lowered.startswith(scheme) for scheme in _FORBIDDEN_SCHEMES)
    )


def _guard_no_forbidden_content(value: Any, *, path: str = "payload") -> None:
    if isinstance(value, Mapping):
        for raw_key, item in value.items():
            key = str(raw_key).strip().casefold()
            if (
                any(part in key for part in _FORBIDDEN_KEY_PARTS)
                or key.endswith("_path")
                or key in {"path", "url", "uri", "image", "audio", "content", "student_name", "display_name"}
            ):
                raise ProjectionPrivacyError(f"{path}.{raw_key} is forbidden in Base projections")
            _guard_no_forbidden_content(item, path=f"{path}.{raw_key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _guard_no_forbidden_content(item, path=f"{path}[{index}]")
    elif isinstance(value, str) and _looks_like_path(value):
        raise ProjectionPrivacyError(f"{path} cannot contain a local or private path")


def _normalize_student_id(value: Any) -> str:
    student_id = str(value or "").strip().upper()
    if not _STUDENT_ID.fullmatch(student_id):
        raise ProjectionError("student_id must be an explicit STU- identifier")
    return student_id


def _normalize_subject_code(value: Any) -> str:
    subject_code = str(value or "").strip().lower()
    if not _SUBJECT_CODE.fullmatch(subject_code):
        raise ProjectionError("subject_code must be a safe subject identifier")
    return subject_code


def _safe_identifier(value: Any, field: str, *, max_length: int = 160) -> str:
    result = str(value or "").strip()
    if not result or len(result) > max_length or not _SAFE_IDENTIFIER.fullmatch(result):
        raise ProjectionError(f"{field} must be an opaque operational identifier")
    if _looks_like_path(result):
        raise ProjectionPrivacyError(f"{field} cannot contain a local or private path")
    return result


def _sha256_value(value: Any, field: str) -> str:
    result = str(value or "").strip().lower()
    if not _SHA256.fullmatch(result):
        raise ProjectionError(f"{field} must be a lowercase SHA-256 digest")
    return result


def _normalize_timestamp(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProjectionError(f"{field} must be an ISO-8601 timestamp with a timezone")
    raw = value.strip()
    try:
        parsed = datetime.fromisoformat(raw[:-1] + "+00:00" if raw.endswith("Z") else raw)
    except ValueError as exc:
        raise ProjectionError(f"{field} must be an ISO-8601 timestamp with a timezone") from exc
    if parsed.tzinfo is None:
        raise ProjectionError(f"{field} must include a timezone")
    return parsed.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _base_timestamp(value: Any, field: str) -> str:
    normalized = _normalize_timestamp(value, field)
    return datetime.fromisoformat(normalized[:-1] + "+00:00").strftime("%Y-%m-%d %H:%M:%S")


def _normalize_date(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProjectionError(f"{field} must use YYYY-MM-DD")
    try:
        return date.fromisoformat(value.strip()).isoformat()
    except ValueError as exc:
        raise ProjectionError(f"{field} must use YYYY-MM-DD") from exc


def _normalize_field(field: FieldSpec, value: Any, *, present: bool) -> Any:
    if not present or value is None:
        if field.required:
            raise ProjectionError(f"{field.name} is required")
        return None
    if field.kind == "identifier":
        return _safe_identifier(value, field.name)
    if field.kind == "enum":
        result = str(value).strip()
        if result not in field.choices:
            raise ProjectionError(
                f"{field.name} must be one of: {', '.join(sorted(field.choices))}"
            )
        return result
    if field.kind == "bool":
        if type(value) is not bool:
            raise ProjectionError(f"{field.name} must be a boolean")
        return value
    if field.kind == "nonnegative_int":
        if type(value) is not int or value < 0:
            raise ProjectionError(f"{field.name} must be a non-negative integer")
        return value
    if field.kind in {"ratio", "positive_number"}:
        if isinstance(value, bool):
            raise ProjectionError(f"{field.name} must be numeric")
        try:
            result = float(value)
        except (TypeError, ValueError) as exc:
            raise ProjectionError(f"{field.name} must be numeric") from exc
        if not isfinite(result):
            raise ProjectionError(f"{field.name} must be finite")
        if field.kind == "ratio" and not 0 <= result <= 1:
            raise ProjectionError(f"{field.name} must be between 0 and 1")
        if field.kind == "positive_number" and result <= 0:
            raise ProjectionError(f"{field.name} must be greater than zero")
        return result
    if field.kind == "date":
        return _normalize_date(value, field.name)
    if field.kind == "timestamp":
        return _base_timestamp(value, field.name)
    raise AssertionError(f"Unknown projection field kind: {field.kind}")


def _validate_projection_semantics(projection_name: str, values: Mapping[str, Any]) -> None:
    if projection_name in {"student_overview", "period_metrics"}:
        if values["scored_attempt_count"] > values["attempt_count"]:
            raise ProjectionError("scored_attempt_count cannot exceed attempt_count")
    if projection_name == "period_metrics":
        if values["period_end"] < values["period_start"]:
            raise ProjectionError("period_end cannot precede period_start")
        if values["calibration_count"] > values["scored_attempt_count"]:
            raise ProjectionError("calibration_count cannot exceed scored_attempt_count")
    elif projection_name == "knowledge_performance":
        if values["distinct_item_count"] > values["attempt_count"]:
            raise ProjectionError("distinct_item_count cannot exceed attempt_count")
    elif projection_name == "retest_summary":
        if values["window_end"] < values["window_start"]:
            raise ProjectionError("window_end cannot precede window_start")
        if values["recovered_count"] + values["still_incorrect_count"] > values["completed_count"]:
            raise ProjectionError("retest outcomes cannot exceed completed_count")
    elif projection_name == "data_quality":
        if values["failed_check_count"] > values["total_check_count"]:
            raise ProjectionError("failed_check_count cannot exceed total_check_count")
        if values["critical_failure_count"] > values["failed_check_count"]:
            raise ProjectionError("critical_failure_count cannot exceed failed_check_count")
    elif projection_name == "generation_runs":
        terminal = values["run_status"] in {"completed", "failed", "cancelled"}
        if terminal != (values["completed_at"] is not None):
            raise ProjectionError("generation terminal status and completed_at must agree")
        if values["run_status"] == "in_progress" and values["started_at"] is None:
            raise ProjectionError("in_progress generation requires started_at")
    elif projection_name == "teacher_policy_correction_inbox":
        terminal = values["review_status"] in {"resolved", "dismissed"}
        if terminal != (values["resolved_at"] is not None):
            raise ProjectionError("inbox terminal status and resolved_at must agree")


def projection_contract() -> dict[str, Any]:
    """Return the local, transport-independent whitelist contract."""
    projections: dict[str, Any] = {}
    for name, spec in PROJECTION_SPECS.items():
        projections[name] = {
            "fields": [field.name for field in spec.fields],
            "required_fields": [field.name for field in spec.fields if field.required],
            "key_fields": list(spec.key_fields),
        }
    return {
        "contract_version": CONTRACT_VERSION,
        "projection_names": list(PROJECTION_SPECS),
        "common_outbound_fields": [
            "projection_upsert_key",
            "projection_name",
            "projection_contract_version",
            "student_id",
            "subject_code",
            "data_as_of",
        ],
        "projections": projections,
    }


def _feishu_tenant_domain(host: str, field: str) -> str:
    normalized = host.strip().casefold().rstrip(".")
    for domain in ("feishu.cn", "larksuite.com"):
        if normalized.endswith(f".{domain}"):
            return domain
    raise ProjectionError(f"{field} must use a tenant-specific Feishu/Lark host")


def _target_reference(
    student_target: Mapping[str, Any],
    field: str,
    *,
    required_path: str,
) -> dict[str, str]:
    target = student_target.get(field)
    if not isinstance(target, Mapping):
        raise ProjectionError(f"student target requires {field}")
    name = target.get("name")
    token = target.get("token")
    url = target.get("url")
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 200:
        raise ProjectionError(f"student {field} target name is missing")
    if _looks_like_path(name):
        raise ProjectionPrivacyError(f"student {field} target name cannot be a local path")
    if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,256}", token.strip()):
        raise ProjectionError(f"student {field} token is missing or malformed")
    if not isinstance(url, str) or not url.strip():
        raise ProjectionError(f"student {field} URL is required")
    try:
        parsed = urlparse(url.strip())
        port = parsed.port
    except ValueError as exc:
        raise ProjectionError(f"student {field} URL is malformed") from exc
    if (
        parsed.scheme.casefold() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
    ):
        raise ProjectionError(f"student {field} URL must be a credential-free HTTPS target")
    host = parsed.hostname.casefold().rstrip(".")
    domain = _feishu_tenant_domain(host, f"student {field} URL")
    segments = [segment for segment in parsed.path.split("/") if segment]
    if required_path not in parsed.path or token.strip() not in segments:
        raise ProjectionError(f"student {field} URL does not match its configured token")
    return {
        "name": name.strip(),
        "token": token.strip(),
        "url": url.strip(),
        "host": host,
        "domain": domain,
    }


def validate_projection_target_config(
    config: Mapping[str, Any],
    *,
    student_id: str,
) -> dict[str, Any]:
    """Fail closed on a local target config and return no token or URL."""
    student_id = _normalize_student_id(student_id)
    if not isinstance(config, Mapping):
        raise ProjectionError("target config must be an object")
    primary = config.get("primary_target")
    guard = config.get("write_guard")
    if not isinstance(primary, Mapping) or not isinstance(guard, Mapping):
        raise ProjectionError("target config requires primary_target and write_guard")

    expected_primary = {
        "tenant_display_name": EXPECTED_TENANT,
        "app_name": EXPECTED_APP,
        "cli_profile": EXPECTED_CLI_PROFILE,
        "identity": EXPECTED_IDENTITY,
    }
    for field, expected in expected_primary.items():
        if primary.get(field) != expected:
            raise ProjectionError(f"primary_target.{field} mismatch")
    if guard.get("require_explicit_profile") is not True:
        raise ProjectionError("write_guard.require_explicit_profile must be true")
    if guard.get("required_profile") != EXPECTED_CLI_PROFILE:
        raise ProjectionError("write_guard.required_profile mismatch")
    if guard.get("required_identity") != EXPECTED_IDENTITY:
        raise ProjectionError("write_guard.required_identity mismatch")
    if guard.get("fail_closed_on_mismatch") is not True:
        raise ProjectionError("write_guard.fail_closed_on_mismatch must be true")
    if guard.get("upload_question_bank") is not False:
        raise ProjectionError("write_guard.upload_question_bank must be false")

    students = primary.get("students")
    if not isinstance(students, Mapping) or student_id not in students:
        raise ProjectionError(f"primary_target.students lacks exact target for {student_id}")
    student_target = students[student_id]
    if not isinstance(student_target, Mapping):
        raise ProjectionError(f"primary_target.students.{student_id} must be an object")
    folder = _target_reference(student_target, "folder", required_path="/drive/folder/")
    base = _target_reference(student_target, "base", required_path="/base/")
    if folder["host"] != base["host"] or folder["domain"] != base["domain"]:
        raise ProjectionError("student folder and Base targets must use the same tenant host")

    fingerprint = payload_hash(
        {
            "contract_version": CONTRACT_VERSION,
            "tenant_display_name": EXPECTED_TENANT,
            "app_name": EXPECTED_APP,
            "cli_profile": EXPECTED_CLI_PROFILE,
            "identity": EXPECTED_IDENTITY,
            "student_id": student_id,
            "tenant_host": base["host"],
            "tenant_domain": base["domain"],
            "folder_name": folder["name"],
            "folder_token": folder["token"],
            "folder_url": folder["url"],
            "base_name": base["name"],
            "base_token": base["token"],
            "base_url": base["url"],
        }
    )
    return {
        "status": "ready",
        "target_identity": {
            "tenant_display_name": EXPECTED_TENANT,
            "app_name": EXPECTED_APP,
            "cli_profile": EXPECTED_CLI_PROFILE,
            "identity": EXPECTED_IDENTITY,
            "student_id": student_id,
            "tenant_host": base["host"],
            "tenant_domain": base["domain"],
        },
        "student_folder_target_present": True,
        "base_target_present": True,
        "target_fingerprint_sha256": fingerprint,
    }


def build_projection_payload(
    projection_name: str,
    *,
    student_id: str,
    subject_code: str,
    data_as_of: str,
    record: Mapping[str, Any],
) -> dict[str, Any]:
    """Build one deterministic flat Base CellValue map from precomputed metrics."""
    projection_name = str(projection_name or "").strip()
    spec = PROJECTION_SPECS.get(projection_name)
    if not spec:
        raise ProjectionError(f"Unknown projection_name: {projection_name}")
    student_id = _normalize_student_id(student_id)
    subject_code = _normalize_subject_code(subject_code)
    normalized_as_of = _normalize_timestamp(data_as_of, "data_as_of")
    if not isinstance(record, Mapping):
        raise ProjectionError("projection record must be an object")
    _guard_no_forbidden_content(record, path="record")

    allowed = {field.name for field in spec.fields}
    unknown = sorted(str(key) for key in record if key not in allowed)
    if unknown:
        raise ProjectionError(f"Unknown projection fields: {', '.join(unknown)}")
    normalized: dict[str, Any] = {}
    for field in spec.fields:
        normalized[field.name] = _normalize_field(
            field,
            record.get(field.name),
            present=field.name in record,
        )
    _validate_projection_semantics(projection_name, normalized)

    key_material = [normalized[field] for field in spec.key_fields]
    upsert_key = stable_id(
        "FBKEY",
        CONTRACT_VERSION,
        projection_name,
        student_id,
        subject_code,
        canonical_json(key_material),
        length=32,
    )
    result: dict[str, Any] = {
        "projection_upsert_key": upsert_key,
        "projection_name": projection_name,
        "projection_contract_version": CONTRACT_VERSION,
        "student_id": student_id,
        "subject_code": subject_code,
        "data_as_of": _base_timestamp(normalized_as_of, "data_as_of"),
    }
    result.update(normalized)
    return result


def stage_projection_run(
    conn: sqlite3.Connection,
    payload: Mapping[str, Any],
    *,
    target_config: Mapping[str, Any],
) -> dict[str, Any]:
    """Stage a deterministic, idempotent local outbox run without calling Feishu."""
    if not isinstance(payload, Mapping):
        raise ProjectionError("projection run payload must be an object")
    _guard_no_forbidden_content(payload)
    allowed = {
        "idempotency_key",
        "projection_name",
        "student_id",
        "subject_code",
        "data_as_of",
        "publisher",
        "records",
    }
    unknown = sorted(str(key) for key in payload if key not in allowed)
    if unknown:
        raise ProjectionError(f"Unknown projection run fields: {', '.join(unknown)}")

    idempotency_key = _safe_identifier(payload.get("idempotency_key"), "idempotency_key", max_length=200)
    projection_name = str(payload.get("projection_name") or "").strip()
    if projection_name not in PROJECTION_SPECS:
        raise ProjectionError(f"Unknown projection_name: {projection_name}")
    student_id = _normalize_student_id(payload.get("student_id"))
    subject_code = _normalize_subject_code(payload.get("subject_code") or "english")
    target_preflight = validate_projection_target_config(
        target_config,
        student_id=student_id,
    )
    target_fingerprint = _sha256_value(
        target_preflight["target_fingerprint_sha256"],
        "target_fingerprint_sha256",
    )
    student_id, subject_code = require_student_enrollment(
        conn,
        student_id,
        subject_code,
    )
    data_as_of = _normalize_timestamp(payload.get("data_as_of"), "data_as_of")
    publisher = _safe_identifier(
        payload.get("publisher") or "opentutor_local_publisher",
        "publisher",
        max_length=80,
    )
    records = payload.get("records")
    if not isinstance(records, list) or not records:
        raise ProjectionError("records must be a non-empty array")
    if len(records) > 200:
        raise ProjectionError("a projection run cannot stage more than 200 records")

    built: list[dict[str, Any]] = []
    for record in records:
        fields = build_projection_payload(
            projection_name,
            student_id=student_id,
            subject_code=subject_code,
            data_as_of=data_as_of,
            record=record,
        )
        built.append(
            {
                "projection_upsert_key": fields["projection_upsert_key"],
                "payload_sha256": payload_hash(fields),
                "fields": fields,
            }
        )
    built.sort(key=lambda item: item["projection_upsert_key"])
    upsert_keys = [item["projection_upsert_key"] for item in built]
    if len(set(upsert_keys)) != len(upsert_keys):
        raise ProjectionError("records contain duplicate stable projection keys")

    bundle_sha256 = payload_hash(
        [
            {
                "projection_upsert_key": item["projection_upsert_key"],
                "payload_sha256": item["payload_sha256"],
            }
            for item in built
        ]
    )
    request_sha256 = payload_hash(
        {
            "contract_version": CONTRACT_VERSION,
            "target_fingerprint_sha256": target_fingerprint,
            "projection_name": projection_name,
            "student_id": student_id,
            "subject_code": subject_code,
            "data_as_of": data_as_of,
            "publisher": publisher,
            "records": built,
        }
    )
    projection_run_id = stable_id("FBPRUN", idempotency_key, length=28)
    now = utc_now()
    duplicate_run_id: str | None = None
    try:
        # SQLite's default transaction is deferred. Acquire the write lease before
        # reading either idempotency or active-key state so two local publishers
        # cannot both pass the checks and race their inserts.
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                """
                SELECT projection_run_id,request_sha256
                FROM base_projection_runs WHERE idempotency_key=?
                """,
                (idempotency_key,),
            ).fetchone()
            if existing:
                if existing["request_sha256"] != request_sha256:
                    raise ProjectionConflict(
                        "idempotency_key already belongs to a different projection run"
                    )
                duplicate_run_id = existing["projection_run_id"]
            else:
                active = conn.execute(
                    """
                    SELECT projection_upsert_key,projection_run_id
                    FROM base_projection_outbox
                    WHERE projection_upsert_key IN ({})
                      AND status IN ('pending','inflight','retryable_failed')
                    LIMIT 1
                    """.format(",".join("?" for _ in upsert_keys)),
                    upsert_keys,
                ).fetchone()
                if active:
                    raise ProjectionStateError(
                        "a stable projection key already has an active delivery run"
                    )

                staged_rows: list[tuple[Any, ...]] = []
                unchanged_count = 0
                for record_no, item in enumerate(built, 1):
                    state = conn.execute(
                        """
                        SELECT last_payload_sha256 FROM base_projection_state
                        WHERE projection_upsert_key=? AND projection_name=?
                          AND student_id=? AND subject_code=?
                        """,
                        (
                            item["projection_upsert_key"],
                            projection_name,
                            student_id,
                            subject_code,
                        ),
                    ).fetchone()
                    unchanged = bool(
                        state
                        and state["last_payload_sha256"] == item["payload_sha256"]
                    )
                    if unchanged:
                        unchanged_count += 1
                    staged_rows.append(
                        (
                            stable_id(
                                "FBPOUT",
                                projection_run_id,
                                item["projection_upsert_key"],
                                length=28,
                            ),
                            projection_run_id,
                            record_no,
                            projection_name,
                            student_id,
                            subject_code,
                            item["projection_upsert_key"],
                            canonical_json(item["fields"]),
                            item["payload_sha256"],
                            "skipped_unchanged" if unchanged else "pending",
                            now,
                            now,
                            now if unchanged else None,
                        )
                    )

                conn.execute(
                    """
                    INSERT INTO base_projection_runs(
                      projection_run_id,idempotency_key,request_sha256,contract_version,
                      target_fingerprint_sha256,projection_name,student_id,subject_code,
                      data_as_of,payload_sha256,record_count,publisher,status,
                      created_at,updated_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?, 'staged',?,?)
                    """,
                    (
                        projection_run_id,
                        idempotency_key,
                        request_sha256,
                        CONTRACT_VERSION,
                        target_fingerprint,
                        projection_name,
                        student_id,
                        subject_code,
                        data_as_of,
                        bundle_sha256,
                        len(staged_rows),
                        publisher,
                        now,
                        now,
                    ),
                )
                conn.executemany(
                    """
                    INSERT INTO base_projection_outbox(
                      outbox_id,projection_run_id,record_no,projection_name,student_id,
                      subject_code,projection_upsert_key,payload_json,payload_sha256,
                      status,attempt_count,created_at,updated_at,completed_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,0,?,?,?)
                    """,
                    staged_rows,
                )
                if unchanged_count == len(staged_rows):
                    conn.execute(
                        """
                        UPDATE base_projection_runs
                        SET status='completed',started_at=?,updated_at=?,completed_at=?
                        WHERE projection_run_id=?
                        """,
                        (now, now, now, projection_run_id),
                    )
    except sqlite3.IntegrityError as exc:
        # A packaged trigger or unique index is still a final line of defence,
        # but callers should receive a stable domain error rather than raw SQL.
        existing = conn.execute(
            """
            SELECT projection_run_id,request_sha256
            FROM base_projection_runs WHERE idempotency_key=?
            """,
            (idempotency_key,),
        ).fetchone()
        if existing:
            if existing["request_sha256"] != request_sha256:
                raise ProjectionConflict(
                    "idempotency_key already belongs to a different projection run"
                ) from None
            duplicate_run_id = existing["projection_run_id"]
        else:
            raise ProjectionStateError(
                "projection staging conflicted with another local writer; retry with the same idempotency_key"
            ) from exc
    except sqlite3.OperationalError as exc:
        if "locked" in str(exc).casefold() or "busy" in str(exc).casefold():
            raise ProjectionStateError(
                "projection staging is busy; retry with the same idempotency_key"
            ) from None
        raise

    if duplicate_run_id is not None:
        return {
            "status": "duplicate",
            "run": projection_run_detail(
                conn,
                duplicate_run_id,
                student_id=student_id,
            ),
        }
    return {
        "status": "created",
        "run": projection_run_detail(conn, projection_run_id, student_id=student_id),
    }


def _internal_now(value: str | None) -> str:
    return _normalize_timestamp(value or utc_now(), "now")


def claim_next_projection_record(
    conn: sqlite3.Connection,
    projection_run_id: str,
    *,
    student_id: str,
    now: str | None = None,
) -> dict[str, Any] | None:
    """Atomically claim one eligible record; transport remains the caller's concern."""
    student_id = _normalize_student_id(student_id)
    current = _internal_now(now)
    run = conn.execute(
        "SELECT * FROM base_projection_runs WHERE projection_run_id=? AND student_id=?",
        (projection_run_id, student_id),
    ).fetchone()
    if not run:
        raise ProjectionStateError(
            f"Unknown projection_run_id for student {student_id}: {projection_run_id}"
        )
    if run["status"] in {"completed", "permanent_failed"}:
        return None
    row = conn.execute(
        """
        SELECT * FROM base_projection_outbox
        WHERE projection_run_id=?
          AND (
            status='pending'
            OR (status='retryable_failed' AND next_attempt_at<=?)
          )
        ORDER BY record_no
        LIMIT 1
        """,
        (projection_run_id, current),
    ).fetchone()
    if not row:
        return None
    with conn:
        cursor = conn.execute(
            """
            UPDATE base_projection_outbox
            SET status='inflight',attempt_count=attempt_count+1,next_attempt_at=NULL,
                last_claimed_at=?,updated_at=?
            WHERE outbox_id=?
              AND (status='pending' OR (status='retryable_failed' AND next_attempt_at<=?))
            """,
            (current, current, row["outbox_id"], current),
        )
        if cursor.rowcount != 1:
            return None
        conn.execute(
            """
            UPDATE base_projection_runs
            SET status='publishing',started_at=COALESCE(started_at,?),updated_at=?
            WHERE projection_run_id=?
            """,
            (current, current, projection_run_id),
        )
    claimed = conn.execute(
        """
        SELECT o.*,s.remote_record_id
        FROM base_projection_outbox o
        LEFT JOIN base_projection_state s
          ON s.projection_upsert_key=o.projection_upsert_key
        WHERE o.outbox_id=?
        """,
        (row["outbox_id"],),
    ).fetchone()
    remote_record_id = claimed["remote_record_id"]
    return {
        "outbox_id": claimed["outbox_id"],
        "projection_run_id": projection_run_id,
        "projection_upsert_key": claimed["projection_upsert_key"],
        "attempt_no": int(claimed["attempt_count"]),
        "operation": "update" if remote_record_id else "lookup_or_create",
        "lookup_field": "projection_upsert_key",
        "remote_record_id": remote_record_id,
        "payload_sha256": claimed["payload_sha256"],
        "fields": json.loads(claimed["payload_json"]),
        "target_fingerprint_sha256": run["target_fingerprint_sha256"],
    }


def _refresh_projection_run(
    conn: sqlite3.Connection,
    projection_run_id: str,
    *,
    now: str,
    failure_category: str | None = None,
    failure_code: str | None = None,
) -> None:
    counts = {
        row["status"]: int(row["count"])
        for row in conn.execute(
            """
            SELECT status,COUNT(*) count FROM base_projection_outbox
            WHERE projection_run_id=? GROUP BY status
            """,
            (projection_run_id,),
        )
    }
    if counts.get("pending", 0) or counts.get("inflight", 0):
        status = "publishing"
    elif counts.get("retryable_failed", 0):
        status = "retryable_failed"
    elif counts.get("permanent_failed", 0):
        status = "permanent_failed"
    else:
        status = "completed"
    completed_at = now if status in {"completed", "permanent_failed"} else None
    conn.execute(
        """
        UPDATE base_projection_runs
        SET status=?,started_at=COALESCE(started_at,?),updated_at=?,completed_at=?,
            last_failure_category=COALESCE(?,last_failure_category),
            last_failure_code=COALESCE(?,last_failure_code)
        WHERE projection_run_id=?
        """,
        (
            status,
            now,
            now,
            completed_at,
            failure_category,
            failure_code,
            projection_run_id,
        ),
    )


def record_projection_delivery_result(
    conn: sqlite3.Connection,
    payload: Mapping[str, Any],
    *,
    student_id: str,
    now: str | None = None,
) -> dict[str, Any]:
    """Record a sanitized delivery outcome and update retry/state ledgers atomically."""
    student_id = _normalize_student_id(student_id)
    if not isinstance(payload, Mapping):
        raise ProjectionError("delivery result must be an object")
    _guard_no_forbidden_content(payload)
    allowed = {
        "idempotency_key",
        "outbox_id",
        "attempt_no",
        "outcome",
        "remote_record_id",
        "readback_payload_sha256",
        "failure_category",
        "failure_code",
        "retry_after_seconds",
    }
    unknown = sorted(str(key) for key in payload if key not in allowed)
    if unknown:
        raise ProjectionError(f"Unknown delivery result fields: {', '.join(unknown)}")
    idempotency_key = _safe_identifier(
        payload.get("idempotency_key"),
        "idempotency_key",
        max_length=200,
    )
    outbox_id = _safe_identifier(payload.get("outbox_id"), "outbox_id")
    attempt_no = payload.get("attempt_no")
    if type(attempt_no) is not int or attempt_no <= 0:
        raise ProjectionError("attempt_no must be a positive integer")
    outcome = str(payload.get("outcome") or "").strip()
    if outcome not in {"succeeded", "retryable_failed", "permanent_failed"}:
        raise ProjectionError("outcome must be succeeded, retryable_failed, or permanent_failed")

    remote_record_id: str | None = None
    readback_payload_sha256: str | None = None
    failure_category: str | None = None
    failure_code: str | None = None
    retry_after_seconds: int | None = None
    if outcome == "succeeded":
        remote_record_id = _safe_identifier(
            payload.get("remote_record_id"),
            "remote_record_id",
        )
        readback_payload_sha256 = _sha256_value(
            payload.get("readback_payload_sha256"),
            "readback_payload_sha256",
        )
        if any(payload.get(field) is not None for field in ("failure_category", "failure_code", "retry_after_seconds")):
            raise ProjectionError("successful delivery cannot carry failure metadata")
    else:
        if payload.get("remote_record_id") is not None:
            raise ProjectionError("failed delivery cannot replace remote_record_id")
        if payload.get("readback_payload_sha256") is not None:
            raise ProjectionError("failed delivery cannot carry a readback payload hash")
        failure_category = str(payload.get("failure_category") or "").strip()
        if failure_category not in FAILURE_CATEGORIES:
            raise ProjectionError(
                f"failure_category must be one of: {', '.join(sorted(FAILURE_CATEGORIES))}"
            )
        failure_code = _safe_identifier(payload.get("failure_code"), "failure_code")
        if outcome == "retryable_failed":
            retry_after_seconds = payload.get("retry_after_seconds", 60)
            if (
                type(retry_after_seconds) is not int
                or retry_after_seconds < 0
                or retry_after_seconds > 604800
            ):
                raise ProjectionError("retry_after_seconds must be an integer from 0 to 604800")
        elif payload.get("retry_after_seconds") is not None:
            raise ProjectionError("permanent failure cannot carry retry_after_seconds")

    result_request = {
        "outbox_id": outbox_id,
        "attempt_no": attempt_no,
        "outcome": outcome,
        "remote_record_id": remote_record_id,
        "readback_payload_sha256": readback_payload_sha256,
        "failure_category": failure_category,
        "failure_code": failure_code,
        "retry_after_seconds": retry_after_seconds,
    }
    result_sha256 = payload_hash(result_request)
    recorded_at = _internal_now(now)
    retry_at = None
    if retry_after_seconds is not None:
        parsed = datetime.fromisoformat(recorded_at[:-1] + "+00:00")
        retry_at = (parsed + timedelta(seconds=retry_after_seconds)).isoformat().replace("+00:00", "Z")
    delivery_attempt_id = stable_id("FBPDEL", idempotency_key, length=28)
    duplicate_attempt: sqlite3.Row | None = None
    recorded_run_id: str | None = None
    if conn.in_transaction:
        raise ProjectionStateError(
            "recording a delivery result requires a clean transaction boundary"
        )
    try:
        # Claim the SQLite write lease before the idempotency and ownership reads.
        # Otherwise two publishers can both observe an inflight row and race the
        # unique attempt insert or apply conflicting state transitions.
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                """
                SELECT a.*,o.projection_run_id
                FROM base_projection_delivery_attempts a
                JOIN base_projection_outbox o ON o.outbox_id=a.outbox_id
                WHERE a.idempotency_key=? AND o.student_id=?
                """,
                (idempotency_key, student_id),
            ).fetchone()
            if existing:
                if existing["result_sha256"] != result_sha256:
                    raise ProjectionConflict(
                        "idempotency_key already belongs to a different delivery result"
                    )
                duplicate_attempt = existing
            else:
                outbox = conn.execute(
                    """
                    SELECT * FROM base_projection_outbox
                    WHERE outbox_id=? AND student_id=?
                    """,
                    (outbox_id, student_id),
                ).fetchone()
                if not outbox:
                    raise ProjectionStateError(
                        f"Unknown outbox_id for student {student_id}: {outbox_id}"
                    )
                if (
                    outbox["status"] != "inflight"
                    or int(outbox["attempt_count"]) != attempt_no
                ):
                    raise ProjectionStateError(
                        "delivery result does not match the active outbox attempt"
                    )
                if outcome == "succeeded":
                    if readback_payload_sha256 != outbox["payload_sha256"]:
                        raise ProjectionStateError(
                            "readback payload hash does not match the staged projection payload"
                        )
                    state = conn.execute(
                        """
                        SELECT remote_record_id FROM base_projection_state
                        WHERE projection_upsert_key=?
                        """,
                        (outbox["projection_upsert_key"],),
                    ).fetchone()
                    if state and state["remote_record_id"] != remote_record_id:
                        raise ProjectionStateError(
                            "remote record ID drift conflicts with the published projection state"
                        )

                conn.execute(
                    """
                    INSERT INTO base_projection_delivery_attempts(
                      delivery_attempt_id,idempotency_key,result_sha256,outbox_id,attempt_no,
                      outcome,remote_record_id,readback_payload_sha256,failure_category,
                      failure_code,retry_at,recorded_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        delivery_attempt_id,
                        idempotency_key,
                        result_sha256,
                        outbox_id,
                        attempt_no,
                        outcome,
                        remote_record_id,
                        readback_payload_sha256,
                        failure_category,
                        failure_code,
                        retry_at,
                        recorded_at,
                    ),
                )
                if outcome == "succeeded":
                    conn.execute(
                        """
                        UPDATE base_projection_outbox
                        SET status='succeeded',next_attempt_at=NULL,last_failure_category=NULL,
                            last_failure_code=NULL,updated_at=?,completed_at=?
                        WHERE outbox_id=?
                        """,
                        (recorded_at, recorded_at, outbox_id),
                    )
                    conn.execute(
                        """
                        INSERT INTO base_projection_state(
                          projection_upsert_key,projection_name,student_id,subject_code,
                          remote_record_id,last_payload_sha256,last_outbox_id,
                          last_delivery_attempt_id,first_published_at,updated_at
                        ) VALUES (?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(projection_upsert_key) DO UPDATE SET
                          remote_record_id=excluded.remote_record_id,
                          last_payload_sha256=excluded.last_payload_sha256,
                          last_outbox_id=excluded.last_outbox_id,
                          last_delivery_attempt_id=excluded.last_delivery_attempt_id,
                          updated_at=excluded.updated_at
                        """,
                        (
                            outbox["projection_upsert_key"],
                            outbox["projection_name"],
                            outbox["student_id"],
                            outbox["subject_code"],
                            remote_record_id,
                            outbox["payload_sha256"],
                            outbox_id,
                            delivery_attempt_id,
                            recorded_at,
                            recorded_at,
                        ),
                    )
                elif outcome == "retryable_failed":
                    conn.execute(
                        """
                        UPDATE base_projection_outbox
                        SET status='retryable_failed',next_attempt_at=?,last_failure_category=?,
                            last_failure_code=?,updated_at=?,completed_at=NULL
                        WHERE outbox_id=?
                        """,
                        (
                            retry_at,
                            failure_category,
                            failure_code,
                            recorded_at,
                            outbox_id,
                        ),
                    )
                else:
                    conn.execute(
                        """
                        UPDATE base_projection_outbox
                        SET status='permanent_failed',next_attempt_at=NULL,last_failure_category=?,
                            last_failure_code=?,updated_at=?,completed_at=?
                        WHERE outbox_id=?
                        """,
                        (
                            failure_category,
                            failure_code,
                            recorded_at,
                            recorded_at,
                            outbox_id,
                        ),
                    )
                recorded_run_id = outbox["projection_run_id"]
                _refresh_projection_run(
                    conn,
                    recorded_run_id,
                    now=recorded_at,
                    failure_category=failure_category,
                    failure_code=failure_code,
                )
    except sqlite3.IntegrityError as exc:
        # The transaction above serializes normal callers. Normalize any final
        # trigger/unique-index race rather than exposing SQLite implementation text.
        existing = conn.execute(
            """
            SELECT a.*,o.projection_run_id
            FROM base_projection_delivery_attempts a
            JOIN base_projection_outbox o ON o.outbox_id=a.outbox_id
            WHERE a.idempotency_key=? AND o.student_id=?
            """,
            (idempotency_key, student_id),
        ).fetchone()
        if existing:
            if existing["result_sha256"] != result_sha256:
                raise ProjectionConflict(
                    "idempotency_key already belongs to a different delivery result"
                ) from None
            duplicate_attempt = existing
        else:
            any_owner = conn.execute(
                """
                SELECT result_sha256 FROM base_projection_delivery_attempts
                WHERE idempotency_key=?
                """,
                (idempotency_key,),
            ).fetchone()
            if any_owner:
                raise ProjectionConflict(
                    "idempotency_key already belongs to a different delivery result"
                ) from None
            raise ProjectionStateError(
                "delivery receipt conflicted with local projection state; retry with the same idempotency_key"
            ) from exc
    except sqlite3.OperationalError as exc:
        if "locked" in str(exc).casefold() or "busy" in str(exc).casefold():
            raise ProjectionStateError(
                "delivery receipt is busy; retry with the same idempotency_key"
            ) from None
        raise

    if duplicate_attempt is not None:
        return {
            "status": "duplicate",
            "delivery_attempt": dict(duplicate_attempt),
            "run": projection_run_detail(
                conn,
                duplicate_attempt["projection_run_id"],
                student_id=student_id,
            ),
        }
    if recorded_run_id is None:  # defensive: every non-duplicate path records one run
        raise ProjectionStateError("delivery receipt did not resolve to a projection run")
    attempt = conn.execute(
        "SELECT * FROM base_projection_delivery_attempts WHERE delivery_attempt_id=?",
        (delivery_attempt_id,),
    ).fetchone()
    return {
        "status": "recorded",
        "delivery_attempt": dict(attempt),
        "run": projection_run_detail(
            conn,
            recorded_run_id,
            student_id=student_id,
        ),
    }


def projection_run_detail(
    conn: sqlite3.Connection,
    projection_run_id: str,
    *,
    student_id: str,
) -> dict[str, Any]:
    student_id = _normalize_student_id(student_id)
    run = conn.execute(
        "SELECT * FROM base_projection_runs WHERE projection_run_id=? AND student_id=?",
        (projection_run_id, student_id),
    ).fetchone()
    if not run:
        raise ProjectionStateError(
            f"Unknown projection_run_id for student {student_id}: {projection_run_id}"
        )
    result = dict(run)
    records: list[dict[str, Any]] = []
    for row in conn.execute(
        """
        SELECT o.*,s.remote_record_id
        FROM base_projection_outbox o
        LEFT JOIN base_projection_state s
          ON s.projection_upsert_key=o.projection_upsert_key
        WHERE o.projection_run_id=? ORDER BY o.record_no
        """,
        (projection_run_id,),
    ):
        item = dict(row)
        item["fields"] = json.loads(item.pop("payload_json"))
        item["delivery_attempts"] = [
            dict(attempt)
            for attempt in conn.execute(
                """
                SELECT delivery_attempt_id,idempotency_key,result_sha256,attempt_no,
                       outcome,remote_record_id,readback_payload_sha256,
                       failure_category,failure_code,retry_at,recorded_at
                FROM base_projection_delivery_attempts
                WHERE outbox_id=? ORDER BY attempt_no
                """,
                (item["outbox_id"],),
            )
        ]
        records.append(item)
    result["records"] = records
    result["status_counts"] = {
        row["status"]: int(row["count"])
        for row in conn.execute(
            """
            SELECT status,COUNT(*) count FROM base_projection_outbox
            WHERE projection_run_id=? GROUP BY status
            """,
            (projection_run_id,),
        )
    }
    return result
