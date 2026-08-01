from __future__ import annotations

import csv
import hashlib
import json
import re
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

from .util import canonical_json, stable_id, utc_now


QB_NAMESPACE = "shanghai_question_bank"
QUESTION_TYPE = "语法填空"
SOURCE_STATUS = "source_checked"
CONFIRMED_STATUSES = {"source_checked", "verified"}
VERIFICATION_RANK = {"rejected": 0, "needs_check": 1, "unverified": 1, "suggested": 2, "source_checked": 3, "verified": 4}

REQUIRED_COVERAGE_CODES = (
    "sentence_backbone",
    "predicate_count",
    "tense",
    "passive_voice",
    "subject_verb_agreement",
    "modal_verb",
    "infinitive",
    "gerund",
    "present_participle",
    "past_participle",
    "non_finite_logical_subject",
    "non_finite_voice",
    "non_finite_sequence",
    "noun_derivation",
    "adjective_derivation",
    "adverb_derivation",
    "noun_number",
    "comparative_degree",
    "negative_prefix",
    "pronoun_form",
    "article",
    "preposition_collocation",
    "coordinating_conjunction",
    "relative_clause",
    "noun_clause",
    "adverbial_clause",
    "connector_function_clause_completeness",
    "inversion",
    "emphasis",
    "subjunctive",
    "fixed_structure",
)


LEGACY_POINT_MAP = {
    "句子结构与完整主干": "sentence_backbone",
    "动词时态": "tense",
    "被动语态": "passive_voice",
    "主谓一致": "subject_verb_agreement",
    "情态动词": "modal_verb",
    "非谓语动词": "non_finite",
    "不定式": "infinitive",
    "动名词": "gerund",
    "分词": "participle",
    "比较结构": "comparative_degree",
    "代词": "pronoun",
    "冠词": "article",
    "介词与固定搭配": "preposition_collocation",
    "并列与连接": "connector_function_clause_completeness",
    "定语从句": "relative_clause",
    "名词性从句": "noun_clause",
    "状语从句": "adverbial_clause",
    "倒装": "inversion",
    "强调句": "emphasis",
    "虚拟语气": "subjunctive",
}


def repair_mojibake(value: str | None) -> str | None:
    """Repair the reversible Latin-1/GBK field corruption present in legacy tags."""
    if value is None:
        return None
    try:
        repaired = value.encode("latin-1").decode("gbk")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return value
    if any("\u4e00" <= char <= "\u9fff" for char in repaired):
        return repaired
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _split_points(value: str | None) -> list[str]:
    normalized = repair_mojibake(value) or ""
    return [part.strip() for part in normalized.split("|") if part.strip()]


def _knowledge_ids(conn) -> dict[str, str]:
    return {row["code"]: row["knowledge_point_id"] for row in conn.execute("SELECT code, knowledge_point_id FROM knowledge_points WHERE active=1")}


def _upsert_used_item_mapping(
    conn,
    *,
    item_id: str,
    knowledge_point_id: str,
    role: str,
    mapping_source: str,
    confidence: float,
    verification_status: str,
    rationale: str,
    source_snapshot_id: str,
) -> str:
    existing = conn.execute(
        "SELECT * FROM item_knowledge_map WHERE item_id=? AND knowledge_point_id=?",
        (item_id, knowledge_point_id),
    ).fetchone()
    if not existing:
        conn.execute(
            """
            INSERT INTO item_knowledge_map(
              item_id, knowledge_point_id, mapping_role, weight,
              mapping_source, confidence, verification_status,
              rationale, source_snapshot_id
            ) VALUES (?, ?, ?, 1.0, ?, ?, ?, ?, ?)
            """,
            (item_id, knowledge_point_id, role, mapping_source, confidence, verification_status, rationale, source_snapshot_id),
        )
        return "inserted"
    if existing["mapping_source"] == "manual" or existing["verification_status"] == "verified":
        return "preserved"
    incoming_rank = VERIFICATION_RANK.get(verification_status, 0)
    existing_rank = VERIFICATION_RANK.get(existing["verification_status"], 0)
    if (incoming_rank, float(confidence)) <= (existing_rank, float(existing["confidence"])):
        return "preserved"
    conn.execute(
        """
        UPDATE item_knowledge_map
        SET mapping_role=?, mapping_source=?, confidence=?, verification_status=?,
            rationale=?, source_snapshot_id=?
        WHERE item_id=? AND knowledge_point_id=?
        """,
        (role, mapping_source, confidence, verification_status, rationale, source_snapshot_id, item_id, knowledge_point_id),
    )
    return "updated"


