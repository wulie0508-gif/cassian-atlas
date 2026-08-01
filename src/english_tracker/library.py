from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import zipfile
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from xml.etree import ElementTree

from .util import stable_id, utc_now


SOURCE_EXTENSIONS = {
    ".doc",
    ".docx",
    ".pdf",
    ".mp3",
    ".wav",
    ".png",
    ".jpg",
    ".jpeg",
    ".txt",
    ".md",
    ".json",
    ".csv",
    ".xlsx",
}

DERIVED_ROOTS = {"00_结构化题库_20260731", "00_学习管理系统"}
ENGLISH_EXAM_MARKER = "3-英语高考真题"
NON_ENGLISH_EXAM_RE = re.compile(r"[\\/](?:1-语文|2-数学|4-物理|5-化学|6-生物|7-政治|8-历史|9-地理)高考真题[\\/]")


def _media_kind(extension: str) -> str:
    if extension in {".doc", ".docx", ".txt", ".md"}:
        return "document"
    if extension == ".pdf":
        return "pdf"
    if extension in {".mp3", ".wav"}:
        return "audio"
    if extension in {".png", ".jpg", ".jpeg"}:
        return "image"
    if extension in {".json", ".csv", ".xlsx"}:
        return "data"
    return "other"


def _subject_scope(relative_path: str) -> str:
    normalized = relative_path.replace("/", "\\")
    if ENGLISH_EXAM_MARKER in normalized:
        return "english"
    if NON_ENGLISH_EXAM_RE.search("\\" + normalized):
        return "non_english"
    # Every source folder in this repository outside the mislabeled multi-subject
    # archive is part of the supplied English library.
    return "english"


def _year_hint(path: Path) -> int | None:
    for text in (path.name, str(path.parent)):
        match = re.search(r"(?<!\d)((?:19|20)\d{2})(?!\d)", text)
        if match:
            year = int(match.group(1))
            if 1980 <= year <= 2100:
                return year
    return None


def _initial_status(scope: str, extension: str) -> str:
    if scope == "non_english":
        return "excluded_non_english"
    if extension == ".doc":
        return "needs_conversion"
    if extension in {".docx", ".pdf", ".txt", ".md", ".json", ".csv"}:
        return "queued"
    return "indexed"


def _iter_source_files(root: Path) -> Iterable[Path]:
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        try:
            relative = path.relative_to(root)
        except ValueError:
            continue
        if not relative.parts or relative.parts[0] in DERIVED_ROOTS:
            continue
        if any(part in {".git", "node_modules", "__pycache__"} for part in relative.parts):
            continue
        if path.suffix.lower() not in SOURCE_EXTENSIONS:
            continue
        yield path


