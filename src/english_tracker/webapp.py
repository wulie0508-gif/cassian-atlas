from __future__ import annotations

import json
import mimetypes
import os
import sqlite3
import webbrowser
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .analytics import due_reviews
from .backup import create_backup
from .dashboard import (
    context_for,
    low_friction_summary,
    overview,
    question_bank_summary,
    question_detail,
    search_questions,
    workflow_summary,
)
from .db import connect, database_path, require_initialized
from .enrichment import search_knowledge
from .grammar_catalog import coverage_matrix, passage_coverage, question_knowledge
from .ingest import import_attempt_diagnostics, import_attempts, import_session
from .library import library_summary, recent_resources
from .metrics import trend_report, weekly_report
from .question_pipeline import (
    search_library_chunks,
    search_staged_questions,
    staged_question_detail,
    structure_summary,
)
from .performance import reading_error_taxonomy, reading_passage_performance, session_performance
from .selection import weighted_set_cover
from .util import random_id, utc_now
from .weights import weight_policy_report, weighted_mastery_report


QUESTION_BANK_ENV = "ENGLISH_TRACKER_QUESTION_BANK"
LIBRARY_ROOT_ENV = "ENGLISH_TRACKER_LIBRARY_ROOT"


def _json_bytes(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str).encode("utf-8")


def _iso_from_date(value: str | None) -> str:
    if not value:
        return datetime.now().astimezone().isoformat(timespec="seconds")
    if "T" not in value:
        return value + "T09:00:00+08:00"
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.isoformat(timespec="seconds")


def _answer_equal(student: str, standard: str) -> bool:
    normalize = lambda value: " ".join(value.casefold().strip().split())
    accepted = [part for part in standard.replace("；", ";").split(";") if part.strip()]
    return normalize(student) in {normalize(value) for value in accepted or [standard]}


class LearningHubServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, handler, *, data_dir: Path, question_bank: Path, library_root: Path, student_id: str):
        super().__init__(address, handler)
        self.data_dir = data_dir
        self.db_path = require_initialized(data_dir)
        self.question_bank = question_bank
        self.library_root = library_root
        self.student_id = student_id