def _reconcile_used_items(conn, snapshot_id: str) -> tuple[int, int]:
    inserted = 0
    updated = 0
    rows = conn.execute(
        """
        SELECT er.item_id, qkm.knowledge_point_id, qkm.role,
               qkm.mapping_source, qkm.confidence, qkm.verification_status,
               qkm.rationale
        FROM question_knowledge_map qkm
        JOIN external_references er
          ON er.namespace=? AND er.reference_type='question_id'
         AND er.external_id=qkm.question_id
        WHERE qkm.source_snapshot_id=? AND qkm.verification_status<>'rejected'
        """,
        (QB_NAMESPACE, snapshot_id),
    )
    for row in rows:
        action = _upsert_used_item_mapping(
            conn,
            item_id=row["item_id"],
            knowledge_point_id=row["knowledge_point_id"],
            role=row["role"],
            mapping_source=row["mapping_source"],
            confidence=row["confidence"],
            verification_status=row["verification_status"],
            rationale=row["rationale"],
            source_snapshot_id=snapshot_id,
        )
        inserted += int(action == "inserted")
        updated += int(action == "updated")
    return inserted, updated


def _rule_mappings(row: sqlite3.Row, direct_codes: set[str]) -> list[dict[str, Any]]:
    answer = repair_mojibake(row["answer"]) or ""
    explanation = repair_mojibake(row["explanation_raw"]) or ""
    primary = repair_mojibake(row["primary_test_point"]) or ""
    secondary = repair_mojibake(row["secondary_test_points"]) or ""
    text = f"{primary}|{secondary}|{explanation}"
    mappings: dict[tuple[str, str], dict[str, Any]] = {}

    def add(code: str, role: str, confidence: float, rationale: str) -> None:
        key = (code, role)
        candidate = {
            "code": code,
            "role": role,
            "mapping_source": "rule",
            "confidence": round(confidence, 3),
            "verification_status": "suggested",
            "rationale": rationale,
        }
        if key not in mappings or confidence > mappings[key]["confidence"]:
            mappings[key] = candidate

    nonfinite_context = any(code in direct_codes for code in {"non_finite", "infinitive", "gerund", "participle", "passive_voice"}) or "非谓语" in explanation
    if nonfinite_context:
        if "不定式" in explanation or re.match(r"(?i)^to\s+", answer.strip()):
            add("infinitive", "secondary", 0.90, "Rule suggestion: the source explanation or answer form identifies an infinitive.")
        if "动名词" in explanation:
            add("gerund", "secondary", 0.92, "Rule suggestion: the source explanation explicitly identifies a gerund.")
        if "现在分词" in explanation or "having " in answer.lower():
            add("present_participle", "secondary", 0.92, "Rule suggestion: the source explanation or form identifies a present participle.")
        if "过去分词" in explanation:
            add("past_participle", "secondary", 0.92, "Rule suggestion: the source explanation explicitly identifies a past participle.")
        if "逻辑主语" in explanation:
            add("non_finite_logical_subject", "prerequisite", 0.93, "Rule suggestion: the explanation explicitly reasons from the non-finite verb's logical subject.")
        if "主动关系" in explanation or "被动关系" in explanation:
            add("non_finite_voice", "prerequisite", 0.91, "Rule suggestion: the explanation explicitly contrasts the logical subject with active/passive voice.")
        if any(token in explanation for token in ("动作先后", "先于", "完成式", "先发生", "同时进行")):
            add("non_finite_sequence", "prerequisite", 0.90, "Rule suggestion: the explanation explicitly discusses action sequence or perfect non-finite form.")

    if any(code in direct_codes for code in {"non_finite", "infinitive", "gerund", "participle", "tense", "passive_voice", "subject_verb_agreement"}) and any(
        token in text for token in ("句子结构", "谓语", "非谓语")
    ):
        add("predicate_count", "prerequisite", 0.82, "Rule suggestion: finite/non-finite selection requires locating and counting clause predicates.")

    if re.search(r"考查(?:名词|名词形式|可数名词)[。；，]", explanation) and "名词性从句" not in explanation:
        add("noun_derivation", "secondary", 0.88, "Rule suggestion: the explanation asks for a noun form.")
    if "考查形容词" in explanation or "应填形容词" in explanation or "形容词形式" in explanation:
        add("adjective_derivation", "secondary", 0.88, "Rule suggestion: the explanation asks for an adjective form.")
    if "考查副词" in explanation or "应填副词" in explanation or "副词形式" in explanation:
        add("adverb_derivation", "secondary", 0.88, "Rule suggestion: the explanation asks for an adverb form.")
    if "名词" in explanation and any(token in explanation for token in ("复数", "单复数")):
        add("noun_number", "secondary", 0.91, "Rule suggestion: the explanation explicitly discusses noun number.")
    if "比较级" in explanation or "最高级" in explanation:
        add("comparative_degree", "secondary", 0.93, "Rule suggestion: the explanation explicitly identifies comparative or superlative degree.")
    if any(token in explanation for token in ("否定前缀", "否定形式", "否定意义")):
        add("negative_prefix", "secondary", 0.86, "Rule suggestion: the explanation explicitly requires a negative derivative.")
    if "pronoun" in direct_codes or "考查代词" in explanation:
        add("pronoun_form", "secondary", 0.86, "Rule suggestion: a pronoun item requires selection of a context-appropriate pronoun form.")

    clause_codes = {"relative_clause", "noun_clause", "adverbial_clause"}
    if direct_codes & clause_codes or any(token in explanation for token in ("引导定语从句", "引导名词性从句", "引导状语从句", "从句中缺少", "从句完整")):
        add("connector_function_clause_completeness", "prerequisite", 0.87, "Rule suggestion: connector choice depends on clause function and completeness.")

    coordinate_answers = {"and", "but", "or", "nor", "so", "yet", "for"}
    answer_alternatives = {part.strip().lower() for part in answer.split("##")}
    if answer_alternatives & coordinate_answers and any(token in explanation for token in ("并列", "连词", "连接两个")):
        add("coordinating_conjunction", "secondary", 0.90, "Rule suggestion: the answer is a coordinating conjunction and the explanation identifies coordination.")

    if any(token in explanation for token in ("固定句型", "句型:", "固定结构", "固定用法")):
        add("fixed_structure", "secondary", 0.78, "Rule suggestion: the source explanation identifies a fixed grammatical structure.")

    return list(mappings.values())