def scan_library(conn, root: str | Path, *, library_key: str = "english_library") -> dict[str, Any]:
    root_path = Path(root).expanduser().resolve()
    if not root_path.is_dir():
        raise ValueError(f"Library root not found: {root_path}")
    run_id = stable_id("RUN", library_key, "inventory", utc_now(), length=24)
    now = utc_now()
    conn.execute(
        """
        INSERT INTO library_parse_runs(
          parse_run_id,library_key,mode,status,started_at,options_json
        ) VALUES (?,?,?,'running',?,?)
        """,
        (run_id, library_key, "inventory", now, json.dumps({"root": str(root_path)}, ensure_ascii=False)),
    )
    processed = 0
    english = 0
    non_english = 0
    changed = 0
    seen: set[str] = set()
    for path in _iter_source_files(root_path):
        relative = path.relative_to(root_path).as_posix()
        seen.add(relative)
        stat = path.stat()
        extension = path.suffix.lower()
        scope = _subject_scope(relative)
        processed += 1
        english += scope == "english"
        non_english += scope == "non_english"
        resource_id = stable_id("RES", library_key, relative.lower(), length=24)
        previous = conn.execute(
            "SELECT size_bytes,modified_at,parse_status FROM library_resources WHERE resource_id=?",
            (resource_id,),
        ).fetchone()
        modified = datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat(timespec="seconds")
        reset = previous and (int(previous["size_bytes"]) != stat.st_size or previous["modified_at"] != modified)
        status = _initial_status(scope, extension) if reset or not previous else previous["parse_status"]
        if reset or not previous:
            changed += 1
        conn.execute(
            """
            INSERT INTO library_resources(
              resource_id,library_key,absolute_path,relative_path,file_name,extension,media_kind,
              subject_scope,source_group,year_hint,size_bytes,modified_at,parse_status,
              verification_status,indexed_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?, 'unverified',?,?)
            ON CONFLICT(resource_id) DO UPDATE SET
              absolute_path=excluded.absolute_path,
              relative_path=excluded.relative_path,
              file_name=excluded.file_name,
              extension=excluded.extension,
              media_kind=excluded.media_kind,
              subject_scope=excluded.subject_scope,
              source_group=excluded.source_group,
              year_hint=excluded.year_hint,
              size_bytes=excluded.size_bytes,
              modified_at=excluded.modified_at,
              parse_status=CASE
                WHEN library_resources.size_bytes<>excluded.size_bytes
                  OR library_resources.modified_at<>excluded.modified_at
                THEN excluded.parse_status
                ELSE library_resources.parse_status
              END,
              sha256=CASE
                WHEN library_resources.size_bytes<>excluded.size_bytes
                  OR library_resources.modified_at<>excluded.modified_at
                THEN NULL ELSE library_resources.sha256 END,
              duplicate_of_resource_id=CASE
                WHEN library_resources.size_bytes<>excluded.size_bytes
                  OR library_resources.modified_at<>excluded.modified_at
                THEN NULL ELSE library_resources.duplicate_of_resource_id END,
              updated_at=excluded.updated_at
            """,
            (
                resource_id,
                library_key,
                str(path),
                relative,
                path.name,
                extension,
                _media_kind(extension),
                scope,
                relative.split("/", 1)[0],
                _year_hint(path),
                stat.st_size,
                modified,
                status,
                now,
                now,
            ),
        )
    # Keep missing records for audit; mark them for review rather than deleting them.
    missing = 0
    for row in conn.execute("SELECT resource_id,relative_path FROM library_resources WHERE library_key=?", (library_key,)):
        if row["relative_path"] not in seen:
            missing += 1
            conn.execute(
                "UPDATE library_resources SET parse_status='needs_review',last_error=?,updated_at=? WHERE resource_id=?",
                ("源文件当前不存在；记录保留用于审计。", now, row["resource_id"]),
            )
    summary = {
        "root": str(root_path),
        "indexed_resources": processed,
        "english_resources": english,
        "excluded_non_english_resources": non_english,
        "changed_or_new": changed,
        "missing_since_last_scan": missing,
    }
    conn.execute(
        """
        UPDATE library_parse_runs
        SET status='completed',total_resources=?,processed_resources=?,successful_resources=?,
            finished_at=?,summary_json=? WHERE parse_run_id=?
        """,
        (processed, processed, processed, utc_now(), json.dumps(summary, ensure_ascii=False), run_id),
    )
    conn.execute(
        """
        UPDATE project_work_items SET total_units=?,completed_units=(
          SELECT COUNT(*) FROM library_resources
          WHERE library_key=? AND subject_scope='english'
            AND parse_status IN ('structured','ingested')
        ),updated_at=? WHERE work_item_id='WORK-FULL-PARSE'
        """,
        (english, library_key, utc_now()),
    )
    conn.commit()
    return {"parse_run_id": run_id, **summary}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def hash_library(
    conn,
    *,
    library_key: str = "english_library",
    include_audio: bool = False,
    limit: int = 0,
) -> dict[str, Any]:
    conditions = ["library_key=?", "subject_scope='english'", "sha256 IS NULL"]
    params: list[Any] = [library_key]
    if not include_audio:
        conditions.append("media_kind<>'audio'")
    sql = "SELECT * FROM library_resources WHERE " + " AND ".join(conditions) + " ORDER BY size_bytes,relative_path"
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    rows = list(conn.execute(sql, params))
    run_id = stable_id("RUN", library_key, "hash", utc_now(), length=24)
    conn.execute(
        """
        INSERT INTO library_parse_runs(
          parse_run_id,library_key,mode,status,total_resources,started_at,options_json
        ) VALUES (?,?,'hash','running',?,?,?)
        """,
        (run_id, library_key, len(rows), utc_now(), json.dumps({"include_audio": include_audio, "limit": limit})),
    )
    successful = 0
    failed = 0
    for row in rows:
        try:
            digest = _sha256(Path(row["absolute_path"]))
            conn.execute(
                "UPDATE library_resources SET sha256=?,updated_at=?,last_error=NULL WHERE resource_id=?",
                (digest, utc_now(), row["resource_id"]),
            )
            successful += 1
        except OSError as exc:
            failed += 1
            conn.execute(
                "UPDATE library_resources SET parse_status='failed',last_error=?,updated_at=? WHERE resource_id=?",
                (str(exc), utc_now(), row["resource_id"]),
            )
        if (successful + failed) % 50 == 0:
            conn.commit()
            conn.execute(
                "UPDATE library_parse_runs SET processed_resources=?,successful_resources=?,failed_resources=? WHERE parse_run_id=?",
                (successful + failed, successful, failed, run_id),
            )
            conn.commit()
    # Prefer DOCX, then DOC, then PDF, and prefer shorter paths as canonical.
    duplicate_groups = conn.execute(
        """
        SELECT sha256 FROM library_resources
        WHERE library_key=? AND subject_scope='english' AND sha256 IS NOT NULL
        GROUP BY sha256 HAVING COUNT(*)>1
        """,
        (library_key,),
    ).fetchall()
    duplicate_count = 0
    preference = {".docx": 0, ".doc": 1, ".pdf": 2, ".txt": 3, ".md": 4, ".mp3": 5, ".wav": 6}
    for group in duplicate_groups:
        items = list(
            conn.execute(
                "SELECT resource_id,extension,relative_path FROM library_resources WHERE library_key=? AND sha256=?",
                (library_key, group["sha256"]),
            )
        )
        items.sort(key=lambda row: (preference.get(row["extension"], 9), len(row["relative_path"]), row["relative_path"]))
        canonical = items[0]["resource_id"]
        conn.execute(
            "UPDATE library_resources SET is_canonical=1,duplicate_of_resource_id=NULL WHERE resource_id=?",
            (canonical,),
        )
        for item in items[1:]:
            duplicate_count += 1
            conn.execute(
                "UPDATE library_resources SET is_canonical=0,duplicate_of_resource_id=? WHERE resource_id=?",
                (canonical, item["resource_id"]),
            )
    status = "completed_with_errors" if failed else "completed"
    summary = {"hashed": successful, "failed": failed, "exact_duplicate_files": duplicate_count}
    conn.execute(
        """
        UPDATE library_parse_runs SET status=?,processed_resources=?,successful_resources=?,failed_resources=?,
          finished_at=?,summary_json=? WHERE parse_run_id=?
        """,
        (status, successful + failed, successful, failed, utc_now(), json.dumps(summary), run_id),
    )
    conn.commit()
    return {"parse_run_id": run_id, **summary}