class LearningHubHandler(BaseHTTPRequestHandler):
    server: LearningHubServer
    server_version = "EnglishLearningHub/1.0"

    def log_message(self, fmt: str, *args) -> None:
        message = f"{self.log_date_time_string()} {self.address_string()} {fmt % args}\n"
        log_path = self.server.data_dir / "logs" / "learning_hub_access.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(message)

    def _send_json(self, value, status: int = 200) -> None:
        payload = _json_bytes(value)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(payload)

    def _error(self, status: int, message: str) -> None:
        self._send_json({"status": "error", "error": message}, status)

    def _body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("Invalid Content-Length") from exc
        if length <= 0 or length > 2_000_000:
            raise ValueError("JSON body is required and must be smaller than 2 MB")
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("JSON body must be an object")
        return payload

    def _conn(self):
        return connect(self.server.db_path)

    def _static(self, path: str) -> None:
        name = "index.html" if path in {"/", "/index.html"} else path.removeprefix("/assets/")
        if name not in {"index.html", "app.js", "styles.css"}:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        package = resources.files("english_tracker.web")
        target = package / name
        if not target.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        payload = target.read_bytes()
        content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
        if name.endswith((".html", ".css", ".js")):
            content_type += "; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'")
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        query = parse_qs(parsed.query)
        if not path.startswith("/api/"):
            self._static(path)
            return
        conn = None
        try:
            conn = self._conn()
            if path == "/api/health":
                integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
                self._send_json(
                    {
                        "status": "ok" if integrity == "ok" else "attention",
                        "database": str(self.server.db_path),
                        "question_bank": str(self.server.question_bank),
                        "library_root": str(self.server.library_root),
                        "integrity_check": integrity,
                        "generated_at": utc_now(),
                    }
                )
            elif path == "/api/overview":
                self._send_json(
                    overview(conn, student_id=self.server.student_id, question_bank=self.server.question_bank)
                )
            elif path == "/api/home":
                self._send_json(low_friction_summary(conn, self.server.student_id))
            elif path == "/api/question-bank":
                self._send_json(question_bank_summary(self.server.question_bank))
            elif path == "/api/questions":
                self._send_json(
                    search_questions(
                        self.server.question_bank,
                        query=query.get("q", [""])[0],
                        question_type=query.get("type", [""])[0],
                        verification_status=query.get("status", [""])[0],
                        limit=int(query.get("limit", ["50"])[0]),
                    )
                )
            elif path.startswith("/api/questions/"):
                self._send_json(question_detail(self.server.question_bank, path.rsplit("/", 1)[-1], conn))
            elif path == "/api/mastery":
                self._send_json(weighted_mastery_report(conn, self.server.student_id))
            elif path == "/api/weights":
                self._send_json(weight_policy_report(conn))
            elif path == "/api/assessments":
                rows = [
                    dict(row)
                    for row in conn.execute(
                        """
                        SELECT ls.session_id,ls.title,ls.started_at,sa.*,
                               awp.evidence_weight,awp.is_calibration_anchor
                        FROM learning_sessions ls
                        JOIN session_assessments sa ON sa.session_id=ls.session_id
                        LEFT JOIN assessment_weight_policies awp
                          ON awp.assessment_kind=sa.assessment_kind
                         AND awp.delivery_mode=sa.delivery_mode
                         AND awp.policy_version='evidence-v1'
                        WHERE ls.student_id=? AND ls.record_status='active'
                        ORDER BY ls.started_at DESC
                        """,
                        (self.server.student_id,),
                    )
                ]
                self._send_json({"count": len(rows), "items": rows})
            elif path == "/api/performance/sessions":
                self._send_json(
                    session_performance(
                        conn,
                        self.server.student_id,
                        domain=query.get("domain", [None])[0],
                        limit=int(query.get("limit", ["100"])[0]),
                    )
                )
            elif path == "/api/reading/error-types":
                self._send_json({"items": reading_error_taxonomy(conn)})
            elif path.startswith("/api/reading/passages/") and path.endswith("/performance"):
                passage_id = path.removeprefix("/api/reading/passages/").removesuffix("/performance").strip("/")
                if not passage_id:
                    raise ValueError("passage_id is required")
                self._send_json(
                    reading_passage_performance(
                        conn,
                        self.server.question_bank,
                        self.server.student_id,
                        passage_id,
                        session_id=query.get("session_id", [None])[0],
                        similar_limit=int(query.get("similar_limit", ["12"])[0]),
                    )
                )
            elif path == "/api/library":
                self._send_json(library_summary(conn))
            elif path == "/api/library/resources":
                self._send_json(
                    {
                        "items": recent_resources(
                            conn,
                            status=query.get("status", [None])[0],
                            limit=int(query.get("limit", ["100"])[0]),
                        )
                    }
                )
            elif path == "/api/library/structure":
                self._send_json(structure_summary(conn))
            elif path == "/api/library/candidates":
                self._send_json(
                    search_staged_questions(
                        conn,
                        query=query.get("q", [""])[0],
                        question_type=query.get("type", [""])[0],
                        verification_status=query.get("status", [""])[0],
                        limit=int(query.get("limit", ["50"])[0]),
                    )
                )
            elif path.startswith("/api/library/candidates/"):
                self._send_json(staged_question_detail(conn, path.rsplit("/", 1)[-1]))
            elif path == "/api/library/search":
                self._send_json(
                    search_library_chunks(
                        conn,
                        query.get("q", [""])[0],
                        limit=int(query.get("limit", ["30"])[0]),
                    )
                )
            elif path == "/api/workflow":
                self._send_json(workflow_summary(conn))
            elif path.startswith("/api/context/"):
                audience = path.rsplit("/", 1)[-1]
                self._send_json(
                    context_for(conn, audience, student_id=self.server.student_id, question_bank=self.server.question_bank)
                )
            elif path == "/api/knowledge/search":
                self._send_json(search_knowledge(conn, query.get("q", [""])[0], limit=int(query.get("limit", ["30"])[0])))
            elif path.startswith("/api/grammar/questions/"):
                self._send_json(question_knowledge(conn, path.rsplit("/", 1)[-1]))
            elif path.startswith("/api/grammar/passages/") and path.endswith("/coverage"):
                passage_id = path.removeprefix("/api/grammar/passages/").removesuffix("/coverage").strip("/")
                if not passage_id:
                    raise ValueError("passage_id is required")
                self._send_json(passage_coverage(conn, passage_id))
            elif path == "/api/grammar/coverage-matrix":
                passage_ids = query.get("passage_id", [])
                minimum = int(query.get("minimum", ["2"])[0])
                self._send_json(coverage_matrix(conn, passage_ids, minimum_confirmed_questions=minimum))
            elif path == "/api/reports/weekly":
                self._send_json(weekly_report(conn, self.server.student_id, week_start=query.get("week_start", [None])[0]))
            elif path == "/api/reports/trends":
                today = datetime.now().astimezone().date()
                start = query.get("start", [(today - timedelta(days=84)).isoformat()])[0]
                end = query.get("end", [today.isoformat()])[0]
                self._send_json(trend_report(conn, self.server.student_id, start=start, end=end))
            elif path == "/api/contracts/dictation-ocr":
                self._send_json(
                    {
                        "contract_version": "1.0",
                        "purpose": "OCR/API只负责产生原始识别文本；本地站点负责确定性批改、保存作答与安排复测。",
                        "input_boundary": "图片二进制不写入学习事实表，由外部OCR生产者保管或另存审计快照。",
                        "submission_endpoint": "/api/dictation/results",
                        "submission_body": {
                            "title": "本次听写名称（可选）",
                            "date": "YYYY-MM-DD（可选）",
                            "delivery_mode": "offline_closed|offline_open|online|home",
                            "items": [{"item_id": "词汇条目ID", "student_answer": "OCR原始识别答案，不要先纠正"}],
                        },
                        "rules": [
                            "必须逐项保留OCR原始答案；空白提交空字符串。",
                            "OCR不得根据标准答案改写识别文本。",
                            "网站使用本地精确匹配批改，不调用模型。",
                            "无法可靠识别时不提交猜测，转人工确认。",
                        ],
                    }
                )
            elif path == "/api/dictation/plan":
                limit = int(query.get("limit", ["20"])[0])
                due = due_reviews(conn, self.server.student_id, domain="vocabulary", limit=limit)
                self._send_json({"generated_at": utc_now(), "plan_size": due["count"], "items": due["items"]})
            else:
                self._error(404, "API endpoint not found")
        except (ValueError, KeyError, sqlite3.Error) as exc:
            self._error(400, str(exc))
        except Exception as exc:  # pragma: no cover - defensive local server boundary
            self._error(500, str(exc))
        finally:
            if conn is not None:
                conn.close()

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        conn = None
        try:
            body = self._body()
            conn = self._conn()
            if path == "/api/assessments":
                session_id = random_id("SES")
                event_id = random_id("EVT")
                started_at = _iso_from_date(body.get("date") or body.get("started_at"))
                raw_score = float(body["raw_score"]) if body.get("raw_score") not in {None, ""} else None
                max_score = float(body["max_score"]) if body.get("max_score") not in {None, ""} else None
                payload = {
                    "event_id": event_id,
                    "idempotency_key": f"web:assessment:{session_id}:v1",
                    "source_thread": "manual",
                    "student_id": self.server.student_id,
                    "session": {
                        "session_id": session_id,
                        "session_type": body.get("assessment_kind", "topic_quiz"),
                        "title": body.get("title") or "线下测试",
                        "started_at": started_at,
                        "ended_at": body.get("ended_at"),
                        "timezone": "Asia/Shanghai",
                    },
                    "assessment": {
                        "assessment_kind": body.get("assessment_kind", "topic_quiz"),
                        "reporting_series": body.get("reporting_series") or body.get("assessment_kind", "topic_quiz"),
                        "delivery_mode": body.get("delivery_mode", "offline_closed"),
                        "raw_score": raw_score,
                        "max_score": max_score,
                        "duration_seconds": int(body["duration_seconds"]) if body.get("duration_seconds") not in {None, ""} else None,
                        "blank_count": int(body["blank_count"]) if body.get("blank_count") not in {None, ""} else None,
                        "validation_status": "verified",
                    },
                }
                backup = create_backup(self.server.db_path, self.server.data_dir / "backups", "web-assessment")
                result = import_session(conn, payload, backup_path=str(backup) if backup else None)
                self._send_json({"status": "created", "session_id": session_id, "result": result}, 201)
            elif path == "/api/grammar/select-passages":
                target_codes = body.get("target_codes")
                if not isinstance(target_codes, list) or not target_codes:
                    raise ValueError("target_codes must be a non-empty array")
                result = weighted_set_cover(
                    conn,
                    target_codes,
                    student_id=body.get("student_id", self.server.student_id),
                    recent_error_days=int(body.get("recent_error_days", 30)),
                    max_passages=int(body.get("max_passages", 5)),
                    as_of=body.get("as_of"),
                )
                self._send_json(result)
            elif path == "/api/classroom/attempts":
                payload = dict(body)
                payload.setdefault("source_thread", "courseware")
                payload.setdefault("student_id", self.server.student_id)
                backup = create_backup(self.server.db_path, self.server.data_dir / "backups", "web-classroom-attempts")
                result = import_attempts(conn, payload, backup_path=str(backup) if backup else None)
                self._send_json({"status": "created", "result": result}, 201)
            elif path == "/api/reading/diagnostics":
                payload = dict(body)
                payload.setdefault("event_id", random_id("EVT"))
                payload.setdefault("idempotency_key", f"web:reading-diagnostics:{payload['event_id']}:v1")
                payload.setdefault("source_thread", "courseware")
                payload.setdefault("student_id", self.server.student_id)
                backup = create_backup(self.server.db_path, self.server.data_dir / "backups", "web-reading-diagnostics")
                result = import_attempt_diagnostics(conn, payload, backup_path=str(backup) if backup else None)
                self._send_json({"status": "created", "result": result}, 201)
            elif path == "/api/dictation/results":
                items = body.get("items")
                if not isinstance(items, list) or not items:
                    raise ValueError("items must be a non-empty array")
                session_id = random_id("SES")
                started_at = _iso_from_date(body.get("date") or body.get("started_at"))
                session_event = random_id("EVT")
                correct = 0
                attempts = []
                for item in items:
                    item_id = item.get("item_id")
                    if not item_id:
                        raise ValueError("Every dictation item requires item_id")
                    source = conn.execute("SELECT answer_snapshot FROM content_items WHERE item_id=? AND record_status='active'", (item_id,)).fetchone()
                    if not source:
                        raise ValueError(f"Unknown active content item: {item_id}")
                    answer = item.get("student_answer")
                    answer_text = "" if answer is None else str(answer)
                    standard = source["answer_snapshot"] or ""
                    is_correct = _answer_equal(answer_text, standard)
                    correct += is_correct
                    attempts.append(
                        {
                            "event_id": random_id("ATT"),
                            "item_id": item_id,
                            "attempted_at": started_at,
                            "student_answer": answer_text,
                            "standard_answer": standard,
                            "answer_capture_status": "captured_blank" if answer_text == "" else "captured",
                            "attempt_phase": "review",
                            "response_mode": "active_recall",
                            "validation_status": "verified",
                            "evaluation": {
                                "result": "correct" if is_correct else "wrong",
                                "score": 1 if is_correct else 0,
                                "max_score": 1,
                                "evaluated_by": "local_exact_match",
                            },
                            "error_types": [],
                        }
                    )
                session_payload = {
                    "event_id": session_event,
                    "idempotency_key": f"web:dictation:{session_id}:session:v1",
                    "source_thread": "dictation",
                    "student_id": self.server.student_id,
                    "session": {
                        "session_id": session_id,
                        "session_type": "dictation",
                        "title": body.get("title") or "本地听写",
                        "started_at": started_at,
                        "timezone": "Asia/Shanghai",
                    },
                    "assessment": {
                        "assessment_kind": "dictation",
                        "reporting_series": body.get("reporting_series") or "weekly_dictation",
                        "delivery_mode": body.get("delivery_mode", "offline_closed"),
                        "raw_score": correct,
                        "max_score": len(attempts),
                        "blank_count": sum(attempt["answer_capture_status"] == "captured_blank" for attempt in attempts),
                        "validation_status": "verified",
                    },
                }
                attempts_event = random_id("EVT")
                attempts_payload = {
                    "event_id": attempts_event,
                    "idempotency_key": f"web:dictation:{session_id}:attempts:v1",
                    "source_thread": "dictation",
                    "student_id": self.server.student_id,
                    "session_id": session_id,
                    "attempts": attempts,
                }
                backup = create_backup(self.server.db_path, self.server.data_dir / "backups", "web-dictation")
                session_result = import_session(conn, session_payload, backup_path=str(backup) if backup else None)
                attempts_result = import_attempts(conn, attempts_payload, backup_path=str(backup) if backup else None)
                self._send_json(
                    {
                        "status": "created",
                        "session_id": session_id,
                        "correct": correct,
                        "total": len(attempts),
                        "session_result": session_result,
                        "attempts_result": attempts_result,
                    },
                    201,
                )
            else:
                self._error(404, "API endpoint not found")
        except (ValueError, KeyError, sqlite3.Error, json.JSONDecodeError) as exc:
            self._error(400, str(exc))
        except Exception as exc:  # pragma: no cover
            self._error(500, str(exc))
        finally:
            if conn is not None:
                conn.close()


