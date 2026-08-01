from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any

from .ingest import _begin_event, _finish_event, _record_event_row, _resolve_error_type
from .util import canonical_json, payload_hash, stable_id, utc_now


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _readonly(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def _norm(value: str | None) -> str:
    value = (value or "").strip().lower()
    value = re.sub(r"^中文到英文[:：]\s*", "", value)
    return re.sub(r"\s+", " ", value).strip(" .;:,，。")


def _answer_head(value: str | None) -> str:
    return _norm(re.split(r"[;；]", value or "", maxsplit=1)[0])


def _legacy_item_knowledge(row: dict) -> list[str]:
    domain = row["domain"]
    combined = " ".join(str(row.get(key) or "") for key in ("unit", "item_type", "hint", "tags")).lower()
    if domain == "vocabulary":
        result = ["vocabulary", "active_recall"]
        if row.get("item_type") in {"phrase", "短语", "句型"} or "phrase" in combined or "固定搭配" in combined:
            result.append("fixed_phrase")
        return result
    if domain == "grammar":
        result = ["grammar"]
        rules = [
            (("nonfinite", "非谓语"), "non_finite"),
            (("passive", "被动"), "voice"),
            (("tense", "时态"), "tense"),
            (("clause", "从句", "how to"), "noun_clause"),
        ]
        for needles, code in rules:
            if any(needle in combined for needle in needles):
                result.append(code)
        return result
    if domain == "sentence":
        return ["translation", "translation_sentence_structure", "inversion"] if "inversion" in combined or "倒装" in combined else ["translation"]
    if domain == "knowledge":
        return ["vocabulary", "fixed_phrase"]
    return []


def _insert_kp_links(conn, item_id: str, codes: list[str], evidence: str) -> None:
    for index, code in enumerate(dict.fromkeys(codes)):
        row = conn.execute("SELECT knowledge_point_id FROM knowledge_points WHERE code=?", (code,)).fetchone()
        if not row:
            continue
        conn.execute(
            """
            INSERT OR IGNORE INTO item_knowledge_map(
              item_id, knowledge_point_id, mapping_role, weight,
              evidence_source, validation_status
            ) VALUES (?, ?, ?, 1.0, ?, 'source_checked')
            """,
            (item_id, row["knowledge_point_id"], "primary" if index == 0 else "secondary", evidence),
        )


def _insert_legacy_record(
    conn,
    event_id: str,
    source_system: str,
    record_type: str,
    legacy_key: str,
    payload: Any,
    status: str,
    target_type: str | None = None,
    target_id: str | None = None,
    note: str | None = None,
) -> None:
    record_id = stable_id("LREC", source_system, record_type, legacy_key)
    conn.execute(
        """
        INSERT INTO legacy_records(
          legacy_record_id, source_system, record_type, legacy_key,
          target_entity_type, target_entity_id, raw_payload_json,
          migration_status, note, created_by_event_id, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            record_id,
            source_system,
            record_type,
            legacy_key,
            target_type,
            target_id,
            canonical_json(payload),
            status,
            note,
            event_id,
            utc_now(),
        ),
    )


def migrate_legacy(
    conn: sqlite3.Connection,
    *,
    legacy_db: str | Path,
    mastery_json: str | Path,
    victor_db: str | Path,
    student_id: str,
    backup_path: str | None = None,
) -> dict[str, Any]:
    legacy_path = Path(legacy_db).resolve()
    mastery_path = Path(mastery_json).resolve()
    victor_path = Path(victor_db).resolve()
    source_hashes = {
        "legacy_review_db": _sha256(legacy_path),
        "mastery_json": _sha256(mastery_path),
        "victor_vocab_db": _sha256(victor_path),
    }
    event_token = payload_hash({"student_id": student_id, "source_hashes": source_hashes})[:20].upper()
    event_id = f"MIG-LEGACY-{event_token}"
    payload = {
        "event_id": event_id,
        "idempotency_key": f"legacy-migration:{student_id}:{event_token}",
        "source_thread": "migration",
        "student_id": student_id,
        "source_paths": {
            "legacy_review_db": str(legacy_path),
            "mastery_json": str(mastery_path),
            "victor_vocab_db": str(victor_path),
        },
        "source_hashes": source_hashes,
    }
    legacy = _readonly(legacy_path)
    victor = _readonly(victor_path)
    try:
        legacy_integrity = legacy.execute("PRAGMA integrity_check").fetchone()[0]
        victor_integrity = victor.execute("PRAGMA integrity_check").fetchone()[0]
        legacy_counts = {
            table: legacy.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            for table in (
                "students",
                "content_items",
                "attempts",
                "error_knowledge",
                "review_state",
                "study_progress",
                "correction_tasks",
                "test_runs",
                "test_items",
            )
        }
        victor_count = victor.execute("SELECT COUNT(*) FROM vocab_entries").fetchone()[0]
        mastery_doc = json.loads(mastery_path.read_text(encoding="utf-8-sig"))
        mastery_items = mastery_doc.get("items", [])
        with conn:
            created, existing = _begin_event(conn, payload, "legacy_migration", backup_path)
            if not created:
                current_counts = {
                    table: conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
                    for table in (
                        "content_items",
                        "attempts",
                        "evaluations",
                        "review_state",
                        "review_tasks",
                        "external_references",
                        "legacy_records",
                    )
                }
                mastery_history_linked = conn.execute(
                    """SELECT COUNT(*) FROM legacy_records
                       WHERE source_system='mastery_json' AND record_type='history'
                         AND migration_status='linked'"""
                ).fetchone()[0]
                mastery_history_retained = conn.execute(
                    """SELECT COUNT(*) FROM legacy_records
                       WHERE source_system='mastery_json' AND record_type='history'
                         AND migration_status='retained_only'"""
                ).fetchone()[0]
                mastery_new_items = conn.execute(
                    "SELECT COUNT(*) FROM content_items WHERE legacy_ref LIKE 'mastery_json.items:%'"
                ).fetchone()[0]
                victor_links = conn.execute(
                    """SELECT COUNT(*) FROM external_references
                       WHERE namespace='victor_vocab' AND reference_type='vocab_entry'"""
                ).fetchone()[0]
                # Recalculate how many deterministic Victor matches shared an
                # already-linked entry; these are preserved as separate legacy
                # items but intentionally do not receive a duplicate canonical ref.
                victor_by_page_head: dict[tuple[int | None, str], list[str]] = defaultdict(list)
                for row in victor.execute("SELECT book_page,headword,entry_id FROM vocab_entries"):
                    victor_by_page_head[(row["book_page"], _norm(row["headword"]))].append(row["entry_id"])
                matched_entries = []
                for row in legacy.execute("SELECT domain,source_page,answer FROM content_items"):
                    if row["domain"] != "vocabulary":
                        continue
                    candidates = victor_by_page_head.get((row["source_page"], _answer_head(row["answer"])), [])
                    if len(candidates) == 1:
                        matched_entries.append(candidates[0])
                source_hashes_after = {
                    "legacy_review_db": _sha256(legacy_path),
                    "mastery_json": _sha256(mastery_path),
                    "victor_vocab_db": _sha256(victor_path),
                }
                return {
                    "status": "duplicate",
                    "event_id": event_id,
                    "source_integrity": {"legacy_review_db": legacy_integrity, "victor_vocab_db": victor_integrity},
                    "source_hashes_before": source_hashes,
                    "source_hashes_after": source_hashes_after,
                    "source_unchanged": source_hashes == source_hashes_after,
                    "source_counts": legacy_counts,
                    "expected_reconciliation": {
                        "legacy_content_items_expected": 372,
                        "legacy_attempts_expected": 298,
                        "content_items_match": legacy_counts["content_items"] == 372,
                        "attempts_match": legacy_counts["attempts"] == 298,
                    },
                    "mastery_json": {
                        "items": len(mastery_items),
                        "history_rows": sum(len(item.get("history") or []) for item in mastery_items),
                        "item_links": conn.execute("SELECT COUNT(*) FROM external_references WHERE namespace='mastery_json'").fetchone()[0],
                        "new_items": mastery_new_items,
                        "history_linked_to_legacy_attempt": mastery_history_linked,
                        "history_retained_without_fabricated_attempt": mastery_history_retained,
                    },
                    "victor_vocab": {
                        "source_entries": victor_count,
                        "linked_unique_entries": victor_links,
                        "duplicate_legacy_candidates_not_relinked": len(matched_entries) - len(set(matched_entries)),
                    },
                    "target_counts_after_migration": {
                        "content_items": legacy_counts["content_items"] + mastery_new_items,
                        "attempts": legacy_counts["attempts"],
                        "evaluations": legacy_counts["attempts"],
                        "review_state": legacy_counts["review_state"],
                        "review_tasks": legacy_counts["correction_tasks"],
                        "external_references": legacy_counts["content_items"]
                        + conn.execute("SELECT COUNT(*) FROM external_references WHERE namespace='mastery_json'").fetchone()[0]
                        + victor_links,
                        "legacy_records": current_counts["legacy_records"],
                    },
                    "target_counts_current": current_counts,
                    "existing": existing,
                }
            if not conn.execute("SELECT 1 FROM students WHERE student_id=?", (student_id,)).fetchone():
                raise RuntimeError(f"Student not initialized: {student_id}")
            now = utc_now()
            legacy_students = [dict(row) for row in legacy.execute("SELECT * FROM students")]
            for row in legacy_students:
                _insert_legacy_record(
                    conn,
                    event_id,
                    "legacy_review",
                    "student",
                    str(row["student_id"]),
                    row,
                    "linked",
                    "student",
                    student_id,
                )

            legacy_items = [dict(row) for row in legacy.execute("SELECT * FROM content_items ORDER BY item_id")]
            for row in legacy_items:
                metadata = {
                    "source": row["source"],
                    "source_version": row["source_version"],
                    "unit": row["unit"],
                    "source_page": row["source_page"],
                    "pdf_page": row["pdf_page"],
                    "hint": row["hint"],
                    "tags": row["tags"],
                    "source_ref": row["source_ref"],
                }
                response_mode = "active_recall" if row["item_type"] in {"recall", "word", "phrase", "短语", "句型"} else "mixed"
                conn.execute(
                    """
                    INSERT INTO content_items(
                      item_id, domain, item_type, prompt_snapshot, answer_snapshot,
                      direction, response_mode, source_validation_status, legacy_ref,
                      metadata_json, content_hash, created_by_event_id, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row["item_id"],
                        row["domain"],
                        row["item_type"],
                        row["prompt"],
                        row["answer"],
                        "prompt_to_answer",
                        response_mode,
                        "verified" if row["verified"] else "needs_check",
                        f"legacy_review.content_items:{row['item_id']}",
                        canonical_json(metadata),
                        payload_hash({"prompt": row["prompt"], "answer": row["answer"], "type": row["item_type"]}),
                        event_id,
                        row["created_at"],
                        now,
                    ),
                )
                xref_id = stable_id("XREF", "legacy_review", "content_item", row["item_id"])
                conn.execute(
                    """
                    INSERT INTO external_references(
                      external_reference_id,item_id,namespace,reference_type,
                      external_id,source_validation_status,metadata_json,created_at
                    ) VALUES (?,?,?,?,?,'verified',?,?)
                    """,
                    (xref_id, row["item_id"], "legacy_review", "content_item", row["item_id"], canonical_json({"source": row["source"]}), now),
                )
                _insert_kp_links(conn, row["item_id"], _legacy_item_knowledge(row), "legacy_item_tags")
                _insert_legacy_record(
                    conn, event_id, "legacy_review", "content_item", row["item_id"], row,
                    "migrated", "content_item", row["item_id"]
                )
                _record_event_row(conn, event_id, "content_item", row["item_id"], "insert")

            # Link calibrated Victor entries only when page and target headword agree.
            victor_by_page_head: dict[tuple[int | None, str], list[str]] = defaultdict(list)
            for row in victor.execute("SELECT * FROM vocab_entries"):
                victor_by_page_head[(row["book_page"], _norm(row["headword"]))].append(row["entry_id"])
            victor_links = 0
            victor_duplicate_candidates = 0
            linked_entries: set[str] = set()
            for row in legacy_items:
                if row["domain"] != "vocabulary":
                    continue
                candidates = victor_by_page_head.get((row["source_page"], _answer_head(row["answer"])), [])
                if len(candidates) == 1:
                    entry_id = candidates[0]
                    if entry_id in linked_entries:
                        victor_duplicate_candidates += 1
                        continue
                    linked_entries.add(entry_id)
                    ref_id = stable_id("XREF", "victor_vocab", "vocab_entry", entry_id)
                    conn.execute(
                        """
                        INSERT INTO external_references(
                          external_reference_id,item_id,namespace,reference_type,
                          external_id,source_validation_status,metadata_json,created_at
                        ) VALUES (?,?,?,?,?,'verified',?,?)
                        """,
                        (
                            ref_id,
                            row["item_id"],
                            "victor_vocab",
                            "vocab_entry",
                            entry_id,
                            canonical_json({"match": "book_page+headword"}),
                            now,
                        ),
                    )
                    victor_links += 1

            # Create one legacy session per attempt date.
            dates = [row[0] for row in legacy.execute("SELECT DISTINCT substr(attempted_at,1,10) FROM attempts ORDER BY 1")]
            for day in dates:
                session_id = f"SES-LEGACY-{day.replace('-', '')}"
                conn.execute(
                    """
                    INSERT INTO learning_sessions(
                      session_id,student_id,source_thread,session_type,title,
                      started_at,note,created_by_event_id,created_at,updated_at
                    ) VALUES (?,?, 'migration','legacy_activity',?,?,?,?,?,?)
                    """,
                    (session_id, student_id, f"Migrated activity {day}", day, "Migrated from the read-only legacy review database.", event_id, now, now),
                )
                _record_event_row(conn, event_id, "learning_session", session_id, "insert")

            legacy_attempt_rows = [dict(row) for row in legacy.execute("SELECT * FROM attempts ORDER BY attempt_id")]
            legacy_attempt_target: dict[int, str] = {}
            source_key_to_attempt: dict[str, str] = {}
            for row in legacy_attempt_rows:
                attempt_id = f"LEGACY-ATT-{row['attempt_id']:06d}"
                legacy_attempt_target[row["attempt_id"]] = attempt_id
                if row.get("source_key") and row["source_key"] not in source_key_to_attempt:
                    source_key_to_attempt[row["source_key"]] = attempt_id
                capture = "captured_blank" if not (row["student_answer"] or "").strip() else "captured"
                event_key = f"LEGACY-ATTEMPT-{row['attempt_id']}"
                conn.execute(
                    """
                    INSERT INTO attempts(
                      attempt_id,event_id,ingest_event_id,student_id,session_id,item_id,
                      attempted_at,student_answer,standard_answer_snapshot,
                      answer_capture_status,attempt_phase,response_mode,validation_status,
                      teacher_note,source_material_ref,created_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        attempt_id,
                        event_key,
                        event_id,
                        student_id,
                        f"SES-LEGACY-{str(row['attempted_at'])[:10].replace('-', '')}",
                        row["item_id"],
                        row["attempted_at"],
                        row["student_answer"],
                        conn.execute("SELECT answer_snapshot FROM content_items WHERE item_id=?", (row["item_id"],)).fetchone()[0],
                        capture,
                        "review" if (row["source_key"] or "").startswith("mastery:") and not (row["source_key"] or "").endswith(":0") else "first",
                        "active_recall" if conn.execute("SELECT domain FROM content_items WHERE item_id=?", (row["item_id"],)).fetchone()[0] == "vocabulary" else "mixed",
                        "verified",
                        row["notes"],
                        row["source_key"],
                        row["created_at"],
                    ),
                )
                eval_id = stable_id("EVAL", attempt_id, 1)
                conn.execute(
                    """
                    INSERT INTO evaluations(
                      evaluation_id,attempt_id,revision_no,result,score,max_score,
                      evaluated_by,is_human_corrected,note,created_at
                    ) VALUES (?,?,1,?,?,1.0,'legacy_migration',0,?,?)
                    """,
                    (
                        eval_id,
                        attempt_id,
                        row["status"] if row["status"] in {"correct", "partial", "wrong"} else "needs_check",
                        row["score"],
                        canonical_json({"legacy_grade": row["grade"], "legacy_confidence": row["confidence"]}),
                        row["created_at"],
                    ),
                )
                if row["error_type"]:
                    error_type_id, raw, confidence, note = _resolve_error_type(conn, row["error_type"])
                    conn.execute(
                        """
                        INSERT INTO attempt_error_map(
                          attempt_id,error_type_id,raw_error_type,confidence,note
                        ) VALUES (?,?,?,?,?)
                        """,
                        (attempt_id, error_type_id, raw, confidence, note),
                    )
                _insert_legacy_record(
                    conn, event_id, "legacy_review", "attempt", str(row["attempt_id"]), row,
                    "migrated", "attempt", attempt_id
                )
                _record_event_row(conn, event_id, "attempt", attempt_id, "insert")

            for row in legacy.execute("SELECT * FROM review_state"):
                row = dict(row)
                last_attempt = None
                if row["last_reviewed"]:
                    hit = legacy.execute(
                        """SELECT attempt_id FROM attempts
                           WHERE student_id=? AND item_id=? AND attempted_at=?
                           ORDER BY attempt_id DESC LIMIT 1""",
                        (row["student_id"], row["item_id"], row["last_reviewed"]),
                    ).fetchone()
                    if hit:
                        last_attempt = legacy_attempt_target[hit[0]]
                state = "due" if row["due_date"] <= "2026-08-01" else "learning"
                if row["flag"] == "suspended":
                    state = "suspended"
                conn.execute(
                    """
                    INSERT INTO review_state(
                      student_id,item_id,state,due_at,interval_days,stability,difficulty,
                      repetitions,lapses,consecutive_errors,last_attempt_id,last_result,
                      last_reviewed_at,scheduling_algorithm,algorithm_version,updated_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'legacy-fsrs','legacy',?)
                    """,
                    (
                        student_id,
                        row["item_id"],
                        state,
                        row["due_date"],
                        row["interval_days"],
                        row["stability"],
                        row["difficulty"],
                        row["reps"],
                        row["lapses"],
                        row["error_streak"],
                        last_attempt,
                        row["last_status"] if row["last_status"] in {"correct", "partial", "wrong"} else None,
                        row["last_reviewed"],
                        now,
                    ),
                )
                _insert_legacy_record(
                    conn, event_id, "legacy_review", "review_state", f"{row['student_id']}:{row['item_id']}", row,
                    "migrated", "review_state", f"{student_id}:{row['item_id']}"
                )

            latest_open_task = {
                row["item_id"]: row["max_task_id"]
                for row in legacy.execute(
                    """SELECT item_id, MAX(task_id) AS max_task_id
                       FROM correction_tasks WHERE status='open' GROUP BY item_id"""
                )
            }
            for row in legacy.execute("SELECT * FROM correction_tasks ORDER BY task_id"):
                row = dict(row)
                task_id = f"LEGACY-RT-{row['task_id']:06d}"
                source_attempt = legacy_attempt_target.get(row["attempt_id"]) if row["attempt_id"] else None
                if row["status"] == "completed":
                    status = "completed"
                elif latest_open_task.get(row["item_id"]) == row["task_id"]:
                    status = "open"
                else:
                    status = "cancelled"
                reason = row["reason"] or row["mode"] or "review"
                if status == "cancelled":
                    reason = "legacy_consolidated_duplicate:" + reason
                conn.execute(
                    """
                    INSERT INTO review_tasks(
                      review_task_id,student_id,item_id,source_attempt_id,reason_code,
                      due_at,priority,status,created_by_event_id,created_at,completed_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        task_id,
                        student_id,
                        row["item_id"],
                        source_attempt,
                        "legacy_correction:" + reason,
                        row["assigned_on"],
                        1.0 + min(row["repetitions"], 10) / 10,
                        status,
                        event_id,
                        row["assigned_on"],
                        row["completed_on"],
                    ),
                )
                _insert_legacy_record(
                    conn, event_id, "legacy_review", "correction_task", str(row["task_id"]), row,
                    "migrated", "review_task", task_id
                )

            # Store aggregate error summaries and testing records for audit without
            # inventing additional attempt-level facts.
            for table, key_fields in (
                ("error_knowledge", ("error_id",)),
                ("study_progress", ("student_id", "source", "studied_on")),
                ("test_runs", ("test_id",)),
                ("test_items", ("test_id", "sequence")),
            ):
                for row in legacy.execute(f"SELECT * FROM {table}"):
                    data = dict(row)
                    key = ":".join(str(data[field]) for field in key_fields)
                    _insert_legacy_record(
                        conn, event_id, "legacy_review", table, key, data,
                        "retained_only", note="Retained for audit; not promoted into a new attempt fact."
                    )

            # Link mastery JSON items to legacy items using source_key first and
            # prompt/answer equality second. Keep scheduler-only history as audit
            # records instead of fabricating answers.
            attempt_item_map: dict[str, set[str]] = defaultdict(set)
            for row in legacy_attempt_rows:
                match = re.match(r"mastery:([^:]+):", row.get("source_key") or "")
                if match:
                    attempt_item_map[match.group(1)].add(row["item_id"])
            # Use the complete legacy answer here. JSON-origin legacy rows often
            # include a usage suffix, while calibrated legacy rows keep only the
            # target answer. Using only the head would make both rows look equal
            # and create false ambiguity.
            legacy_by_pair: dict[tuple[str, str], list[str]] = defaultdict(list)
            for row in legacy_items:
                legacy_by_pair[(_norm(row["prompt"]), _norm(row["answer"]))].append(row["item_id"])
            mastery_links = 0
            mastery_new_items = 0
            mastery_history_linked = 0
            mastery_history_retained = 0
            for item in mastery_items:
                mastery_id = str(item["id"])
                candidates = attempt_item_map.get(mastery_id, set())
                target_item = next(iter(candidates)) if len(candidates) == 1 else None
                if not target_item:
                    pair_candidates = legacy_by_pair.get((_norm(item.get("front")), _answer_head(item.get("back"))), [])
                    if len(pair_candidates) == 1:
                        target_item = pair_candidates[0]
                if not target_item:
                    target_item = f"MST-{mastery_id}"
                    conn.execute(
                        """
                        INSERT INTO content_items(
                          item_id,domain,item_type,prompt_snapshot,answer_snapshot,
                          direction,response_mode,source_validation_status,legacy_ref,
                          metadata_json,content_hash,created_by_event_id,created_at,updated_at
                        ) VALUES (?,?,?,?,?,'prompt_to_answer','active_recall','needs_check',?,?,?,?,?,?)
                        """,
                        (
                            target_item,
                            "knowledge" if item.get("type") == "concept" else "vocabulary",
                            item.get("type", "recall"),
                            item.get("front"),
                            item.get("back"),
                            f"mastery_json.items:{mastery_id}",
                            canonical_json({key: item.get(key) for key in ("topic", "source", "created")}),
                            payload_hash({"front": item.get("front"), "back": item.get("back")}),
                            event_id,
                            item.get("created") or now,
                            now,
                        ),
                    )
                    _insert_kp_links(conn, target_item, ["vocabulary", "active_recall"], "mastery_json")
                    mastery_new_items += 1
                ref_id = stable_id("XREF", "mastery_json", "item", mastery_id)
                conn.execute(
                    """
                    INSERT INTO external_references(
                      external_reference_id,item_id,namespace,reference_type,external_id,
                      source_validation_status,metadata_json,created_at
                    ) VALUES (?,?,?,?,?,'source_checked',?,?)
                    """,
                    (ref_id, target_item, "mastery_json", "item", mastery_id, canonical_json({"topic": item.get("topic")}), now),
                )
                mastery_links += 1
                _insert_legacy_record(
                    conn, event_id, "mastery_json", "item", mastery_id, item,
                    "linked" if not target_item.startswith("MST-") else "migrated",
                    "content_item", target_item
                )
                for index, history in enumerate(item.get("history") or []):
                    source_key = f"mastery:{mastery_id}:{index}"
                    linked_attempt = source_key_to_attempt.get(source_key)
                    if linked_attempt:
                        status = "linked"
                        mastery_history_linked += 1
                    else:
                        status = "retained_only"
                        mastery_history_retained += 1
                    _insert_legacy_record(
                        conn,
                        event_id,
                        "mastery_json",
                        "history",
                        f"{mastery_id}:{index}",
                        history,
                        status,
                        "attempt" if linked_attempt else None,
                        linked_attempt,
                        None if linked_attempt else "Scheduler-only or non-identical history retained; no attempt was fabricated.",
                    )

            total_source_records = sum(legacy_counts.values()) + len(mastery_items) + sum(len(item.get("history") or []) for item in mastery_items)
            _finish_event(conn, event_id, total_source_records, legacy_counts["content_items"] + legacy_counts["attempts"] + mastery_new_items)
            after_counts = {
                table: conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
                for table in (
                    "content_items",
                    "attempts",
                    "evaluations",
                    "review_state",
                    "review_tasks",
                    "external_references",
                    "legacy_records",
                )
            }
        source_hashes_after = {
            "legacy_review_db": _sha256(legacy_path),
            "mastery_json": _sha256(mastery_path),
            "victor_vocab_db": _sha256(victor_path),
        }
        return {
            "status": "applied",
            "event_id": event_id,
            "source_integrity": {"legacy_review_db": legacy_integrity, "victor_vocab_db": victor_integrity},
            "source_hashes_before": source_hashes,
            "source_hashes_after": source_hashes_after,
            "source_unchanged": source_hashes == source_hashes_after,
            "source_counts": legacy_counts,
            "expected_reconciliation": {
                "legacy_content_items_expected": 372,
                "legacy_attempts_expected": 298,
                "content_items_match": legacy_counts["content_items"] == 372,
                "attempts_match": legacy_counts["attempts"] == 298,
            },
            "mastery_json": {
                "items": len(mastery_items),
                "history_rows": sum(len(item.get("history") or []) for item in mastery_items),
                "item_links": mastery_links,
                "new_items": mastery_new_items,
                "history_linked_to_legacy_attempt": mastery_history_linked,
                "history_retained_without_fabricated_attempt": mastery_history_retained,
            },
            "victor_vocab": {
                "source_entries": victor_count,
                "linked_unique_entries": victor_links,
                "duplicate_legacy_candidates_not_relinked": victor_duplicate_candidates,
            },
            "target_counts_after_migration": after_counts,
        }
    finally:
        legacy.close()
        victor.close()