def _docx_text(path: Path) -> str:
    with zipfile.ZipFile(path) as archive:
        names = [name for name in archive.namelist() if name == "word/document.xml" or name.startswith("word/header") or name.startswith("word/footer")]
        paragraphs: list[str] = []
        for name in names:
            root = ElementTree.fromstring(archive.read(name))
            for paragraph in root.iter("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p"):
                text = "".join(node.text or "" for node in paragraph.iter("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t"))
                if text.strip():
                    paragraphs.append(text.strip())
        return "\n".join(paragraphs)


def _pdf_text(path: Path) -> tuple[str, int]:
    try:
        from pypdf import PdfReader  # type: ignore
    except ImportError as exc:  # pragma: no cover - optional parser dependency
        raise RuntimeError("PDF文本提取需要可选依赖 pypdf；扫描PDF仍需OCR。") from exc
    reader = PdfReader(str(path))
    return "\n\n".join((page.extract_text() or "") for page in reader.pages), len(reader.pages)


def extract_library_text(
    conn,
    cache_root: str | Path,
    *,
    library_key: str = "english_library",
    limit: int = 0,
) -> dict[str, Any]:
    cache = Path(cache_root).expanduser().resolve()
    cache.mkdir(parents=True, exist_ok=True)
    params: list[Any] = [library_key]
    sql = """
      SELECT * FROM library_resources
      WHERE library_key=? AND subject_scope='english' AND is_canonical=1
        AND parse_status IN ('queued','failed','extracting')
        AND extension IN ('.docx','.pdf','.txt','.md','.json','.csv')
      ORDER BY CASE extension WHEN '.docx' THEN 0 WHEN '.txt' THEN 1 WHEN '.md' THEN 2 WHEN '.json' THEN 3 WHEN '.csv' THEN 4 ELSE 5 END,
               size_bytes,relative_path
    """
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    rows = list(conn.execute(sql, params))
    run_id = stable_id("RUN", library_key, "extract", utc_now(), length=24)
    conn.execute(
        """
        INSERT INTO library_parse_runs(
          parse_run_id,library_key,mode,status,total_resources,started_at,options_json
        ) VALUES (?,?,'extract','running',?,?,?)
        """,
        (run_id, library_key, len(rows), utc_now(), json.dumps({"cache_root": str(cache), "limit": limit}, ensure_ascii=False)),
    )
    success = 0
    failed = 0
    needs_ocr = 0
    for row in rows:
        resource_id = row["resource_id"]
        path = Path(row["absolute_path"])
        conn.execute("UPDATE library_resources SET parse_status='extracting',updated_at=? WHERE resource_id=?", (utc_now(), resource_id))
        try:
            pages = None
            method = "plain_text"
            if row["extension"] == ".docx":
                text = _docx_text(path)
                method = "docx_xml"
            elif row["extension"] == ".pdf":
                text, pages = _pdf_text(path)
                method = "pdf_text_layer"
            else:
                text = path.read_text(encoding="utf-8", errors="replace")
            normalized = re.sub(r"\r\n?", "\n", text).strip()
            if row["extension"] == ".pdf" and len(normalized) < max(120, (pages or 1) * 30):
                needs_ocr += 1
                conn.execute(
                    """
                    UPDATE library_resources SET parse_status='needs_ocr',extraction_method=?,
                      extracted_char_count=?,page_count=?,last_error=?,updated_at=? WHERE resource_id=?
                    """,
                    (method, len(normalized), pages, "PDF文本层不足，需要OCR。", utc_now(), resource_id),
                )
            else:
                output = cache / f"{resource_id}.txt"
                output.write_text(normalized, encoding="utf-8", newline="\n")
                probable_questions = len(re.findall(r"(?m)^\s*(?:\d{1,3}|[A-D])\s*[.．、)]\s+", normalized))
                conn.execute(
                    """
                    UPDATE library_resources SET parse_status='extracted',extraction_method=?,
                      extracted_text_path=?,extracted_char_count=?,page_count=?,question_count=?,
                      last_error=NULL,updated_at=? WHERE resource_id=?
                    """,
                    (method, str(output), len(normalized), pages, probable_questions, utc_now(), resource_id),
                )
                success += 1
        except (OSError, ValueError, RuntimeError, zipfile.BadZipFile, sqlite3.Error) as exc:
            failed += 1
            status = "needs_ocr" if row["extension"] == ".pdf" else "failed"
            conn.execute(
                "UPDATE library_resources SET parse_status=?,last_error=?,updated_at=? WHERE resource_id=?",
                (status, str(exc), utc_now(), resource_id),
            )
        processed = success + failed + needs_ocr
        if processed % 25 == 0:
            conn.execute(
                "UPDATE library_parse_runs SET processed_resources=?,successful_resources=?,failed_resources=? WHERE parse_run_id=?",
                (processed, success, failed, run_id),
            )
            conn.commit()
    status = "completed_with_errors" if failed or needs_ocr else "completed"
    summary = {"extracted": success, "needs_ocr": needs_ocr, "failed": failed}
    conn.execute(
        """
        UPDATE library_parse_runs SET status=?,processed_resources=?,successful_resources=?,failed_resources=?,
          finished_at=?,summary_json=? WHERE parse_run_id=?
        """,
        (status, success + failed + needs_ocr, success, failed, utc_now(), json.dumps(summary), run_id),
    )
    conn.commit()
    return {"parse_run_id": run_id, **summary}


