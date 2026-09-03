from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import unicodedata
from datetime import UTC, datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable

from .util import canonical_json, payload_hash, stable_id, utc_now


QUESTION_BANK_NAMESPACE = "shanghai_question_bank"
SELECTION_ALGORITHM_VERSION = "verified-question-selection-v1"
PUBLIC_EXPLANATION_CACHE_KEY_VERSION = "public-question-explanation-cache-v1"
DEFAULT_EXPLANATION_SCHEMA_VERSION = "public-question-explanation-v1"
DEFAULT_EXPLANATION_POLICY_VERSION = "opentutor-public-explanation-v1"
DEFAULT_RUBRIC_VERSION = "english-general-rubric-v1"

CONFIRMED_QUESTION_STATUSES = frozenset({"source_checked", "verified"})
REUSABLE_EXPLANATION_STATUSES = frozenset({"source_checked", "teacher_confirmed"})
VERIFIED_REAL_SOURCE_MODES = frozenset(
    {
        "inline_analysis",
        "scanned_textbook_ocr",
        "scanned_pdf_pair",
        "question_only",
        "inline_analysis_only",
    }
)
VERIFIED_SOURCE_PROCESSING_STATUSES = frozenset(
    {"completed", "ocr_structured", "source_checked", "verified"}
)
SELECTION_REASON_CODES = frozenset(
    {
        "due_retest",
        "repeated_weakness_repair",
        "transfer_check",
        "exam_coverage",
        "course_stage_core",
        "maintenance_sample",
        "teacher_pinned",
        "exact_retest",
        "same_skill_new_item",
    }
)
TRAINING_MODES = frozenset({"correction", "transfer", "assessment", "coverage"})
SELECTION_REQUEST_FIELDS = frozenset(
    {
        "student_id",
        "subject_code",
        "generation_id",
        "training_mode",
        "data_as_of",
        "candidate_question_ids",
        "candidate_context",
        "target_knowledge_codes",
        "max_questions",
        "max_groups",
        "duplicate_window_days",
        "near_duplicate_threshold",
        "allow_exact_retests",
        "recent_question_ids",
        "recent_passage_ids",
        "random_seed",
        "idempotency_key",
        "explanation_contract",
    }
)
CANDIDATE_CONTEXT_FIELDS = frozenset(
    {"reason_codes", "knowledge_codes", "evidence_references", "priority"}
)
EVIDENCE_REFERENCE_FIELDS = frozenset({"entity_type", "entity_id", "as_of"})
EVIDENCE_ENTITY_TYPES = frozenset(
    {
        "attempt",
        "review_task",
        "knowledge_evidence",
        "assessment",
        "course_stage",
        "teacher_target",
        "teacher_policy",
        "artifact",
        "session",
    }
)
PUBLIC_EXPLANATION_REQUEST_FIELDS = frozenset(
    {
        "question_id",
        "explanation_status",
        "explanation",
        "created_by",
        "confirmed_by",
        "explanation_contract",
    }
)
EXPLANATION_CONTRACT_FIELDS = frozenset(
    {
        "rubric_version",
        "rubric",
        "policy_version",
        "explanation_policy_version",
        "schema_version",
        "explanation_schema_version",
    }
)

_PASSAGE_TYPE_TOKENS = (
    "语法填空",
    "完形",
    "阅读",
    "六选五",
    "选句填空",
    "grammar fill",
    "grammar cloze",
    "cloze",
    "reading",
    "six of seven",
    "sentence insertion",
    "summary",
    "概要",
)
_OPTION_TYPE_TOKENS = (
    "完形",
    "阅读理解",
    "六选五",
    "选句填空",
    "选择",
    "cloze",
    "reading comprehension",
    "multiple choice",
    "six of seven",
    "sentence insertion",
)
_GENERATED_SOURCE_TOKENS = (
    "generated",
    "synthetic",
    "adapted",
    "model_generated",
    "ai_generated",
    "仿写",
    "改编",
    "生成",
)
_FORBIDDEN_PUBLIC_EXPLANATION_KEYS = frozenset(
    {
        "student",
        "student_id",
        "student_answer",
        "learner",
        "learner_id",
        "attempt",
        "attempt_id",
        "diagnosis",
        "personalized_diagnosis",
        "review_task_id",
        "student_history",
        "student_specific",
        "student_diagnosis",
        "student_error",
        "personalization",
    }
)
_FORBIDDEN_PUBLIC_KEY_FRAGMENTS = (
    "student_",
    "learner_",
    "attempt_",
    "diagnosis",
    "personalized",
    "学生",
    "学员",
    "本次作答",
    "个性化诊断",
)
_FORBIDDEN_PUBLIC_VALUE_PATTERNS = (
    re.compile(r"(?i)(?<![A-Z0-9])STU-[A-Z0-9][A-Z0-9._-]*"),
    re.compile(r"(?i)(?<![A-Z0-9])ATT(?:EMPT)?-[A-Z0-9][A-Z0-9._-]*"),
    re.compile(r"(?i)\b(?:private|file)://"),
    re.compile(r"(?i)(?<![A-Z0-9])[A-Z]:[\\/](?:[^\s]+)"),
    re.compile(r"\\\\[^\\\s]+\\[^\\\s]+"),
    re.compile(
        r"(?i)\bthis\s+(?:student|learner)\s+"
        r"(?:answered|chose|selected|wrote|responded)\b"
    ),
    re.compile(r"(?:该学生|这位学生|该学员|这位学员|本次作答)"),
)


def _contains_student_display_name(value: str, display_names: frozenset[str]) -> bool:
    folded = value.casefold()
    return any(display_name in folded for display_name in display_names)


class SelectionManifestConflict(ValueError):
    """Raised when an idempotency or immutable-cache identity is reused differently."""


def _row_value(row: sqlite3.Row | dict[str, Any] | None, key: str, default: Any = None) -> Any:
    if row is None:
        return default
    if isinstance(row, dict):
        return row.get(key, default)
    return row[key] if key in row.keys() else default


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
    )