def _build_mappings(row: sqlite3.Row) -> list[dict[str, Any]]:
    mappings: dict[tuple[str, str], dict[str, Any]] = {}
    direct_codes: set[str] = set()

    def add(mapping: dict[str, Any]) -> None:
        key = (mapping["code"], mapping["role"])
        existing = mappings.get(key)
        if existing is None or (
            VERIFICATION_RANK.get(mapping["verification_status"], 0),
            mapping["confidence"],
        ) > (
            VERIFICATION_RANK.get(existing["verification_status"], 0),
            existing["confidence"],
        ):
            mappings[key] = mapping

    labels = [(repair_mojibake(row["primary_test_point"]), "primary")]
    labels.extend((label, "secondary") for label in _split_points(row["secondary_test_points"]))
    for label, role in labels:
        code = LEGACY_POINT_MAP.get(label or "")
        if not code:
            continue
        direct_codes.add(code)
        add(
            {
                "code": code,
                "role": role,
                "mapping_source": "legacy",
                "confidence": 0.95 if role == "primary" else 0.90,
                "verification_status": "source_checked",
                "rationale": f"Source-checked legacy {role} label: {label}",
            }
        )

    parent_rules = {
        "infinitive": "non_finite",
        "gerund": "non_finite",
        "participle": "non_finite",
        "passive_voice": "voice",
    }
    for child, parent in parent_rules.items():
        if child in direct_codes:
            add(
                {
                    "code": parent,
                    "role": "prerequisite",
                    "mapping_source": "legacy",
                    "confidence": 0.95,
                    "verification_status": "source_checked",
                    "rationale": f"Taxonomy parent implied by source-checked label mapped to {child}.",
                }
            )

    for mapping in _rule_mappings(row, direct_codes):
        add(mapping)
    return list(mappings.values())