def convert_legacy_word(
    conn,
    cache_root: str | Path,
    *,
    library_key: str = "english_library",
    limit: int = 100,
) -> dict[str, Any]:
    if limit <= 0:
        raise ValueError("Legacy Word conversion requires a positive batch limit")
    try:
        import pythoncom  # type: ignore
        import win32com.client  # type: ignore
    except ImportError as exc:  # pragma: no cover - optional Windows parser dependency
        raise RuntimeError("旧版DOC转换需要安装可选依赖：pip install -e .[parsing]") from exc
    cache = Path(cache_root).expanduser().resolve()
    conversion_root = cache / "converted_docx"
    text_root = cache / "extracted"
    conversion_root.mkdir(parents=True, exist_ok=True)
    text_root.mkdir(parents=True, exist_ok=True)
    rows = list(
        conn.execute(
            """
            SELECT * FROM library_resources
            WHERE library_key=? AND subject_scope='english' AND is_canonical=1
              AND extension='.doc' AND parse_status IN ('needs_conversion','failed')
            ORDER BY size_bytes,relative_path LIMIT ?
            """,
            (library_key, limit),
        )
    )
    run_id = stable_id("RUN", library_key, "doc-convert", utc_now(), length=24)
    conn.execute(
        """
        INSERT INTO library_parse_runs(
          parse_run_id,library_key,mode,status,total_resources,started_at,options_json
        ) VALUES (?,?,'extract','running',?,?,?)
        """,
        (run_id, library_key, len(rows), utc_now(), json.dumps({"legacy_doc_conversion": True, "limit": limit}, ensure_ascii=False)),
    )
    conn.commit()
    success = 0
    failed = 0
    pythoncom.CoInitialize()
    word = None
    try:
        word = win32com.client.DispatchEx("Word.Application")
        word.Visible = False
        word.DisplayAlerts = 0
        try:
            word.AutomationSecurity = 3
        except Exception:
            pass
        for row in rows:
            resource_id = row["resource_id"]
            source_path = Path(row["absolute_path"])
            output_docx = conversion_root / f"{resource_id}.docx"
            output_text = text_root / f"{resource_id}.txt"
            document = None
            try:
                conn.execute(
                    "UPDATE library_resources SET parse_status='extracting',updated_at=? WHERE resource_id=?",
                    (utc_now(), resource_id),
                )
                conn.commit()
                document = word.Documents.Open(
                    FileName=str(source_path),
                    ConfirmConversions=False,
                    ReadOnly=True,
                    AddToRecentFiles=False,
                    Visible=False,
                    OpenAndRepair=True,
                    NoEncodingDialog=True,
                )
                document.SaveAs2(FileName=str(output_docx), FileFormat=16, AddToRecentFiles=False)
                document.Close(SaveChanges=False)
                document = None
                text = _docx_text(output_docx)
                normalized = re.sub(r"\r\n?", "\n", text).strip()
                output_text.write_text(normalized, encoding="utf-8", newline="\n")
                probable_questions = len(re.findall(r"(?m)^\s*(?:\d{1,3}|[A-D])\s*[.．、)]\s+", normalized))
                conn.execute(
                    """
                    UPDATE library_resources SET parse_status='extracted',extraction_method='word_com_doc_conversion',
                      extracted_text_path=?,extracted_char_count=?,question_count=?,last_error=NULL,updated_at=?
                    WHERE resource_id=?
                    """,
                    (str(output_text), len(normalized), probable_questions, utc_now(), resource_id),
                )
                success += 1
            except Exception as exc:  # COM exceptions have several runtime-specific types
                failed += 1
                if document is not None:
                    try:
                        document.Close(SaveChanges=False)
                    except Exception:
                        pass
                conn.execute(
                    "UPDATE library_resources SET parse_status='failed',last_error=?,updated_at=? WHERE resource_id=?",
                    (str(exc)[:2000], utc_now(), resource_id),
                )
            conn.execute(
                "UPDATE library_parse_runs SET processed_resources=?,successful_resources=?,failed_resources=? WHERE parse_run_id=?",
                (success + failed, success, failed, run_id),
            )
            conn.commit()
    finally:
        if word is not None:
            try:
                word.Quit()
            except Exception:
                pass
        pythoncom.CoUninitialize()
    status = "completed_with_errors" if failed else "completed"
    remaining = conn.execute(
        """
        SELECT COUNT(*) FROM library_resources
        WHERE library_key=? AND subject_scope='english' AND is_canonical=1
          AND extension='.doc' AND parse_status IN ('needs_conversion','failed')
        """,
        (library_key,),
    ).fetchone()[0]
    summary = {"converted_and_extracted": success, "failed": failed, "remaining_batchable": remaining}
    conn.execute(
        """
        UPDATE library_parse_runs SET status=?,finished_at=?,summary_json=? WHERE parse_run_id=?
        """,
        (status, utc_now(), json.dumps(summary), run_id),
    )
    conn.commit()
    return {"parse_run_id": run_id, **summary}