def _parse_datetime(value: str, *, field: str) -> datetime:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError(f"{field} is required")
    normalized = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        result = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO-8601 timestamp") from exc
    if result.tzinfo is None:
        result = result.replace(tzinfo=UTC)
    return result.astimezone(UTC)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _normalize_whitespace(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _normalize_duplicate_text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return re.sub(r"[\W_]+", "", text, flags=re.UNICODE)


def _readonly_question_bank(path: str | Path) -> tuple[Path, sqlite3.Connection, str]:
    source_path = Path(path).expanduser().resolve()
    if not source_path.is_file():
        raise ValueError(f"Question bank not found: {source_path}")
    snapshot_sha256 = _sha256_file(source_path)
    conn = sqlite3.connect(f"file:{source_path.as_posix()}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    required = {"questions", "passages", "sources"}
    missing = sorted(table for table in required if not _table_exists(conn, table))
    if missing:
        conn.close()
        raise ValueError(f"Question bank is missing required table(s): {', '.join(missing)}")
    return source_path, conn, snapshot_sha256


def _require_enrollment(conn: sqlite3.Connection, student_id: str, subject_code: str) -> None:
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


def _requires_passage(question_type: Any) -> bool:
    value = _normalize_whitespace(question_type).casefold()
    return any(token in value for token in _PASSAGE_TYPE_TOKENS)


def _requires_options(question_type: Any) -> bool:
    value = _normalize_whitespace(question_type).casefold()
    return any(token in value for token in _OPTION_TYPE_TOKENS)


def _load_question(qconn: sqlite3.Connection, question_id: str) -> sqlite3.Row | None:
    return qconn.execute(
        "SELECT * FROM questions WHERE question_id=?",
        (question_id,),
    ).fetchone()


def _load_passage(qconn: sqlite3.Connection, passage_id: str | None) -> sqlite3.Row | None:
    if not passage_id:
        return None
    return qconn.execute(
        "SELECT * FROM passages WHERE passage_id=?",
        (passage_id,),
    ).fetchone()


def _load_source(qconn: sqlite3.Connection, source_id: str | None) -> sqlite3.Row | None:
    if not source_id:
        return None
    return qconn.execute(
        "SELECT * FROM sources WHERE source_id=?",
        (source_id,),
    ).fetchone()


def _load_options(qconn: sqlite3.Connection, question_id: str) -> list[dict[str, Any]]:
    if not _table_exists(qconn, "options"):
        return []
    columns = {str(row[1]) for row in qconn.execute("PRAGMA table_info(options)")}
    order = "option_order, option_label" if "option_order" in columns else "option_label"
    return [
        dict(row)
        for row in qconn.execute(
            f"SELECT * FROM options WHERE question_id=? ORDER BY {order}",
            (question_id,),
        )
    ]


def _source_id(question: sqlite3.Row, passage: sqlite3.Row | None) -> str:
    return _normalize_whitespace(
        _row_value(question, "source_id") or _row_value(passage, "source_id")
    )


def _source_locator(
    question: sqlite3.Row,
    passage: sqlite3.Row | None,
    source: sqlite3.Row | None,
) -> dict[str, Any]:
    source_path = (
        _row_value(question, "source_path")
        or _row_value(source, "original_path")
        or _row_value(source, "pdf_original_path")
    )
    source_page = _row_value(question, "source_page") or _row_value(passage, "source_page")
    return {
        "namespace": QUESTION_BANK_NAMESPACE,
        "question_id": _row_value(question, "question_id"),
        "passage_id": _row_value(question, "passage_id"),
        "source_id": _source_id(question, passage),
        "source_path": _normalize_whitespace(source_path) or None,
        "source_page": source_page,
        "original_number": _row_value(question, "original_number"),
    }


def _source_is_real(source: sqlite3.Row | None) -> bool:
    if source is None:
        return False
    mode = _normalize_whitespace(_row_value(source, "source_mode")).casefold()
    processing_status = _normalize_whitespace(
        _row_value(source, "processing_status")
    ).casefold()
    if mode not in VERIFIED_REAL_SOURCE_MODES:
        return False
    if processing_status and processing_status not in VERIFIED_SOURCE_PROCESSING_STATUSES:
        return False
    verification_fields = (
        "verification_status",
        "source_verification_status",
        "validation_status",
    )
    for field in verification_fields:
        if field not in source.keys():
            continue
        value = _normalize_whitespace(_row_value(source, field)).casefold()
        if value and value not in CONFIRMED_QUESTION_STATUSES:
            return False
    notes = _normalize_whitespace(_row_value(source, "notes")).casefold()
    combined = f"{mode} {notes}"
    return not any(token in combined for token in _GENERATED_SOURCE_TOKENS)


def _question_prompt_payload(
    question: sqlite3.Row,
    passage: sqlite3.Row | None,
    options: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "passage_id": _row_value(question, "passage_id"),
        "passage_text": _normalize_whitespace(_row_value(passage, "passage_text")),
        "question_type": _normalize_whitespace(_row_value(question, "question_type")),
        "original_number": _normalize_whitespace(_row_value(question, "original_number")),
        "stem": _normalize_whitespace(_row_value(question, "stem")),
        "options": [
            {
                "label": _normalize_whitespace(option.get("option_label")),
                "text": _normalize_whitespace(option.get("option_text")),
            }
            for option in options
        ],
    }


def _question_identity(
    question: sqlite3.Row,
    passage: sqlite3.Row | None,
    source: sqlite3.Row | None,
    options: list[dict[str, Any]],
) -> dict[str, Any]:
    prompt = _question_prompt_payload(question, passage, options)
    answer = _normalize_whitespace(_row_value(question, "answer"))
    duplicate_text = "\n".join(
        [
            prompt["passage_text"],
            prompt["stem"],
            *[f"{option['label']} {option['text']}" for option in prompt["options"]],
        ]
    )
    locator = _source_locator(question, passage, source)
    return {
        "question_id": str(_row_value(question, "question_id")),
        "passage_id": _normalize_whitespace(_row_value(question, "passage_id")) or None,
        "source_id": _source_id(question, passage),
        "question_type": _normalize_whitespace(_row_value(question, "question_type")),
        "original_number": _row_value(question, "original_number"),
        "verification_status": _normalize_whitespace(
            _row_value(question, "verification_status")
        ),
        "source_locator": locator,
        "question_content_sha256": payload_hash(prompt),
        "standard_answer_sha256": payload_hash({"answer": answer}),
        "duplicate_exact_sha256": hashlib.sha256(
            _normalize_duplicate_text(duplicate_text).encode("utf-8")
        ).hexdigest(),
        "normalized_duplicate_text": _normalize_duplicate_text(duplicate_text),
        "source_labels": {
            "primary": _row_value(question, "primary_test_point"),
            "secondary": _row_value(question, "secondary_test_points"),
        },
    }


def _question_issues(
    question: sqlite3.Row,
    passage: sqlite3.Row | None,
    source: sqlite3.Row | None,
    options: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    status = _normalize_whitespace(_row_value(question, "verification_status"))
    if status not in CONFIRMED_QUESTION_STATUSES:
        issues.append({"reason_code": "unverified_question", "status": status or None})
    if not _normalize_whitespace(_row_value(question, "stem")):
        issues.append({"reason_code": "incomplete_question", "field": "stem"})
    if not _normalize_whitespace(_row_value(question, "answer")):
        issues.append({"reason_code": "missing_standard_answer", "field": "answer"})
    if not _source_is_real(source):
        issues.append({"reason_code": "not_real_question", "field": "source"})
    locator = _source_locator(question, passage, source)
    if not locator["source_id"] or not locator["source_path"]:
        issues.append({"reason_code": "missing_source_locator", "locator": locator})
    if _requires_options(_row_value(question, "question_type")):
        if not options or any(not _normalize_whitespace(option.get("option_text")) for option in options):
            issues.append({"reason_code": "incomplete_question", "field": "options"})
    return issues


def _ordered_passage_questions(
    qconn: sqlite3.Connection,
    passage_id: str,
) -> list[sqlite3.Row]:
    columns = {str(row[1]) for row in qconn.execute("PRAGMA table_info(questions)")}
    order_parts = [
        part
        for column, part in (
            ("source_ordinal", "source_ordinal"),
            ("original_number", "original_number"),
            ("question_id", "question_id"),
        )
        if column in columns
    ]
    order_sql = ", ".join(order_parts) or "rowid"
    return list(
        qconn.execute(
            f"SELECT * FROM questions WHERE passage_id=? ORDER BY {order_sql}",
            (passage_id,),
        )
    )


def _build_group(
    qconn: sqlite3.Connection,
    requested_question: sqlite3.Row,
) -> tuple[dict[str, Any] | None, str | None, dict[str, Any]]:
    passage_id = _normalize_whitespace(_row_value(requested_question, "passage_id")) or None
    if _requires_passage(_row_value(requested_question, "question_type")) and not passage_id:
        return None, "incomplete_passage", {"issue": "passage_id is required for this type"}

    if passage_id:
        passage = _load_passage(qconn, passage_id)
        questions = _ordered_passage_questions(qconn, passage_id)
        if not passage or not questions:
            return None, "incomplete_passage", {"passage_id": passage_id, "issue": "missing passage or questions"}
        passage_status = _normalize_whitespace(_row_value(passage, "verification_status"))
        passage_text = _normalize_whitespace(_row_value(passage, "passage_text"))
        if passage_status not in CONFIRMED_QUESTION_STATUSES or not passage_text:
            return None, "incomplete_passage", {
                "passage_id": passage_id,
                "passage_verification_status": passage_status or None,
                "has_passage_text": bool(passage_text),
            }
        group_kind = "passage"
    else:
        passage = None
        questions = [requested_question]
        group_kind = "item"

    identities: list[dict[str, Any]] = []
    member_issues: list[dict[str, Any]] = []
    source_ids: set[str] = set()
    for question in questions:
        question_passage = passage if _row_value(question, "passage_id") else None
        source_id = _source_id(question, question_passage)
        source = _load_source(qconn, source_id)
        options = _load_options(qconn, str(_row_value(question, "question_id")))
        issues = _question_issues(question, question_passage, source, options)
        if issues:
            member_issues.append(
                {
                    "question_id": _row_value(question, "question_id"),
                    "issues": issues,
                }
            )
        identity = _question_identity(question, question_passage, source, options)
        identities.append(identity)
        if source_id:
            source_ids.add(source_id)

    if member_issues:
        if group_kind == "passage":
            return None, "incomplete_passage", {
                "passage_id": passage_id,
                "expected_question_count": len(questions),
                "member_issues": member_issues,
            }
        first_reason = member_issues[0]["issues"][0]["reason_code"]
        return None, first_reason, {"member_issues": member_issues}
    if len(source_ids) != 1:
        reason = "incomplete_passage" if group_kind == "passage" else "missing_source_locator"
        return None, reason, {"source_ids": sorted(source_ids)}

    group_prompt = {
        "group_kind": group_kind,
        "passage_id": passage_id,
        "passage_text": _normalize_whitespace(_row_value(passage, "passage_text")),
        "questions": [
            {
                "question_id": identity["question_id"],
                "question_content_sha256": identity["question_content_sha256"],
            }
            for identity in identities
        ],
    }
    normalized_text = _normalize_duplicate_text(
        "\n".join(identity["normalized_duplicate_text"] for identity in identities)
    )
    first = identities[0]
    group_key = f"passage:{passage_id}" if passage_id else f"question:{first['question_id']}"
    return (
        {
            "group_key": group_key,
            "group_kind": group_kind,
            "passage_id": passage_id,
            "source_id": next(iter(source_ids)),
            "source_locator": {
                "namespace": QUESTION_BANK_NAMESPACE,
                "source_id": next(iter(source_ids)),
                "passage_id": passage_id,
                "source_path": first["source_locator"]["source_path"],
                "source_page": _row_value(passage, "source_page")
                if passage
                else first["source_locator"]["source_page"],
            },
            "passage_content_sha256": (
                payload_hash({"passage_text": _normalize_whitespace(_row_value(passage, "passage_text"))})
                if passage
                else None
            ),
            "group_content_sha256": payload_hash(group_prompt),
            "duplicate_exact_sha256": hashlib.sha256(normalized_text.encode("utf-8")).hexdigest(),
            "normalized_duplicate_text": normalized_text,
            "questions": identities,
            "expected_question_count": len(identities),
        },
        None,
        {},
    )


def _knowledge_bundle(
    conn: sqlite3.Connection,
    identity: dict[str, Any],
) -> dict[str, Any]:
    mappings: list[dict[str, Any]] = []
    question_id = identity["question_id"]
    if _table_exists(conn, "question_deep_knowledge_map"):
        mappings.extend(
            dict(row)
            for row in conn.execute(
                """
                SELECT kp.code, qm.role, qm.mapping_source, qm.confidence,
                       qm.verification_status, qm.rationale
                FROM question_deep_knowledge_map qm
                JOIN knowledge_points kp ON kp.knowledge_point_id=qm.knowledge_point_id
                WHERE qm.question_id=? AND qm.verification_status<>'rejected'
                ORDER BY kp.code, qm.role, qm.mapping_source
                """,
                (question_id,),
            )
        )
    if _table_exists(conn, "question_knowledge_map") and _table_exists(conn, "source_snapshots"):
        mappings.extend(
            dict(row)
            for row in conn.execute(
                """
                SELECT kp.code, qm.role, qm.mapping_source, qm.confidence,
                       qm.verification_status, qm.rationale
                FROM question_knowledge_map qm
                JOIN source_snapshots ss
                  ON ss.source_snapshot_id=qm.source_snapshot_id
                 AND ss.namespace=? AND ss.is_current=1
                JOIN knowledge_points kp ON kp.knowledge_point_id=qm.knowledge_point_id
                WHERE qm.question_id=? AND qm.verification_status<>'rejected'
                ORDER BY kp.code, qm.role, qm.mapping_source
                """,
                (QUESTION_BANK_NAMESPACE, question_id),
            )
        )
    unique = {
        canonical_json(mapping): mapping
        for mapping in mappings
    }
    ordered = [unique[key] for key in sorted(unique)]
    confirmed_codes = sorted(
        {
            str(mapping["code"])
            for mapping in ordered
            if mapping.get("verification_status") in CONFIRMED_QUESTION_STATUSES
        }
    )
    value = {
        "source_labels": identity["source_labels"],
        "mappings": ordered,
    }
    return {
        **value,
        "confirmed_codes": confirmed_codes,
        "knowledge_mapping_sha256": payload_hash(value),
    }


def _normalize_explanation_contract(value: Any) -> dict[str, Any]:
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ValueError("explanation_contract must be an object")
    unknown_fields = sorted(set(value) - EXPLANATION_CONTRACT_FIELDS)
    if unknown_fields:
        raise ValueError(
            "Unknown explanation_contract field(s): " + ", ".join(unknown_fields)
        )
    rubric_version = _normalize_whitespace(value.get("rubric_version") or DEFAULT_RUBRIC_VERSION)
    policy_version = _normalize_whitespace(
        value.get("policy_version") or value.get("explanation_policy_version")
        or DEFAULT_EXPLANATION_POLICY_VERSION
    )
    schema_version = _normalize_whitespace(
        value.get("schema_version") or value.get("explanation_schema_version")
        or DEFAULT_EXPLANATION_SCHEMA_VERSION
    )
    if not rubric_version or not policy_version or not schema_version:
        raise ValueError("rubric, policy, and schema versions must be non-empty")
    rubric = value.get("rubric") or {}
    if not isinstance(rubric, dict):
        raise ValueError("explanation_contract.rubric must be an object")
    rubric_sha256 = payload_hash({"rubric_version": rubric_version, "rubric": rubric})
    return {
        "cache_key_version": PUBLIC_EXPLANATION_CACHE_KEY_VERSION,
        "rubric_version": rubric_version,
        "rubric": rubric,
        "rubric_sha256": rubric_sha256,
        "explanation_policy_version": policy_version,
        "explanation_schema_version": schema_version,
    }


def public_explanation_cache_key(
    *,
    question_id: str,
    source_snapshot_sha256: str,
    question_content_sha256: str,
    standard_answer_sha256: str,
    knowledge_mapping_sha256: str,
    rubric_sha256: str,
    explanation_policy_version: str,
    explanation_schema_version: str,
    cache_key_version: str = PUBLIC_EXPLANATION_CACHE_KEY_VERSION,
) -> str:
    """Return the stable cache key for public, non-student explanation content."""
    identity = {
        "cache_key_version": cache_key_version,
        "question_id": question_id,
        "source_snapshot_sha256": source_snapshot_sha256,
        "question_content_sha256": question_content_sha256,
        "standard_answer_sha256": standard_answer_sha256,
        "knowledge_mapping_sha256": knowledge_mapping_sha256,
        "rubric_sha256": rubric_sha256,
        "explanation_policy_version": explanation_policy_version,
        "explanation_schema_version": explanation_schema_version,
    }
    return payload_hash(identity)


def _cache_identity(
    identity: dict[str, Any],
    knowledge: dict[str, Any],
    source_snapshot_sha256: str,
    contract: dict[str, Any],
) -> dict[str, Any]:
    cache_key = public_explanation_cache_key(
        question_id=identity["question_id"],
        source_snapshot_sha256=source_snapshot_sha256,
        question_content_sha256=identity["question_content_sha256"],
        standard_answer_sha256=identity["standard_answer_sha256"],
        knowledge_mapping_sha256=knowledge["knowledge_mapping_sha256"],
        rubric_sha256=contract["rubric_sha256"],
        explanation_policy_version=contract["explanation_policy_version"],
        explanation_schema_version=contract["explanation_schema_version"],
        cache_key_version=contract["cache_key_version"],
    )
    return {
        "cache_key": cache_key,
        "cache_key_version": contract["cache_key_version"],
        "question_id": identity["question_id"],
        "source_snapshot_sha256": source_snapshot_sha256,
        "question_content_sha256": identity["question_content_sha256"],
        "standard_answer_sha256": identity["standard_answer_sha256"],
        "knowledge_mapping_sha256": knowledge["knowledge_mapping_sha256"],
        "rubric_version": contract["rubric_version"],
        "rubric_sha256": contract["rubric_sha256"],
        "explanation_policy_version": contract["explanation_policy_version"],
        "explanation_schema_version": contract["explanation_schema_version"],
    }


def _context_for_candidates(
    conn: sqlite3.Connection,
    candidate_ids: list[str],
    raw_context: Any,
) -> dict[str, dict[str, Any]]:
    if not isinstance(raw_context, dict):
        raise ValueError("candidate_context must be an object keyed by question_id")
    unexpected_context_ids = sorted(set(raw_context) - set(candidate_ids))
    if unexpected_context_ids:
        raise ValueError(
            "candidate_context contains unrequested question_id(s): "
            + ", ".join(unexpected_context_ids)
        )
    known_codes = {
        str(row["code"])
        for row in conn.execute("SELECT code FROM knowledge_points WHERE active=1")
    }
    result: dict[str, dict[str, Any]] = {}
    for index, question_id in enumerate(candidate_ids):
        value = raw_context.get(question_id)
        if not isinstance(value, dict):
            raise ValueError(f"candidate_context.{question_id} must be an object")
        unknown_fields = sorted(set(value) - CANDIDATE_CONTEXT_FIELDS)
        if unknown_fields:
            raise ValueError(
                f"Unknown candidate_context.{question_id} field(s): "
                + ", ".join(unknown_fields)
            )
        raw_reasons = value.get("reason_codes")
        if not isinstance(raw_reasons, list):
            raise ValueError(f"candidate_context.{question_id}.reason_codes must be a list")
        reasons = list(
            dict.fromkeys(str(item).strip() for item in raw_reasons if str(item).strip())
        )
        if not reasons:
            raise ValueError(f"candidate_context.{question_id}.reason_codes is required")
        unknown_reasons = sorted(set(reasons) - SELECTION_REASON_CODES)
        if unknown_reasons:
            raise ValueError(
                f"Unknown selection reason code(s) for {question_id}: {', '.join(unknown_reasons)}"
            )
        raw_knowledge_codes = value.get("knowledge_codes", [])
        if not isinstance(raw_knowledge_codes, list):
            raise ValueError(f"candidate_context.{question_id}.knowledge_codes must be a list")
        knowledge_codes = list(
            dict.fromkeys(
                str(item).strip()
                for item in raw_knowledge_codes
                if str(item).strip()
            )
        )
        unknown_codes = sorted(set(knowledge_codes) - known_codes)
        if unknown_codes:
            raise ValueError(
                f"Unknown knowledge-point code(s) for {question_id}: {', '.join(unknown_codes)}"
            )
        evidence = value.get("evidence_references") or []
        if not isinstance(evidence, list):
            raise ValueError(f"candidate_context.{question_id}.evidence_references must be a list")
        normalized_evidence: list[dict[str, Any]] = []
        for evidence_index, reference in enumerate(evidence):
            prefix = f"candidate_context.{question_id}.evidence_references[{evidence_index}]"
            if not isinstance(reference, dict):
                raise ValueError(f"{prefix} must be an object")
            unknown_reference_fields = sorted(set(reference) - EVIDENCE_REFERENCE_FIELDS)
            if unknown_reference_fields:
                raise ValueError(
                    f"Unknown {prefix} field(s): " + ", ".join(unknown_reference_fields)
                )
            entity_type = _normalize_whitespace(reference.get("entity_type"))
            entity_id = _normalize_whitespace(reference.get("entity_id"))
            if entity_type not in EVIDENCE_ENTITY_TYPES:
                raise ValueError(
                    f"{prefix}.entity_type must be one of: "
                    + ", ".join(sorted(EVIDENCE_ENTITY_TYPES))
                )
            if not entity_id or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", entity_id):
                raise ValueError(f"{prefix}.entity_id must be an opaque identifier")
            as_of = reference.get("as_of")
            normalized_reference: dict[str, Any] = {
                "entity_type": entity_type,
                "entity_id": entity_id,
            }
            if as_of is not None:
                as_of_value = _normalize_whitespace(as_of)
                _parse_datetime(as_of_value, field=f"{prefix}.as_of")
                normalized_reference["as_of"] = as_of_value
            normalized_evidence.append(normalized_reference)
        try:
            priority = float(value.get("priority", 0))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"candidate_context.{question_id}.priority must be numeric") from exc
        result[question_id] = {
            "reason_codes": reasons,
            "knowledge_codes": knowledge_codes,
            "evidence_references": normalized_evidence,
            "priority": priority,
            "candidate_ordinal": index + 1,
        }
    return result


def _aggregate_group_context(
    requested_ids: list[str],
    contexts: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    values = [contexts[question_id] for question_id in requested_ids]
    return {
        "reason_codes": sorted({item for value in values for item in value["reason_codes"]}),
        "knowledge_codes": sorted(
            {item for value in values for item in value["knowledge_codes"]}
        ),
        "evidence_references": [
            item for value in values for item in value["evidence_references"]
        ],
        "priority": max(value["priority"] for value in values),
        "candidate_ordinal": min(value["candidate_ordinal"] for value in values),
    }


def _duplicate_relations(qconn: sqlite3.Connection) -> dict[frozenset[str], str]:
    if not _table_exists(qconn, "duplicate_map"):
        return {}
    result: dict[frozenset[str], str] = {}
    for row in qconn.execute("SELECT * FROM duplicate_map"):
        first = _normalize_whitespace(_row_value(row, "canonical_question_id"))
        second = _normalize_whitespace(_row_value(row, "duplicate_question_id"))
        if not first or not second:
            continue
        kind = _normalize_whitespace(_row_value(row, "similarity_type")).casefold()
        result[frozenset({first, second})] = "near_duplicate" if "near" in kind else "exact_duplicate"
    return result


def _within_history_window(value: str, start: datetime, end: datetime) -> bool:
    try:
        parsed = _parse_datetime(value, field="history timestamp")
    except ValueError:
        return False
    return start <= parsed <= end


def _recent_history(
    conn: sqlite3.Connection,
    *,
    student_id: str,
    data_as_of: datetime,
    duplicate_window_days: int,
    recent_question_ids: Iterable[str],
    recent_passage_ids: Iterable[str],
) -> dict[str, Any]:
    start = data_as_of - timedelta(days=duplicate_window_days)
    question_ids = {_normalize_whitespace(value) for value in recent_question_ids if _normalize_whitespace(value)}
    passage_ids = {_normalize_whitespace(value) for value in recent_passage_ids if _normalize_whitespace(value)}
    question_hashes: set[str] = set()
    group_hashes: set[str] = set()

    for row in conn.execute(
        """
        SELECT er.external_id, er.external_parent_id, a.attempted_at
        FROM attempts a
        JOIN external_references er ON er.item_id=a.item_id
        WHERE a.student_id=? AND a.record_status='active'
          AND er.namespace=? AND er.reference_type='question_id'
        """,
        (student_id, QUESTION_BANK_NAMESPACE),
    ):
        if _within_history_window(str(row["attempted_at"]), start, data_as_of):
            question_ids.add(str(row["external_id"]))
            if row["external_parent_id"]:
                passage_ids.add(str(row["external_parent_id"]))

    for row in conn.execute(
        """
        SELECT m.created_at, g.passage_id, g.group_content_sha256,
               i.question_id, i.question_content_sha256
        FROM question_selection_manifests m
        JOIN question_selection_groups g
          ON g.selection_manifest_id=m.selection_manifest_id
        JOIN question_selection_items i
          ON i.selection_manifest_id=m.selection_manifest_id
         AND i.selection_group_id=g.selection_group_id
        WHERE m.student_id=? AND m.status='finalized'
        """,
        (student_id,),
    ):
        if not _within_history_window(str(row["created_at"]), start, data_as_of):
            continue
        question_ids.add(str(row["question_id"]))
        question_hashes.add(str(row["question_content_sha256"]))
        group_hashes.add(str(row["group_content_sha256"]))
        if row["passage_id"]:
            passage_ids.add(str(row["passage_id"]))
    return {
        "start": start,
        "end": data_as_of,
        "question_ids": question_ids,
        "passage_ids": passage_ids,
        "question_hashes": question_hashes,
        "group_hashes": group_hashes,
    }


def _history_groups(
    qconn: sqlite3.Connection,
    history: dict[str, Any],
) -> list[dict[str, Any]]:
    representatives: dict[str, dict[str, Any]] = {}
    for passage_id in sorted(history["passage_ids"]):
        question = qconn.execute(
            "SELECT * FROM questions WHERE passage_id=? ORDER BY question_id LIMIT 1",
            (passage_id,),
        ).fetchone()
        if question:
            group, _, _ = _build_group(qconn, question)
            if group:
                representatives[group["group_key"]] = group
    for question_id in sorted(history["question_ids"]):
        question = _load_question(qconn, question_id)
        if question:
            group, _, _ = _build_group(qconn, question)
            if group:
                representatives[group["group_key"]] = group
    return list(representatives.values())


def _relation_from_duplicate_map(
    candidate: dict[str, Any],
    other: dict[str, Any],
    relations: dict[frozenset[str], str],
) -> tuple[str | None, str | None]:
    for candidate_id in (item["question_id"] for item in candidate["questions"]):
        for other_id in (item["question_id"] for item in other["questions"]):
            relation = relations.get(frozenset({candidate_id, other_id}))
            if relation:
                return relation, other_id
    return None, None


def _duplicate_check(
    candidate: dict[str, Any],
    others: list[dict[str, Any]],
    *,
    exact_question_ids: set[str],
    exact_passage_ids: set[str],
    question_hashes: set[str],
    group_hashes: set[str],
    relations: dict[frozenset[str], str],
    near_duplicate_threshold: float,
) -> dict[str, Any]:
    candidate_ids = {item["question_id"] for item in candidate["questions"]}
    candidate_hashes = {item["question_content_sha256"] for item in candidate["questions"]}
    exact_ids = sorted(candidate_ids & exact_question_ids)
    exact_hashes = sorted(candidate_hashes & question_hashes)
    if candidate.get("passage_id") and candidate["passage_id"] in exact_passage_ids:
        return {
            "result": "exact_duplicate",
            "matched_question_id": exact_ids[0] if exact_ids else None,
            "matched_passage_id": candidate["passage_id"],
            "similarity_score": 1.0,
            "basis": "recent_passage_id",
        }
    if exact_ids or exact_hashes or candidate["group_content_sha256"] in group_hashes:
        return {
            "result": "exact_duplicate",
            "matched_question_id": exact_ids[0] if exact_ids else None,
            "similarity_score": 1.0,
            "basis": "stable_id_or_content_hash",
        }

    for other in others:
        other_ids = {item["question_id"] for item in other["questions"]}
        other_hashes = {item["question_content_sha256"] for item in other["questions"]}
        if (
            candidate["group_key"] == other["group_key"]
            or candidate["duplicate_exact_sha256"] == other["duplicate_exact_sha256"]
            or candidate_hashes & other_hashes
            or candidate_ids & other_ids
        ):
            matched = sorted(candidate_ids & other_ids)
            return {
                "result": "exact_duplicate",
                "matched_question_id": matched[0] if matched else other["questions"][0]["question_id"],
                "matched_passage_id": other.get("passage_id"),
                "similarity_score": 1.0,
                "basis": "selected_or_history_content",
            }
        relation, matched_id = _relation_from_duplicate_map(candidate, other, relations)
        if relation:
            return {
                "result": relation,
                "matched_question_id": matched_id,
                "matched_passage_id": other.get("passage_id"),
                "similarity_score": 1.0 if relation == "exact_duplicate" else None,
                "basis": "question_bank_duplicate_map",
            }
        left = candidate["normalized_duplicate_text"]
        right = other["normalized_duplicate_text"]
        if min(len(left), len(right)) < 32:
            continue
        similarity = SequenceMatcher(None, left, right, autojunk=False).ratio()
        if similarity >= near_duplicate_threshold:
            return {
                "result": "near_duplicate",
                "matched_question_id": other["questions"][0]["question_id"],
                "matched_passage_id": other.get("passage_id"),
                "similarity_score": round(similarity, 6),
                "basis": "normalized_sequence_similarity",
            }
    return {"result": "new_item", "similarity_score": None, "basis": "no_match"}


def _exclusion(
    *,
    question_id: str,
    reason_code: str,
    detail: dict[str, Any],
    group: dict[str, Any] | None,
) -> dict[str, Any]:
    identity = None
    if group:
        identity = next(
            (item for item in group["questions"] if item["question_id"] == question_id),
            group["questions"][0],
        )
    return {
        "candidate_key": f"question:{question_id}",
        "candidate_question_id": question_id,
        "candidate_passage_id": group.get("passage_id") if group else None,
        "source_id": group.get("source_id") if group else None,
        "source_locator": identity["source_locator"] if identity else {"question_id": question_id},
        "reason_code": reason_code,
        "detail": detail,
        "question_content_sha256": identity["question_content_sha256"] if identity else None,
        "matched_question_id": detail.get("matched_question_id"),
        "similarity_score": detail.get("similarity_score"),
    }


def _json_load(value: Any) -> Any:
    if value in {None, ""}:
        return None
    return json.loads(str(value))


def question_selection_manifest_detail(
    conn: sqlite3.Connection,
    selection_manifest_id: str,
    *,
    student_id: str,
) -> dict[str, Any]:
    explicit_student = _normalize_whitespace(student_id)
    if not explicit_student:
        raise ValueError("student_id is required")
    manifest_row = conn.execute(
        """
        SELECT * FROM question_selection_manifests
        WHERE selection_manifest_id=? AND student_id=?
        """,
        (selection_manifest_id, explicit_student),
    ).fetchone()
    if not manifest_row:
        raise ValueError(
            f"Unknown selection_manifest_id for student {explicit_student}: {selection_manifest_id}"
        )
    manifest = dict(manifest_row)
    for field in (
        "source_locator_json",
        "target_knowledge_json",
        "selection_policy_json",
        "explanation_contract_json",
        "coverage_json",
    ):
        manifest[field.removesuffix("_json")] = _json_load(manifest.pop(field))
    groups = []
    for row in conn.execute(
        """
        SELECT * FROM question_selection_groups
        WHERE selection_manifest_id=? ORDER BY ordinal
        """,
        (selection_manifest_id,),
    ):
        value = dict(row)
        for field in (
            "source_locator_json",
            "reason_codes_json",
            "knowledge_codes_json",
            "evidence_references_json",
            "duplicate_check_json",
        ):
            value[field.removesuffix("_json")] = _json_load(value.pop(field))
        groups.append(value)
    items = []
    for row in conn.execute(
        """
        SELECT * FROM question_selection_items
        WHERE selection_manifest_id=? ORDER BY ordinal
        """,
        (selection_manifest_id,),
    ):
        value = dict(row)
        for field in (
            "source_locator_json",
            "reason_codes_json",
            "knowledge_codes_json",
            "mapping_evidence_json",
            "duplicate_check_json",
        ):
            value[field.removesuffix("_json")] = _json_load(value.pop(field))
        items.append(value)
    exclusions = []
    for row in conn.execute(
        """
        SELECT * FROM question_selection_exclusions
        WHERE selection_manifest_id=? ORDER BY candidate_key, reason_code
        """,
        (selection_manifest_id,),
    ):
        value = dict(row)
        value["source_locator"] = _json_load(value.pop("source_locator_json"))
        value["detail"] = _json_load(value.pop("detail_json"))
        exclusions.append(value)
    return {
        "manifest": manifest,
        "groups": groups,
        "items": items,
        "exclusions": exclusions,
    }


def create_question_selection_manifest(
    conn: sqlite3.Connection,
    question_bank: str | Path,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Select verified real questions and persist an immutable, student-owned manifest.

    The caller supplies a structured candidate set and explicit reason/evidence metadata.
    This function never invokes a model and opens the external question bank read-only.
    """

    if not isinstance(payload, dict):
        raise ValueError("selection payload must be an object")
    unknown_request_fields = sorted(set(payload) - SELECTION_REQUEST_FIELDS)
    if unknown_request_fields:
        raise ValueError(
            "Unknown selection request field(s): " + ", ".join(unknown_request_fields)
        )
    student_id = _normalize_whitespace(payload.get("student_id"))
    subject_code = _normalize_whitespace(payload.get("subject_code") or "english").lower()
    training_mode = _normalize_whitespace(payload.get("training_mode"))
    data_as_of_raw = _normalize_whitespace(payload.get("data_as_of"))
    if not student_id or not training_mode or not data_as_of_raw:
        raise ValueError("student_id, training_mode, and data_as_of are required")
    if training_mode not in TRAINING_MODES:
        raise ValueError("training_mode must be correction, transfer, assessment, or coverage")
    if subject_code != "english":
        raise ValueError("The Shanghai question-bank selection module only supports subject_code=english")
    _require_enrollment(conn, student_id, subject_code)
    data_as_of = _parse_datetime(data_as_of_raw, field="data_as_of")

    raw_candidate_ids = payload.get("candidate_question_ids")
    if not isinstance(raw_candidate_ids, list) or not raw_candidate_ids:
        raise ValueError("candidate_question_ids must be a non-empty list")
    candidate_ids = list(
        dict.fromkeys(
            _normalize_whitespace(value)
            for value in raw_candidate_ids
            if _normalize_whitespace(value)
        )
    )
    if not candidate_ids:
        raise ValueError("candidate_question_ids must contain at least one ID")
    contexts = _context_for_candidates(conn, candidate_ids, payload.get("candidate_context"))

    raw_target_codes = payload.get("target_knowledge_codes") or []
    if not isinstance(raw_target_codes, list):
        raise ValueError("target_knowledge_codes must be a list")
    target_codes = list(
        dict.fromkeys(
            _normalize_whitespace(value)
            for value in raw_target_codes
            if _normalize_whitespace(value)
        )
    )
    known_codes = {
        str(row["code"])
        for row in conn.execute("SELECT code FROM knowledge_points WHERE active=1")
    }
    unknown_targets = sorted(set(target_codes) - known_codes)
    if unknown_targets:
        raise ValueError(f"Unknown target knowledge-point codes: {', '.join(unknown_targets)}")

    try:
        max_questions = int(payload.get("max_questions") or 1_000_000)
        max_groups = int(payload.get("max_groups") or len(candidate_ids))
        duplicate_window_days = int(payload.get("duplicate_window_days", 30))
        near_duplicate_threshold = float(payload.get("near_duplicate_threshold", 0.92))
    except (TypeError, ValueError) as exc:
        raise ValueError("selection limits and duplicate policy values must be numeric") from exc
    if max_questions < 1 or max_groups < 1:
        raise ValueError("max_questions and max_groups must be positive")
    if duplicate_window_days < 0:
        raise ValueError("duplicate_window_days cannot be negative")
    if not 0.8 <= near_duplicate_threshold <= 1.0:
        raise ValueError("near_duplicate_threshold must be between 0.8 and 1.0")
    raw_allow_exact_retests = payload.get("allow_exact_retests", False)
    if not isinstance(raw_allow_exact_retests, bool):
        raise ValueError("allow_exact_retests must be a boolean")
    allow_exact_retests = raw_allow_exact_retests
    if allow_exact_retests and training_mode != "correction":
        raise ValueError("allow_exact_retests is only valid in correction mode")
    raw_recent_question_ids = payload.get("recent_question_ids") or []
    raw_recent_passage_ids = payload.get("recent_passage_ids") or []
    if not isinstance(raw_recent_question_ids, list):
        raise ValueError("recent_question_ids must be a list")
    if not isinstance(raw_recent_passage_ids, list):
        raise ValueError("recent_passage_ids must be a list")
    recent_question_ids = list(
        dict.fromkeys(
            _normalize_whitespace(value)
            for value in raw_recent_question_ids
            if _normalize_whitespace(value)
        )
    )
    recent_passage_ids = list(
        dict.fromkeys(
            _normalize_whitespace(value)
            for value in raw_recent_passage_ids
            if _normalize_whitespace(value)
        )
    )
    explanation_contract = _normalize_explanation_contract(payload.get("explanation_contract"))
    selection_policy = {
        "only_verified_real_questions": True,
        "complete_passage_groups": True,
        "max_questions": max_questions,
        "max_groups": max_groups,
        "duplicate_window_days": duplicate_window_days,
        "near_duplicate_threshold": near_duplicate_threshold,
        "allow_exact_retests": allow_exact_retests,
        "recent_question_ids": recent_question_ids,
        "recent_passage_ids": recent_passage_ids,
    }

    source_path, qconn, source_snapshot_sha256 = _readonly_question_bank(question_bank)
    exclusions: list[dict[str, Any]] = []
    groups_by_key: dict[str, dict[str, Any]] = {}
    requested_by_group: dict[str, list[str]] = {}
    try:
        for question_id in candidate_ids:
            question = _load_question(qconn, question_id)
            if not question:
                exclusions.append(
                    _exclusion(
                        question_id=question_id,
                        reason_code="unknown_question",
                        detail={"question_id": question_id},
                        group=None,
                    )
                )
                continue
            group, reason, detail = _build_group(qconn, question)
            if not group:
                exclusions.append(
                    _exclusion(
                        question_id=question_id,
                        reason_code=str(reason),
                        detail=detail,
                        group=None,
                    )
                )
                continue
            groups_by_key[group["group_key"]] = group
            requested_by_group.setdefault(group["group_key"], []).append(question_id)

        history = _recent_history(
            conn,
            student_id=student_id,
            data_as_of=data_as_of,
            duplicate_window_days=duplicate_window_days,
            recent_question_ids=recent_question_ids,
            recent_passage_ids=recent_passage_ids,
        )
        history_groups = _history_groups(qconn, history)
        relations = _duplicate_relations(qconn)

        candidates: list[dict[str, Any]] = []
        for key, group in groups_by_key.items():
            group_context = _aggregate_group_context(requested_by_group[key], contexts)
            group["requested_question_ids"] = requested_by_group[key]
            group["context"] = group_context
            target_matches = len(set(group_context["knowledge_codes"]) & set(target_codes))
            teacher_pinned = int("teacher_pinned" in group_context["reason_codes"])
            group["sort_key"] = (
                -teacher_pinned,
                -target_matches,
                -group_context["priority"],
                group_context["candidate_ordinal"],
                key,
            )
            candidates.append(group)
        candidates.sort(key=lambda value: value["sort_key"])

        selected: list[dict[str, Any]] = []
        selected_questions = 0
        for group in candidates:
            within_manifest_duplicate = _duplicate_check(
                group,
                selected,
                exact_question_ids=set(),
                exact_passage_ids=set(),
                question_hashes=set(),
                group_hashes=set(),
                relations=relations,
                near_duplicate_threshold=near_duplicate_threshold,
            )
            if within_manifest_duplicate["result"] in {
                "exact_duplicate",
                "near_duplicate",
            }:
                for question_id in group["requested_question_ids"]:
                    exclusions.append(
                        _exclusion(
                            question_id=question_id,
                            reason_code=within_manifest_duplicate["result"],
                            detail={
                                **within_manifest_duplicate,
                                "scope": "current_manifest",
                            },
                            group=group,
                        )
                    )
                continue
            duplicate = _duplicate_check(
                group,
                history_groups,
                exact_question_ids=history["question_ids"],
                exact_passage_ids=history["passage_ids"],
                question_hashes=history["question_hashes"],
                group_hashes=history["group_hashes"],
                relations=relations,
                near_duplicate_threshold=near_duplicate_threshold,
            )
            requested_history_ids = sorted(
                set(group["requested_question_ids"]) & history["question_ids"]
            )
            if (
                duplicate["result"] == "exact_duplicate"
                and allow_exact_retests
                and requested_history_ids
            ):
                duplicate = {**duplicate, "result": "exact_retest", "allowed": True}
                duplicate["historical_question_ids"] = requested_history_ids
                if "exact_retest" not in group["context"]["reason_codes"]:
                    group["context"]["reason_codes"].append("exact_retest")
                    group["context"]["reason_codes"].sort()
            elif duplicate["result"] in {"exact_duplicate", "near_duplicate"}:
                for question_id in group["requested_question_ids"]:
                    exclusions.append(
                        _exclusion(
                            question_id=question_id,
                            reason_code=duplicate["result"],
                            detail=duplicate,
                            group=group,
                        )
                    )
                continue
            group["duplicate_check"] = duplicate
            if len(selected) >= max_groups:
                for question_id in group["requested_question_ids"]:
                    exclusions.append(
                        _exclusion(
                            question_id=question_id,
                            reason_code="group_limit",
                            detail={"max_groups": max_groups},
                            group=group,
                        )
                    )
                continue
            if selected_questions + group["expected_question_count"] > max_questions:
                for question_id in group["requested_question_ids"]:
                    exclusions.append(
                        _exclusion(
                            question_id=question_id,
                            reason_code="question_limit",
                            detail={
                                "max_questions": max_questions,
                                "selected_question_count": selected_questions,
                                "whole_group_question_count": group["expected_question_count"],
                                "complete_group_preserved": True,
                            },
                            group=group,
                        )
                    )
                continue
            selected.append(group)
            selected_questions += group["expected_question_count"]
    finally:
        qconn.close()

    if _sha256_file(source_path) != source_snapshot_sha256:
        raise RuntimeError("Read-only question-bank source changed during selection")

    source_locator = {
        "namespace": QUESTION_BANK_NAMESPACE,
        "path": str(source_path),
        "sha256": source_snapshot_sha256,
    }
    request_basis = {
        "student_id": student_id,
        "subject_code": subject_code,
        "generation_id": payload.get("generation_id"),
        "training_mode": training_mode,
        "data_as_of": data_as_of_raw,
        "candidate_question_ids": candidate_ids,
        "candidate_context": contexts,
        "target_knowledge_codes": target_codes,
        "selection_policy": selection_policy,
        "recent_question_ids": recent_question_ids,
        "recent_passage_ids": recent_passage_ids,
        "explanation_contract": explanation_contract,
        "source_snapshot_sha256": source_snapshot_sha256,
        "algorithm_version": SELECTION_ALGORITHM_VERSION,
        "random_seed": payload.get("random_seed"),
    }
    request_sha256 = payload_hash(request_basis)
    idempotency_key = _normalize_whitespace(
        payload.get("idempotency_key")
        or f"opentutor:question-selection:{request_sha256}:v1"
    )
    coverage_confirmed: dict[str, list[str]] = {code: [] for code in target_codes}
    coverage_context_only: dict[str, list[str]] = {code: [] for code in target_codes}
    for group in selected:
        group_codes = set(group["context"]["knowledge_codes"])
        for identity in group["questions"]:
            knowledge = _knowledge_bundle(conn, identity)
            identity["knowledge"] = knowledge
            identity["knowledge_codes"] = list(knowledge["confirmed_codes"])
            cache_identity = _cache_identity(
                identity,
                knowledge,
                source_snapshot_sha256,
                explanation_contract,
            )
            identity["explanation_cache"] = cache_identity
            cached = conn.execute(
                """
                SELECT explanation_status FROM public_question_explanations
                WHERE cache_key=?
                """,
                (cache_identity["cache_key"],),
            ).fetchone()
            identity["public_explanation_status"] = (
                str(cached["explanation_status"]) if cached else "not_generated"
            )
            for code in target_codes:
                if code in identity["knowledge_codes"]:
                    coverage_confirmed[code].append(identity["question_id"])
                elif (
                    code in group_codes
                    and identity["question_id"] in group["requested_question_ids"]
                ):
                    coverage_context_only[code].append(identity["question_id"])
    coverage = {
        "targets": [
            {
                "code": code,
                "confirmed_question_ids": list(dict.fromkeys(coverage_confirmed[code])),
                "context_only_question_ids": list(dict.fromkeys(coverage_context_only[code])),
                "status": (
                    "confirmed"
                    if coverage_confirmed[code]
                    else ("selected_unconfirmed" if coverage_context_only[code] else "uncovered")
                ),
            }
            for code in target_codes
        ],
        "uncovered": [
            code
            for code in target_codes
            if not coverage_confirmed[code] and not coverage_context_only[code]
        ],
        "selected_unconfirmed": [
            code
            for code in target_codes
            if not coverage_confirmed[code] and coverage_context_only[code]
        ],
        "candidate_question_count": len(candidate_ids),
        "selected_group_count": len(selected),
        "selected_question_count": selected_questions,
        "excluded_candidate_count": len(exclusions),
    }

    if conn.in_transaction:
        raise ValueError(
            "create_question_selection_manifest requires a clean transaction boundary"
        )
    conn.execute("BEGIN IMMEDIATE")
    existing = conn.execute(
        "SELECT * FROM question_selection_manifests WHERE idempotency_key=?",
        (idempotency_key,),
    ).fetchone()
    if existing:
        conn.commit()
        if existing["student_id"] != student_id:
            raise SelectionManifestConflict("idempotency_key already belongs to another student")
        if existing["request_sha256"] != request_sha256:
            raise SelectionManifestConflict(
                "idempotency_key already belongs to a different selection request"
            )
        return {
            "status": "duplicate",
            **question_selection_manifest_detail(
                conn,
                str(existing["selection_manifest_id"]),
                student_id=student_id,
            ),
        }

    selection_manifest_id = stable_id("SEL", idempotency_key, request_sha256)
    now = utc_now()
    with conn:
        conn.execute(
            """
            INSERT INTO question_selection_manifests(
              selection_manifest_id,idempotency_key,request_sha256,student_id,subject_code,
              generation_id,training_mode,status,source_namespace,source_snapshot_sha256,
              source_locator_json,data_as_of,algorithm_version,random_seed,target_knowledge_json,
              selection_policy_json,explanation_contract_json,candidate_question_count,
              selected_group_count,selected_question_count,exclusion_count,coverage_json,
              created_at,finalized_at
            ) VALUES (?,?,?,?,?,?,?,'building',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)
            """,
            (
                selection_manifest_id,
                idempotency_key,
                request_sha256,
                student_id,
                subject_code,
                payload.get("generation_id"),
                training_mode,
                QUESTION_BANK_NAMESPACE,
                source_snapshot_sha256,
                canonical_json(source_locator),
                data_as_of_raw,
                SELECTION_ALGORITHM_VERSION,
                payload.get("random_seed"),
                canonical_json(target_codes),
                canonical_json(selection_policy),
                canonical_json(explanation_contract),
                len(candidate_ids),
                len(selected),
                selected_questions,
                len(exclusions),
                canonical_json(coverage),
                now,
            ),
        )
        item_ordinal = 0
        for group_ordinal, group in enumerate(selected, 1):
            group_id = stable_id("SELG", selection_manifest_id, group["group_key"])
            group_codes = sorted(
                set(group["context"]["knowledge_codes"])
                | {
                    code
                    for identity in group["questions"]
                    for code in identity["knowledge_codes"]
                }
            )
            conn.execute(
                """
                INSERT INTO question_selection_groups(
                  selection_group_id,selection_manifest_id,group_kind,passage_id,source_id,
                  source_locator_json,passage_content_sha256,group_content_sha256,
                  expected_question_count,selected_question_count,complete_group,
                  reason_codes_json,knowledge_codes_json,evidence_references_json,
                  duplicate_check_json,ordinal
                ) VALUES (?,?,?,?,?,?,?,?,?,?,1,?,?,?,?,?)
                """,
                (
                    group_id,
                    selection_manifest_id,
                    group["group_kind"],
                    group["passage_id"],
                    group["source_id"],
                    canonical_json(group["source_locator"]),
                    group["passage_content_sha256"],
                    group["group_content_sha256"],
                    group["expected_question_count"],
                    group["expected_question_count"],
                    canonical_json(group["context"]["reason_codes"]),
                    canonical_json(group_codes),
                    canonical_json(group["context"]["evidence_references"]),
                    canonical_json(group["duplicate_check"]),
                    group_ordinal,
                ),
            )
            for within_group, identity in enumerate(group["questions"], 1):
                item_ordinal += 1
                mapping_evidence = {
                    "source_labels": identity["source_labels"],
                    "confirmed_codes": identity["knowledge"]["confirmed_codes"],
                    "mappings": identity["knowledge"]["mappings"],
                    "selection_target_codes": group["context"]["knowledge_codes"],
                    "selection_context_evidence": group["context"]["evidence_references"],
                }
                conn.execute(
                    """
                    INSERT INTO question_selection_items(
                      selection_manifest_id,selection_group_id,question_id,source_id,passage_id,
                      question_type,original_number,source_locator_json,question_content_sha256,
                      standard_answer_sha256,verification_status,is_real_question,
                      reason_codes_json,knowledge_codes_json,mapping_evidence_json,
                      duplicate_check_json,expected_public_explanation_cache_key,
                      public_explanation_status,group_ordinal,ordinal
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,1,?,?,?,?,?,?,?,?)
                    """,
                    (
                        selection_manifest_id,
                        group_id,
                        identity["question_id"],
                        identity["source_id"],
                        identity["passage_id"],
                        identity["question_type"],
                        identity["original_number"],
                        canonical_json(identity["source_locator"]),
                        identity["question_content_sha256"],
                        identity["standard_answer_sha256"],
                        identity["verification_status"],
                        canonical_json(group["context"]["reason_codes"]),
                        canonical_json(identity["knowledge_codes"]),
                        canonical_json(mapping_evidence),
                        canonical_json(group["duplicate_check"]),
                        identity["explanation_cache"]["cache_key"],
                        identity["public_explanation_status"],
                        within_group,
                        item_ordinal,
                    ),
                )
        for exclusion in exclusions:
            exclusion_id = stable_id(
                "SELEX",
                selection_manifest_id,
                exclusion["candidate_key"],
                exclusion["reason_code"],
            )
            conn.execute(
                """
                INSERT INTO question_selection_exclusions(
                  exclusion_id,selection_manifest_id,candidate_key,candidate_question_id,
                  candidate_passage_id,source_id,source_locator_json,reason_code,detail_json,
                  question_content_sha256,matched_question_id,similarity_score,created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    exclusion_id,
                    selection_manifest_id,
                    exclusion["candidate_key"],
                    exclusion["candidate_question_id"],
                    exclusion["candidate_passage_id"],
                    exclusion["source_id"],
                    canonical_json(exclusion["source_locator"]),
                    exclusion["reason_code"],
                    canonical_json(exclusion["detail"]),
                    exclusion["question_content_sha256"],
                    exclusion["matched_question_id"],
                    exclusion["similarity_score"],
                    now,
                ),
            )
        conn.execute(
            """
            UPDATE question_selection_manifests
            SET status='finalized', finalized_at=?
            WHERE selection_manifest_id=?
            """,
            (now, selection_manifest_id),
        )
    return {
        "status": "created",
        **question_selection_manifest_detail(
            conn,
            selection_manifest_id,
            student_id=student_id,
        ),
    }


def _public_explanation_forbidden_paths(
    value: Any,
    path: str = "explanation",
    *,
    student_display_names: frozenset[str] = frozenset(),
) -> list[str]:
    forbidden: list[str] = []
    if isinstance(value, dict):
        for key, nested in value.items():
            raw_key = str(key).casefold()
            normalized = re.sub(r"[^a-z0-9]+", "_", raw_key).strip("_")
            next_path = f"{path}.{key}"
            if (
                normalized in _FORBIDDEN_PUBLIC_EXPLANATION_KEYS
                or any(fragment in raw_key or fragment in normalized for fragment in _FORBIDDEN_PUBLIC_KEY_FRAGMENTS)
            ):
                forbidden.append(next_path)
            forbidden.extend(
                _public_explanation_forbidden_paths(
                    nested,
                    next_path,
                    student_display_names=student_display_names,
                )
            )
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            forbidden.extend(
                _public_explanation_forbidden_paths(
                    nested,
                    f"{path}[{index}]",
                    student_display_names=student_display_names,
                )
            )
    elif isinstance(value, str):
        normalized_value = _normalize_whitespace(value).casefold()
        if any(pattern.search(value) for pattern in _FORBIDDEN_PUBLIC_VALUE_PATTERNS) or (
            _contains_student_display_name(normalized_value, student_display_names)
        ):
            forbidden.append(path)
    return forbidden


def _nontrivial_student_display_names(conn: sqlite3.Connection) -> frozenset[str]:
    """Return every local learner name worth treating as identifying text.

    Active status is intentionally ignored: an inactive learner's identity is still
    private. One-character labels are skipped because they are commonly ordinary
    words, initials, or option labels rather than useful identity detectors.
    """

    names: set[str] = set()
    for row in conn.execute("SELECT display_name FROM students"):
        normalized = _normalize_whitespace(row[0]).casefold()
        significant = "".join(character for character in normalized if character.isalnum())
        if len(significant) >= 2:
            names.add(normalized)
    return frozenset(names)


def _verified_question_for_cache(
    qconn: sqlite3.Connection,
    question_id: str,
) -> tuple[dict[str, Any], sqlite3.Row, sqlite3.Row | None]:
    question = _load_question(qconn, question_id)
    if not question:
        raise ValueError(f"Unknown question_id: {question_id}")
    complete_group, group_reason, group_detail = _build_group(qconn, question)
    if not complete_group:
        raise ValueError(
            "Public explanation requires a verified real complete question group: "
            + canonical_json(
                {"reason_code": group_reason, "detail": group_detail}
            )
        )
    passage = _load_passage(qconn, _row_value(question, "passage_id"))
    source = _load_source(qconn, _source_id(question, passage))
    options = _load_options(qconn, question_id)
    issues = _question_issues(question, passage, source, options)
    if issues:
        raise ValueError(
            f"Public explanation requires a verified real complete question: {canonical_json(issues)}"
        )
    if passage and (
        _normalize_whitespace(_row_value(passage, "verification_status"))
        not in CONFIRMED_QUESTION_STATUSES
        or not _normalize_whitespace(_row_value(passage, "passage_text"))
    ):
        raise ValueError("Public explanation requires a verified complete source passage")
    identity = next(
        item for item in complete_group["questions"] if item["question_id"] == question_id
    )
    return identity, question, source


def _explanation_row(row: sqlite3.Row) -> dict[str, Any]:
    value = dict(row)
    value["source_locator"] = _json_load(value.pop("source_locator_json"))
    value["explanation"] = _json_load(value.pop("explanation_json"))
    return value


def cache_public_explanation(
    conn: sqlite3.Connection,
    question_bank: str | Path,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Cache caller-supplied public explanation content; no model is called here.

    Only ``source_checked`` and ``teacher_confirmed`` explanations are reusable and
    accepted by this API. Draft/pending content deliberately needs a separate review
    workflow and is never inserted into the reusable cache by this function.
    """

    if not isinstance(payload, dict):
        raise ValueError("public explanation payload must be an object")
    unknown_request_fields = sorted(set(payload) - PUBLIC_EXPLANATION_REQUEST_FIELDS)
    if unknown_request_fields:
        raise ValueError(
            "Unknown public explanation request field(s): "
            + ", ".join(unknown_request_fields)
        )
    question_id = _normalize_whitespace(payload.get("question_id"))
    explanation = payload.get("explanation")
    explanation_status = _normalize_whitespace(payload.get("explanation_status"))
    created_by = _normalize_whitespace(payload.get("created_by"))
    confirmed_by = _normalize_whitespace(payload.get("confirmed_by"))
    if not question_id or not isinstance(explanation, dict) or not explanation:
        raise ValueError("question_id and a non-empty explanation object are required")
    if explanation_status not in REUSABLE_EXPLANATION_STATUSES:
        raise ValueError("explanation_status must be source_checked or teacher_confirmed")
    if not created_by or not confirmed_by:
        raise ValueError("created_by and confirmed_by are required for reusable explanations")
    student_display_names = _nontrivial_student_display_names(conn)
    forbidden = _public_explanation_forbidden_paths(
        explanation,
        student_display_names=student_display_names,
    )
    forbidden.extend(
        _public_explanation_forbidden_paths(
            {"created_by": created_by, "confirmed_by": confirmed_by},
            "metadata",
            student_display_names=student_display_names,
        )
    )
    if forbidden:
        raise ValueError(
            "Public explanation cannot contain student-specific diagnosis fields: "
            + ", ".join(forbidden)
        )
    contract = _normalize_explanation_contract(payload.get("explanation_contract"))
    source_path, qconn, source_snapshot_sha256 = _readonly_question_bank(question_bank)
    try:
        identity, _, _ = _verified_question_for_cache(qconn, question_id)
    finally:
        qconn.close()
    if _sha256_file(source_path) != source_snapshot_sha256:
        raise RuntimeError("Read-only question-bank source changed while caching explanation")
    knowledge = _knowledge_bundle(conn, identity)
    cache_identity = _cache_identity(
        identity,
        knowledge,
        source_snapshot_sha256,
        contract,
    )
    explanation_sha256 = payload_hash(explanation)
    if conn.in_transaction:
        raise ValueError("cache_public_explanation requires a clean transaction boundary")
    conn.execute("BEGIN IMMEDIATE")
    existing = conn.execute(
        "SELECT * FROM public_question_explanations WHERE cache_key=?",
        (cache_identity["cache_key"],),
    ).fetchone()
    if existing:
        conn.commit()
        if existing["explanation_sha256"] != explanation_sha256:
            raise SelectionManifestConflict(
                "cache_key already exists with different public explanation content"
            )
        if existing["explanation_status"] not in REUSABLE_EXPLANATION_STATUSES:
            raise SelectionManifestConflict(
                "matching cache entry is stale; bump the rubric or explanation policy version"
            )
        return {
            "status": "duplicate",
            "cache_identity": cache_identity,
            "explanation": _explanation_row(existing),
            "invalidated_count": 0,
        }

    now = utc_now()
    explanation_id = stable_id("PUBEXP", cache_identity["cache_key"])
    with conn:
        superseded = conn.execute(
            """
            SELECT public_explanation_id FROM public_question_explanations
            WHERE question_id=?
              AND explanation_status IN ('ai_draft','pending_review','source_checked','teacher_confirmed')
            ORDER BY updated_at DESC LIMIT 1
            """,
            (question_id,),
        ).fetchone()
        changed = conn.execute(
            """
            UPDATE public_question_explanations
            SET explanation_status='stale', invalidated_at=?,
                invalidation_reason='deterministic_cache_identity_changed', updated_at=?
            WHERE question_id=? AND cache_key<>?
              AND explanation_status IN ('ai_draft','pending_review','source_checked','teacher_confirmed')
            """,
            (now, now, question_id, cache_identity["cache_key"]),
        ).rowcount
        conn.execute(
            """
            INSERT INTO public_question_explanations(
              public_explanation_id,cache_key,cache_key_version,question_id,source_namespace,
              source_id,passage_id,source_locator_json,source_snapshot_sha256,
              question_content_sha256,standard_answer_sha256,knowledge_mapping_sha256,
              rubric_version,rubric_sha256,explanation_policy_version,
              explanation_schema_version,explanation_status,explanation_json,
              explanation_sha256,created_by,confirmed_by,supersedes_public_explanation_id,
              created_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                explanation_id,
                cache_identity["cache_key"],
                cache_identity["cache_key_version"],
                question_id,
                QUESTION_BANK_NAMESPACE,
                identity["source_id"],
                identity["passage_id"],
                canonical_json(identity["source_locator"]),
                cache_identity["source_snapshot_sha256"],
                cache_identity["question_content_sha256"],
                cache_identity["standard_answer_sha256"],
                cache_identity["knowledge_mapping_sha256"],
                cache_identity["rubric_version"],
                cache_identity["rubric_sha256"],
                cache_identity["explanation_policy_version"],
                cache_identity["explanation_schema_version"],
                explanation_status,
                canonical_json(explanation),
                explanation_sha256,
                created_by,
                confirmed_by,
                superseded["public_explanation_id"] if superseded else None,
                now,
                now,
            ),
        )
    row = conn.execute(
        "SELECT * FROM public_question_explanations WHERE public_explanation_id=?",
        (explanation_id,),
    ).fetchone()
    return {
        "status": "created",
        "cache_identity": cache_identity,
        "explanation": _explanation_row(row),
        "invalidated_count": int(changed),
    }


def lookup_public_explanation(
    conn: sqlite3.Connection,
    question_bank: str | Path,
    question_id: str,
    *,
    explanation_contract: dict[str, Any] | None = None,
) -> dict[str, Any]:
    explicit_question_id = _normalize_whitespace(question_id)
    if not explicit_question_id:
        raise ValueError("question_id is required")
    contract = _normalize_explanation_contract(explanation_contract)
    source_path, qconn, source_snapshot_sha256 = _readonly_question_bank(question_bank)
    try:
        identity, _, _ = _verified_question_for_cache(qconn, explicit_question_id)
    finally:
        qconn.close()
    if _sha256_file(source_path) != source_snapshot_sha256:
        raise RuntimeError("Read-only question-bank source changed during explanation lookup")
    knowledge = _knowledge_bundle(conn, identity)
    cache_identity = _cache_identity(
        identity,
        knowledge,
        source_snapshot_sha256,
        contract,
    )
    row = conn.execute(
        "SELECT * FROM public_question_explanations WHERE cache_key=?",
        (cache_identity["cache_key"],),
    ).fetchone()
    if row and row["explanation_status"] in REUSABLE_EXPLANATION_STATUSES:
        return {
            "status": "hit",
            "cache_identity": cache_identity,
            "explanation": _explanation_row(row),
        }
    current = conn.execute(
        """
        SELECT cache_key,explanation_status,updated_at
        FROM public_question_explanations
        WHERE question_id=?
        ORDER BY updated_at DESC LIMIT 1
        """,
        (explicit_question_id,),
    ).fetchone()
    return {
        "status": "miss",
        "reason": (
            "matching_entry_not_reusable"
            if row
            else ("deterministic_cache_identity_changed" if current else "not_generated")
        ),
        "cache_identity": cache_identity,
        "latest_entry": dict(current) if current else None,
    }


def invalidate_public_explanations(
    conn: sqlite3.Connection,
    question_bank: str | Path,
    question_id: str,
    *,
    explanation_contract: dict[str, Any] | None = None,
    reason: str = "deterministic_cache_identity_changed",
) -> dict[str, Any]:
    """Mark active rows stale when current source/answer/mapping/version yields a new key."""

    validation_error: str | None = None
    try:
        lookup = lookup_public_explanation(
            conn,
            question_bank,
            question_id,
            explanation_contract=explanation_contract,
        )
    except ValueError as exc:
        message = str(exc)
        if not (
            message.startswith("Public explanation requires")
            or message.startswith("Unknown question_id:")
        ):
            raise
        lookup = None
        validation_error = message
    expected_key = lookup["cache_identity"]["cache_key"] if lookup else None
    explicit_reason = _normalize_whitespace(reason)
    if not explicit_reason:
        raise ValueError("reason is required")
    now = utc_now()
    with conn:
        if expected_key:
            changed = conn.execute(
                """
                UPDATE public_question_explanations
                SET explanation_status='stale',invalidated_at=?,invalidation_reason=?,updated_at=?
                WHERE question_id=? AND cache_key<>?
                  AND explanation_status IN ('ai_draft','pending_review','source_checked','teacher_confirmed')
                """,
                (now, explicit_reason, now, _normalize_whitespace(question_id), expected_key),
            ).rowcount
        else:
            changed = conn.execute(
                """
                UPDATE public_question_explanations
                SET explanation_status='stale',invalidated_at=?,invalidation_reason=?,updated_at=?
                WHERE question_id=?
                  AND explanation_status IN ('ai_draft','pending_review','source_checked','teacher_confirmed')
                """,
                (now, explicit_reason, now, _normalize_whitespace(question_id)),
            ).rowcount
    return {
        "status": "invalidated" if changed else "unchanged",
        "invalidated_count": int(changed),
        "expected_cache_key": expected_key,
        "lookup_status": lookup["status"] if lookup else "source_not_reusable",
        "validation_error": validation_error,
    }