def current_snapshot_id(conn, namespace: str = QB_NAMESPACE) -> str:
    row = conn.execute(
        "SELECT source_snapshot_id FROM source_snapshots WHERE namespace=? AND is_current=1",
        (namespace,),
    ).fetchone()
    if not row:
        raise ValueError("No current grammar catalog snapshot. Run `knowledge sync` first.")
    return row["source_snapshot_id"]


def sync_grammar_catalog(conn, question_bank: str | Path) -> dict[str, Any]:
    source_path = Path(question_bank).expanduser().resolve()
    if not source_path.is_file():
        raise ValueError(f"Question bank not found: {source_path}")
    digest = _sha256(source_path)
    filter_definition = canonical_json({"question_type": QUESTION_TYPE, "verification_status": SOURCE_STATUS})
    existing = conn.execute(
        "SELECT * FROM source_snapshots WHERE namespace=? AND source_sha256=? AND filter_definition=?",
        (QB_NAMESPACE, digest, filter_definition),
    ).fetchone()
    if existing:
        with conn:
            conn.execute("UPDATE source_snapshots SET is_current=0 WHERE namespace=?", (QB_NAMESPACE,))
            conn.execute("UPDATE source_snapshots SET is_current=1 WHERE source_snapshot_id=?", (existing["source_snapshot_id"],))
            linked, updated = _reconcile_used_items(conn, existing["source_snapshot_id"])
        return {
            "status": "duplicate",
            "source_snapshot_id": existing["source_snapshot_id"],
            "question_count": existing["question_count"],
            "passage_count": existing["passage_count"],
            "used_item_mappings_linked": linked,
            "used_item_mappings_updated": updated,
            "source_unchanged": True,
        }

    qconn = sqlite3.connect(f"file:{source_path.as_posix()}?mode=ro", uri=True, timeout=10)
    qconn.row_factory = sqlite3.Row
    qconn.execute("PRAGMA query_only=ON")
    try:
        questions = list(
            qconn.execute(
                """
                SELECT q.*, p.title AS passage_title,
                       p.verification_status AS passage_verification_status
                FROM questions q
                JOIN passages p ON p.passage_id=q.passage_id
                WHERE q.question_type=? AND q.verification_status=?
                ORDER BY q.passage_id, q.source_ordinal, q.original_number, q.question_id
                """,
                (QUESTION_TYPE, SOURCE_STATUS),
            )
        )
    finally:
        qconn.close()
    if not questions:
        raise ValueError("The source contains no source_checked grammar-fill questions.")

    grouped: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in questions:
        grouped[row["passage_id"]].append(row)
    snapshot_id = stable_id("SNAP", QB_NAMESPACE, digest, filter_definition)
    now = utc_now()
    knowledge_ids = _knowledge_ids(conn)
    unknown_codes: set[str] = set()
    mapping_count = 0
    rule_suggestions = 0
    legacy_mappings = 0
    linked_item_mappings = 0
    updated_item_mappings = 0
    try:
        with conn:
            conn.execute("UPDATE source_snapshots SET is_current=0 WHERE namespace=?", (QB_NAMESPACE,))
            conn.execute(
                """
                INSERT INTO source_snapshots(
                  source_snapshot_id, namespace, source_uri, source_sha256,
                  source_size_bytes, source_modified_at, filter_definition,
                  question_count, passage_count, is_current, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                """,
                (
                    snapshot_id,
                    QB_NAMESPACE,
                    str(source_path),
                    digest,
                    source_path.stat().st_size,
                    str(source_path.stat().st_mtime_ns),
                    filter_definition,
                    len(questions),
                    len(grouped),
                    now,
                ),
            )
            for passage_id, rows in grouped.items():
                complete = all(
                    row["answer"] is not None
                    and str(row["answer"]).strip()
                    and row["primary_test_point"] is not None
                    and str(row["primary_test_point"]).strip()
                    and row["passage_verification_status"] == SOURCE_STATUS
                    for row in rows
                )
                first = rows[0]
                conn.execute(
                    """
                    INSERT INTO grammar_passage_catalog(
                      source_snapshot_id, passage_id, source_id, title,
                      question_count, source_checked_question_count,
                      complete_passage, verification_status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        snapshot_id,
                        passage_id,
                        first["source_id"],
                        repair_mojibake(first["passage_title"]),
                        len(rows),
                        len(rows),
                        int(bool(complete)),
                        SOURCE_STATUS if complete else "needs_check",
                    ),
                )
            for row in questions:
                primary_normalized = repair_mojibake(row["primary_test_point"])
                secondary_normalized = repair_mojibake(row["secondary_test_points"])
                conn.execute(
                    """
                    INSERT INTO grammar_question_catalog(
                      source_snapshot_id, question_id, passage_id, source_id,
                      original_number, year, exam_type, district_or_school,
                      difficulty_label, answer_available, explanation_available,
                      primary_test_point_raw, primary_test_point_normalized,
                      secondary_test_points_raw, secondary_test_points_normalized,
                      verification_status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        snapshot_id,
                        row["question_id"],
                        row["passage_id"],
                        row["source_id"],
                        row["original_number"],
                        row["year"],
                        repair_mojibake(row["exam_type"]),
                        repair_mojibake(row["district_or_school"]),
                        repair_mojibake(row["difficulty"]),
                        int(bool(row["answer"] and str(row["answer"]).strip())),
                        int(bool(row["explanation_raw"] and str(row["explanation_raw"]).strip())),
                        row["primary_test_point"],
                        primary_normalized,
                        row["secondary_test_points"],
                        secondary_normalized,
                        SOURCE_STATUS,
                    ),
                )
                for mapping in _build_mappings(row):
                    kp_id = knowledge_ids.get(mapping["code"])
                    if not kp_id:
                        unknown_codes.add(mapping["code"])
                        continue
                    conn.execute(
                        """
                        INSERT INTO question_knowledge_map(
                          source_snapshot_id, question_id, knowledge_point_id,
                          role, mapping_source, confidence, verification_status,
                          rationale, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            snapshot_id,
                            row["question_id"],
                            kp_id,
                            mapping["role"],
                            mapping["mapping_source"],
                            mapping["confidence"],
                            mapping["verification_status"],
                            mapping["rationale"],
                            now,
                            now,
                        ),
                    )
                    mapping_count += 1
                    rule_suggestions += int(mapping["mapping_source"] == "rule")
                    legacy_mappings += int(mapping["mapping_source"] == "legacy")
                    used_items = conn.execute(
                        """
                        SELECT item_id FROM external_references
                        WHERE namespace=? AND reference_type='question_id' AND external_id=?
                        """,
                        (QB_NAMESPACE, row["question_id"]),
                    ).fetchall()
                    for item in used_items:
                        action = _upsert_used_item_mapping(
                            conn,
                            item_id=item["item_id"],
                            knowledge_point_id=kp_id,
                            role=mapping["role"],
                            mapping_source=mapping["mapping_source"],
                            confidence=mapping["confidence"],
                            verification_status=mapping["verification_status"],
                            rationale=mapping["rationale"],
                            source_snapshot_id=snapshot_id,
                        )
                        linked_item_mappings += int(action == "inserted")
                        updated_item_mappings += int(action == "updated")
            after_digest = _sha256(source_path)
            if after_digest != digest:
                raise RuntimeError("Read-only question-bank source changed during synchronization.")
    except Exception:
        raise
    return {
        "status": "applied",
        "source_snapshot_id": snapshot_id,
        "question_count": len(questions),
        "passage_count": len(grouped),
        "complete_passage_count": sum(
            1
            for rows in grouped.values()
            if all(row["answer"] and row["primary_test_point"] and row["passage_verification_status"] == SOURCE_STATUS for row in rows)
        ),
        "answer_count": sum(bool(row["answer"] and str(row["answer"]).strip()) for row in questions),
        "primary_point_count": sum(bool(row["primary_test_point"] and str(row["primary_test_point"]).strip()) for row in questions),
        "explanation_count": sum(bool(row["explanation_raw"] and str(row["explanation_raw"]).strip()) for row in questions),
        "secondary_point_count": sum(bool(row["secondary_test_points"] and str(row["secondary_test_points"]).strip()) for row in questions),
        "mapping_count": mapping_count,
        "legacy_mapping_count": legacy_mappings,
        "rule_suggestion_count": rule_suggestions,
        "used_item_mappings_linked": linked_item_mappings,
        "used_item_mappings_updated": updated_item_mappings,
        "unknown_knowledge_codes": sorted(unknown_codes),
        "source_unchanged": after_digest == digest,
    }


def question_knowledge(conn, question_id: str, *, snapshot_id: str | None = None) -> dict[str, Any]:
    snapshot_id = snapshot_id or current_snapshot_id(conn)
    question = conn.execute(
        """
        SELECT q.*, p.title AS passage_title
        FROM grammar_question_catalog q
        JOIN grammar_passage_catalog p
          ON p.source_snapshot_id=q.source_snapshot_id AND p.passage_id=q.passage_id
        WHERE q.source_snapshot_id=? AND q.question_id=?
        """,
        (snapshot_id, question_id),
    ).fetchone()
    if not question:
        raise ValueError(f"Question is not present in snapshot {snapshot_id}: {question_id}")
    mappings = [
        dict(row)
        for row in conn.execute(
            """
            SELECT kp.code, kp.name_cn, kp.name_en, qkm.role,
                   qkm.mapping_source, qkm.confidence,
                   qkm.verification_status, qkm.rationale
            FROM question_knowledge_map qkm
            JOIN knowledge_points kp ON kp.knowledge_point_id=qkm.knowledge_point_id
            WHERE qkm.source_snapshot_id=? AND qkm.question_id=?
              AND qkm.verification_status<>'rejected'
            ORDER BY
              CASE qkm.role WHEN 'primary' THEN 1 WHEN 'secondary' THEN 2 WHEN 'prerequisite' THEN 3 ELSE 4 END,
              qkm.confidence DESC, kp.code
            """,
            (snapshot_id, question_id),
        )
    ]
    return {
        "source_snapshot_id": snapshot_id,
        "question": dict(question),
        "mappings": mappings,
        "confirmed_mapping_count": sum(row["verification_status"] in CONFIRMED_STATUSES for row in mappings),
        "suggested_mapping_count": sum(row["verification_status"] == "suggested" for row in mappings),
        "warning": "Suggested mappings require manual review; model_suggested mappings can never be auto-verified.",
    }


def passage_coverage(conn, passage_id: str, *, snapshot_id: str | None = None) -> dict[str, Any]:
    snapshot_id = snapshot_id or current_snapshot_id(conn)
    passage = conn.execute(
        "SELECT * FROM grammar_passage_catalog WHERE source_snapshot_id=? AND passage_id=?",
        (snapshot_id, passage_id),
    ).fetchone()
    if not passage:
        raise ValueError(f"Passage is not present in snapshot {snapshot_id}: {passage_id}")
    rows = list(
        conn.execute(
            """
            SELECT q.question_id, q.original_number, kp.code, kp.name_cn,
                   qkm.role, qkm.mapping_source, qkm.confidence, qkm.verification_status
            FROM grammar_question_catalog q
            LEFT JOIN question_knowledge_map qkm
              ON qkm.source_snapshot_id=q.source_snapshot_id AND qkm.question_id=q.question_id
              AND qkm.verification_status<>'rejected'
            LEFT JOIN knowledge_points kp ON kp.knowledge_point_id=qkm.knowledge_point_id
            WHERE q.source_snapshot_id=? AND q.passage_id=?
            ORDER BY q.original_number, q.question_id, kp.code
            """,
            (snapshot_id, passage_id),
        )
    )
    coverage: dict[str, dict[str, Any]] = {}
    questions: dict[str, dict[str, Any]] = {}
    for row in rows:
        question = questions.setdefault(row["question_id"], {"question_id": row["question_id"], "original_number": row["original_number"], "knowledge_points": []})
        if not row["code"]:
            continue
        question["knowledge_points"].append(
            {
                "code": row["code"],
                "role": row["role"],
                "verification_status": row["verification_status"],
                "confidence": row["confidence"],
            }
        )
        point = coverage.setdefault(
            row["code"],
            {"code": row["code"], "name_cn": row["name_cn"], "confirmed_questions": set(), "suggested_questions": set()},
        )
        bucket = "confirmed_questions" if row["verification_status"] in CONFIRMED_STATUSES else "suggested_questions"
        point[bucket].add(row["question_id"])
    coverage_rows = []
    for point in coverage.values():
        confirmed = sorted(point.pop("confirmed_questions"))
        suggested = sorted(point.pop("suggested_questions") - set(confirmed))
        coverage_rows.append({**point, "confirmed_count": len(confirmed), "suggested_count": len(suggested), "confirmed_question_ids": confirmed, "suggested_question_ids": suggested})
    coverage_rows.sort(key=lambda row: (-row["confirmed_count"], -row["suggested_count"], row["code"]))
    return {
        "source_snapshot_id": snapshot_id,
        "passage": dict(passage),
        "coverage": coverage_rows,
        "questions": list(questions.values()),
    }


def coverage_matrix(
    conn,
    passage_ids: Iterable[str],
    *,
    snapshot_id: str | None = None,
    required_codes: Iterable[str] = REQUIRED_COVERAGE_CODES,
    minimum_confirmed_questions: int = 2,
) -> dict[str, Any]:
    snapshot_id = snapshot_id or current_snapshot_id(conn)
    ids = list(dict.fromkeys(passage_ids))
    if not ids:
        raise ValueError("At least one passage_id is required.")
    passages = [passage_coverage(conn, passage_id, snapshot_id=snapshot_id) for passage_id in ids]
    codes = list(dict.fromkeys(required_codes))
    cells: dict[str, dict[str, dict[str, int]]] = {}
    for result in passages:
        by_code = {row["code"]: row for row in result["coverage"]}
        cells[result["passage"]["passage_id"]] = {
            code: {
                "confirmed": by_code.get(code, {}).get("confirmed_count", 0),
                "suggested": by_code.get(code, {}).get("suggested_count", 0),
            }
            for code in codes
        }
    summary = []
    for code in codes:
        confirmed = sum(cells[passage_id][code]["confirmed"] for passage_id in ids)
        suggested = sum(cells[passage_id][code]["suggested"] for passage_id in ids)
        if confirmed >= minimum_confirmed_questions:
            status = "covered"
        elif confirmed == 1:
            status = "insufficient"
        elif suggested:
            status = "suggested_only"
        else:
            status = "uncovered"
        summary.append({"code": code, "confirmed_count": confirmed, "suggested_count": suggested, "status": status})
    return {
        "source_snapshot_id": snapshot_id,
        "passage_ids": ids,
        "minimum_confirmed_questions": minimum_confirmed_questions,
        "matrix": cells,
        "knowledge_summary": summary,
        "uncovered": [row["code"] for row in summary if row["status"] == "uncovered"],
        "insufficient": [row["code"] for row in summary if row["status"] in {"insufficient", "suggested_only"}],
        "methodology": {
            "confirmed": "Only source_checked or manually verified mappings count as confirmed coverage.",
            "suggested": "Rule/model suggestions are displayed separately and never promoted automatically.",
            "threshold": f"At least {minimum_confirmed_questions} confirmed question mappings across the selected passages.",
        },
    }


def write_coverage_csv(result: dict[str, Any], path: str | Path) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    passages = result["passage_ids"]
    with target.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["knowledge_point", *[f"{passage_id}:confirmed" for passage_id in passages], *[f"{passage_id}:suggested" for passage_id in passages], "confirmed_total", "suggested_total", "status"])
        for row in result["knowledge_summary"]:
            code = row["code"]
            writer.writerow(
                [
                    code,
                    *[result["matrix"][passage_id][code]["confirmed"] for passage_id in passages],
                    *[result["matrix"][passage_id][code]["suggested"] for passage_id in passages],
                    row["confirmed_count"],
                    row["suggested_count"],
                    row["status"],
                ]
            )