def propagate_duplicate_status(conn, *, library_key: str = "english_library") -> dict[str, Any]:
    rows = list(
        conn.execute(
            """
            SELECT d.resource_id,c.parse_status,c.extraction_method,c.extracted_text_path,
                   c.extracted_char_count,c.page_count,c.question_count,c.passage_count,
                   c.knowledge_mapping_count,c.verification_status
            FROM library_resources d
            JOIN library_resources c ON c.resource_id=d.duplicate_of_resource_id
            WHERE d.library_key=? AND d.subject_scope='english' AND d.is_canonical=0
              AND c.parse_status IN ('extracted','structured','ingested','needs_ocr','needs_review')
            """,
            (library_key,),
        )
    )
    for row in rows:
        conn.execute(
            """
            UPDATE library_resources SET parse_status=?,extraction_method='exact_duplicate_reuse',
              extracted_text_path=?,extracted_char_count=?,page_count=?,question_count=?,passage_count=?,
              knowledge_mapping_count=?,verification_status=?,last_error=NULL,updated_at=? WHERE resource_id=?
            """,
            (
                row["parse_status"], row["extracted_text_path"], row["extracted_char_count"], row["page_count"],
                row["question_count"], row["passage_count"], row["knowledge_mapping_count"],
                row["verification_status"], utc_now(), row["resource_id"],
            ),
        )
    text_progress = conn.execute(
        """
        SELECT
          SUM(CASE WHEN parse_status IN ('structured','ingested') THEN 1 ELSE 0 END) completed,
          COUNT(*) total
        FROM library_resources
        WHERE library_key=? AND subject_scope='english'
          AND extension NOT IN ('.mp3','.wav','.m4a','.aac','.flac','.ogg')
        """,
        (library_key,),
    ).fetchone()
    completed = int(text_progress["completed"] or 0)
    total = int(text_progress["total"] or 0)
    conn.execute(
        """
        UPDATE project_work_items SET completed_units=?,total_units=?,status=CASE
          WHEN ? >= ? AND ? > 0 THEN 'completed' ELSE 'in_progress' END,
          evidence_path='exports/library_structure_current_summary.json',updated_at=?
        WHERE work_item_id='WORK-FULL-PARSE'
        """,
        (completed, total, completed, total, total, utc_now()),
    )
    conn.commit()
    return {"duplicates_updated": len(rows)}