def serve(
    data_dir: str | Path,
    *,
    question_bank: str | Path,
    library_root: str | Path,
    student_id: str = "STU-001",
    host: str = "127.0.0.1",
    port: int = 8788,
    open_browser: bool = False,
) -> None:
    data_path = Path(data_dir).expanduser().resolve()
    question_bank_path = Path(question_bank).expanduser().resolve()
    library_path = Path(library_root).expanduser().resolve()
    if not question_bank_path.exists():
        raise ValueError(f"Question bank not found: {question_bank_path}")
    if not library_path.is_dir():
        raise ValueError(f"Library root not found: {library_path}")
    server = LearningHubServer(
        (host, port),
        LearningHubHandler,
        data_dir=data_path,
        question_bank=question_bank_path,
        library_root=library_path,
        student_id=student_id,
    )
    with connect(server.db_path) as conn:
        conn.execute(
            """
            UPDATE project_work_items
            SET status='completed',completed_units=1,total_units=1,
                evidence_path='http://127.0.0.1:8788',blocker=NULL,updated_at=?
            WHERE work_item_id='WORK-LOCAL-HUB'
            """,
            (utc_now(),),
        )
        conn.execute(
            """
            UPDATE project_work_items
            SET status='completed',completed_units=1,total_units=1,
                evidence_path='/api/contracts/dictation-ocr',blocker=NULL,updated_at=?
            WHERE work_item_id='WORK-OCR-INTEGRATION'
            """,
            (utc_now(),),
        )
    url = f"http://{host}:{port}"
    print(json.dumps({"status": "serving", "url": url, "database": str(database_path(data_path))}, ensure_ascii=False))
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def configured_question_bank(explicit: str | None) -> Path:
    raw = explicit or os.environ.get(QUESTION_BANK_ENV)
    if not raw:
        raise ValueError(f"Set {QUESTION_BANK_ENV} or pass --question-bank")
    return Path(raw).expanduser().resolve()


def configured_library_root(explicit: str | None) -> Path:
    raw = explicit or os.environ.get(LIBRARY_ROOT_ENV)
    if not raw:
        raise ValueError(f"Set {LIBRARY_ROOT_ENV} or pass --library-root")
    return Path(raw).expanduser().resolve()