def library_summary(conn, *, library_key: str = "english_library") -> dict[str, Any]:
    totals = dict(
        conn.execute(
            """
            SELECT COUNT(*) total_resources,
                   SUM(CASE WHEN subject_scope='english' THEN 1 ELSE 0 END) english_resources,
                   SUM(CASE WHEN subject_scope='non_english' THEN 1 ELSE 0 END) excluded_non_english,
                   SUM(CASE WHEN subject_scope='english' THEN size_bytes ELSE 0 END) english_bytes,
                   SUM(CASE WHEN subject_scope='english' AND is_canonical=0 THEN 1 ELSE 0 END) exact_duplicates,
                   SUM(CASE WHEN subject_scope='english' AND extension IN ('.mp3','.wav','.m4a','.aac','.flac','.ogg') THEN 1 ELSE 0 END) audio_resources,
                   SUM(CASE WHEN subject_scope='english' AND extension NOT IN ('.mp3','.wav','.m4a','.aac','.flac','.ogg') THEN 1 ELSE 0 END) text_resources,
                   SUM(CASE WHEN subject_scope='english' AND parse_status IN ('structured','ingested') THEN 1 ELSE 0 END) completed_resources,
                   SUM(CASE WHEN subject_scope='english' AND parse_status IN ('indexed','extracted','structured','ingested','needs_review') THEN 1 ELSE 0 END) auditable_resources
            FROM library_resources WHERE library_key=?
            """,
            (library_key,),
        ).fetchone()
    )
    by_status = [
        dict(row)
        for row in conn.execute(
            """
            SELECT parse_status,COUNT(*) resource_count,SUM(size_bytes) size_bytes
            FROM library_resources WHERE library_key=? AND subject_scope='english'
            GROUP BY parse_status ORDER BY resource_count DESC
            """,
            (library_key,),
        )
    ]
    by_extension = [
        dict(row)
        for row in conn.execute(
            """
            SELECT extension,COUNT(*) resource_count,SUM(size_bytes) size_bytes
            FROM library_resources WHERE library_key=? AND subject_scope='english'
            GROUP BY extension ORDER BY resource_count DESC
            """,
            (library_key,),
        )
    ]
    by_group = [
        dict(row)
        for row in conn.execute(
            """
            SELECT source_group,COUNT(*) resource_count,SUM(size_bytes) size_bytes,
                   SUM(CASE WHEN parse_status IN ('structured','ingested') THEN 1 ELSE 0 END) completed_resources
            FROM library_resources WHERE library_key=? AND subject_scope='english'
            GROUP BY source_group ORDER BY resource_count DESC
            """,
            (library_key,),
        )
    ]
    latest_run = conn.execute(
        "SELECT * FROM library_parse_runs WHERE library_key=? ORDER BY started_at DESC LIMIT 1", (library_key,)
    ).fetchone()
    english_count = int(totals.get("english_resources") or 0)
    completed = int(totals.get("completed_resources") or 0)
    text_count = int(totals.get("text_resources") or 0)
    auditable = int(totals.get("auditable_resources") or 0)
    result = {
        "library_key": library_key,
        "generated_at": utc_now(),
        "totals": {
            **totals,
            "english_gb": round(float(totals.get("english_bytes") or 0) / 1024**3, 3),
            # Backwards-compatible field: text parsing, not audio transcription.
            "completion_rate": round(completed / text_count, 4) if text_count else 0.0,
            "text_completion_rate": round(completed / text_count, 4) if text_count else 0.0,
            "state_coverage_rate": round(auditable / english_count, 4) if english_count else 0.0,
        },
        "by_status": by_status,
        "by_extension": by_extension,
        "by_source_group": by_group,
        "latest_run": dict(latest_run) if latest_run else None,
    }
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='library_source_sets'").fetchone():
        from .question_pipeline import structure_summary

        result["structure"] = structure_summary(conn, library_key=library_key)
    return result


def reconcile_question_bank(
    conn,
    question_bank: str | Path,
    *,
    library_key: str = "english_library",
) -> dict[str, Any]:
    source = sqlite3.connect(f"file:{Path(question_bank).expanduser().resolve().as_posix()}?mode=ro", uri=True)
    source.row_factory = sqlite3.Row
    source.execute("PRAGMA query_only=ON")
    counts = {
        row["source_id"]: (row["question_count"], row["passage_count"])
        for row in source.execute(
            """
            SELECT s.source_id,COUNT(DISTINCT q.question_id) question_count,
                   COUNT(DISTINCT NULLIF(q.passage_id,'')) passage_count
            FROM sources s LEFT JOIN questions q ON q.source_id=s.source_id
            GROUP BY s.source_id
            """
        )
    }
    used: dict[str, str] = {}
    for row in source.execute(
        """
        SELECT source_id,processing_status,original_path,answer_path,pdf_original_path,pdf_answer_path
        FROM sources WHERE processing_status IN ('completed','ocr_structured')
        """
    ):
        for field in ("original_path", "answer_path", "pdf_original_path", "pdf_answer_path"):
            if row[field]:
                used[str(Path(row[field]).resolve()).casefold()] = row["source_id"]
    matched = 0
    now = utc_now()
    for row in conn.execute(
        "SELECT resource_id,absolute_path FROM library_resources WHERE library_key=? AND subject_scope='english'",
        (library_key,),
    ):
        source_id = used.get(str(Path(row["absolute_path"]).resolve()).casefold())
        if not source_id:
            continue
        question_count, passage_count = counts.get(source_id, (0, 0))
        conn.execute(
            """
            UPDATE library_resources SET parse_status='ingested',question_count=?,passage_count=?,
              extraction_method=COALESCE(extraction_method,'existing_question_bank'),
              verification_status='source_checked',last_error=NULL,updated_at=? WHERE resource_id=?
            """,
            (question_count, passage_count, now, row["resource_id"]),
        )
        matched += 1
    # Exact copies of an ingested file are complete by reuse and retain their duplicate link.
    duplicate_reused = conn.execute(
        """
        UPDATE library_resources SET parse_status='ingested',extraction_method='exact_duplicate_reuse',
          verification_status='source_checked',updated_at=?
        WHERE library_key=? AND subject_scope='english' AND is_canonical=0
          AND duplicate_of_resource_id IN (
            SELECT resource_id FROM library_resources WHERE parse_status='ingested'
          )
        """,
        (now, library_key),
    ).rowcount
    completed = conn.execute(
        """
        SELECT COUNT(*) FROM library_resources
        WHERE library_key=? AND subject_scope='english' AND parse_status IN ('structured','ingested')
        """,
        (library_key,),
    ).fetchone()[0]
    total = conn.execute(
        "SELECT COUNT(*) FROM library_resources WHERE library_key=? AND subject_scope='english'",
        (library_key,),
    ).fetchone()[0]
    conn.execute(
        """
        UPDATE project_work_items SET completed_units=?,total_units=?,evidence_path=?,updated_at=?
        WHERE work_item_id='WORK-FULL-PARSE'
        """,
        (completed, total, str(Path(question_bank).resolve()), now),
    )
    conn.commit()
    source.close()
    return {
        "matched_ingested_source_files": matched,
        "exact_duplicates_completed_by_reuse": duplicate_reused,
        "completed_resources": completed,
        "total_english_resources": total,
    }


def reuse_textbook_ocr(
    conn,
    question_bank: str | Path,
    cache_root: str | Path,
    *,
    library_key: str = "english_library",
) -> dict[str, Any]:
    """Reuse already-indexed textbook OCR pages without changing source PDFs."""
    question_bank_path = Path(question_bank).expanduser().resolve()
    source = sqlite3.connect(f"file:{question_bank_path.as_posix()}?mode=ro", uri=True)
    source.row_factory = sqlite3.Row
    source.execute("PRAGMA query_only=ON")
    cache = Path(cache_root).expanduser().resolve()
    text_root = cache / "extracted"
    text_root.mkdir(parents=True, exist_ok=True)

    def book_key(name: str) -> str:
        value = re.sub(r"\(1\)(?=\.pdf$)", "", name, flags=re.I)
        return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", value.lower())

    books: dict[str, dict[str, Any]] = {}
    for row in source.execute(
        """
        SELECT book_code,MIN(pdf_path) pdf_path,COUNT(*) pages,
               SUM(ocr_char_count) chars,GROUP_CONCAT(normalized_text, char(10) || char(10)) text
        FROM textbook_pages GROUP BY book_code ORDER BY book_code
        """
    ):
        key = book_key(Path(row["pdf_path"]).name)
        question_count = source.execute(
            "SELECT COUNT(*) FROM questions WHERE source_id=?",
            (f"SRC-AWK-2026-{row['book_code'].split('_', 1)[0]}",),
        ).fetchone()[0]
        method_count = source.execute(
            "SELECT COUNT(*) FROM teaching_methods WHERE book_code=?",
            (row["book_code"],),
        ).fetchone()[0]
        books[key] = {**dict(row), "question_count": question_count, "method_count": method_count}

    matched = pages_reused = questions_linked = methods_linked = 0
    for resource in conn.execute(
        """
        SELECT * FROM library_resources
        WHERE library_key=? AND subject_scope='english' AND extension='.pdf'
          AND relative_path LIKE '%2026%\u89e3\u9898\u89c9\u9192%'
        """,
        (library_key,),
    ):
        book = books.get(book_key(resource["file_name"]))
        if not book:
            continue
        normalized = re.sub(r"\r\n?", "\n", book["text"] or "").strip()
        output = text_root / f"{resource['resource_id']}.txt"
        output.write_text(normalized, encoding="utf-8", newline="\n")
        conn.execute(
            """
            UPDATE library_resources SET parse_status='ingested',extraction_method='question_bank_textbook_ocr_reuse',
              extracted_text_path=?,extracted_char_count=?,page_count=?,question_count=?,
              knowledge_mapping_count=?,verification_status='ocr_only',last_error=NULL,updated_at=?
            WHERE resource_id=?
            """,
            (
                str(output), len(normalized), int(book["pages"] or 0), int(book["question_count"] or 0),
                int(book["method_count"] or 0), utc_now(), resource["resource_id"],
            ),
        )
        matched += 1
        pages_reused += int(book["pages"] or 0)
        questions_linked += int(book["question_count"] or 0)
        methods_linked += int(book["method_count"] or 0)
    source.close()
    conn.commit()
    return {
        "question_bank": str(question_bank_path),
        "resources_matched": matched,
        "unique_ocr_pages": sum(int(book["pages"] or 0) for book in books.values()),
        "ocr_pages_reused": pages_reused,
        "question_links": questions_linked,
        "teaching_method_links": methods_linked,
        "verification_status": "ocr_only",
        "source_files_modified": False,
    }


def import_pdf_ocr_json(
    conn,
    resource_id: str,
    json_dir: str | Path,
    cache_root: str | Path,
) -> dict[str, Any]:
    resource = conn.execute(
        "SELECT * FROM library_resources WHERE resource_id=? AND extension='.pdf'",
        (resource_id,),
    ).fetchone()
    if not resource:
        raise ValueError(f"Unknown PDF resource: {resource_id}")
    root = Path(json_dir).expanduser().resolve()
    files = sorted(root.glob("*.json"))
    if not files:
        raise ValueError(f"No OCR JSON files found: {root}")
    pages: list[str] = []
    engines: Counter[str] = Counter()
    failures: list[str] = []
    for path in files:
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
            text = re.sub(r"\r\n?", "\n", str(payload.get("text") or "")).strip()
            pages.append(text)
            engines[str(payload.get("ocr_engine") or "unknown")] += 1
        except (OSError, ValueError, TypeError) as exc:
            failures.append(f"{path.name}: {exc}")
    if not pages or sum(len(page) for page in pages) < 100:
        raise ValueError("OCR output is empty or too short to index safely")
    normalized = "\n\n".join(f"[PAGE {index}]\n{text}" for index, text in enumerate(pages, start=1))
    output_root = Path(cache_root).expanduser().resolve() / "extracted"
    output_root.mkdir(parents=True, exist_ok=True)
    output = output_root / f"{resource_id}.txt"
    output.write_text(normalized, encoding="utf-8", newline="\n")
    probable_questions = len(re.findall(r"(?m)^\s*(?:\d{1,3})\s*[.．、\)]\s+", normalized))
    status = "needs_review" if failures else "extracted"
    conn.execute(
        """
        UPDATE library_resources SET parse_status=?,extraction_method='windows_media_ocr',
          extracted_text_path=?,extracted_char_count=?,page_count=?,question_count=?,
          verification_status='ocr_only',last_error=?,updated_at=? WHERE resource_id=?
        """,
        (
            status, str(output), len(normalized), len(pages), probable_questions,
            "; ".join(failures[:20]) if failures else None, utc_now(), resource_id,
        ),
    )
    run_id = stable_id("RUN", resource_id, "ocr-import", utc_now(), length=24)
    summary = {
        "resource_id": resource_id,
        "source_pdf": resource["absolute_path"],
        "ocr_json_dir": str(root),
        "pages_imported": len(pages),
        "characters": len(normalized),
        "probable_questions": probable_questions,
        "engines": dict(engines),
        "failed_pages": failures,
        "source_files_modified": False,
    }
    conn.execute(
        """
        INSERT INTO library_parse_runs(
          parse_run_id,library_key,mode,status,total_resources,processed_resources,
          successful_resources,failed_resources,started_at,finished_at,options_json,summary_json
        ) VALUES (?,?,'extract',?,1,1,?,?,?, ?,?,?)
        """,
        (
            run_id, resource["library_key"], "completed_with_errors" if failures else "completed",
            0 if failures else 1, 1 if failures else 0, utc_now(), utc_now(),
            json.dumps({"ocr_import": True, "resource_id": resource_id}, ensure_ascii=False),
            json.dumps(summary, ensure_ascii=False),
        ),
    )
    conn.commit()
    return {"parse_run_id": run_id, **summary}


def recent_resources(conn, *, library_key: str = "english_library", status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    params: list[Any] = [library_key]
    clause = ""
    if status:
        clause = " AND parse_status=?"
        params.append(status)
    params.append(max(1, min(limit, 500)))
    return [
        dict(row)
        for row in conn.execute(
            f"""
            SELECT resource_id,relative_path,file_name,extension,media_kind,source_group,year_hint,
                   size_bytes,parse_status,is_canonical,duplicate_of_resource_id,extraction_method,
                   extracted_char_count,question_count,verification_status,last_error,updated_at
            FROM library_resources
            WHERE library_key=? AND subject_scope='english' {clause}
            ORDER BY updated_at DESC,relative_path LIMIT ?
            """,
            params,
        )
    ]
