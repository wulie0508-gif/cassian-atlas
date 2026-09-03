from __future__ import annotations

import json
import mimetypes
import os
import sqlite3
import webbrowser
from datetime import datetime, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from . import __version__
from .analytics import due_reviews
from .backup import create_backup
from .dashboard import (
    context_for,
    low_friction_summary,
    overview,
    question_bank_summary,
    question_detail,
    search_questions,
    teacher_dashboard,
    workflow_summary,
)
from .db import connect, database_path, migration_status, require_initialized
from .enrichment import search_knowledge
from .extraction import (
    ExtractionConflict,
    commit_extraction_batch,
    create_extraction_batch,
    extraction_batch_detail,
    extraction_review,
    submit_human_decisions,
    submit_provider_results,
)
from .generation import generation_detail, list_generations, start_generation, update_generation
from .grammar_catalog import coverage_matrix, passage_coverage, question_knowledge
from .ingest import IngestConflict, import_attempts, import_session
from .library import library_summary, recent_resources
from .metrics import trend_report, weekly_report
from .orchestration import (
    agent_dashboard,
    append_run_event,
    capability_manifest,
    list_runs,
    plan_route,
    register_run,
)
from .question_pipeline import (
    search_library_chunks,
    search_staged_questions,
    staged_question_detail,
    structure_summary,
)
from .performance import reading_error_taxonomy, reading_passage_performance, session_performance
from .selection import weighted_set_cover
from .util import utc_now
from .weights import weight_policy_report, weighted_mastery_report
from .workflows import record_assessment, record_dictation, record_reading_diagnostics
from .workspace import (
    app_config,
    create_student,
    require_student_enrollment,
    student_summaries,
    subject_overview,
)


QUESTION_BANK_ENV = "ENGLISH_TRACKER_QUESTION_BANK"
LIBRARY_ROOT_ENV = "ENGLISH_TRACKER_LIBRARY_ROOT"


def _json_bytes(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str).encode("utf-8")


class LearningHubServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address,
        handler,
        *,
        data_dir: Path,
        question_bank: Path,
        library_root: Path,
        student_id: str | None,
    ):
        super().__init__(address, handler)
        self.data_dir = data_dir
        self.db_path = require_initialized(data_dir)
        self.question_bank = question_bank
        self.library_root = library_root
        self.student_id = student_id


class LearningHubHandler(BaseHTTPRequestHandler):
    server: LearningHubServer
    server_version = "CassianAtlasDashboard/1.0"

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

    def _conn(self, *, readonly: bool = False):
        return connect(self.server.db_path, readonly=readonly)

    def _selected_student(
        self,
        conn,
        *,
        query: dict | None = None,
        body: dict | None = None,
        allow_server_fallback: bool = True,
    ) -> str:
        query = query or {}
        body = body or {}
        candidates: list[tuple[str, str]] = []
        if body.get("student_id") is not None and str(body.get("student_id")).strip():
            candidates.append(("JSON body", str(body["student_id"]).strip().upper()))
        header_student = self.headers.get("X-Student-ID")
        if header_student and header_student.strip():
            candidates.append(("X-Student-ID header", header_student.strip().upper()))
        for query_student in query.get("student_id", []):
            if query_student and str(query_student).strip():
                candidates.append(("student_id query parameter", str(query_student).strip().upper()))
        distinct = {value for _, value in candidates}
        if len(distinct) > 1:
            sources = ", ".join(f"{source}={value}" for source, value in candidates)
            raise ValueError(f"Conflicting student_id values: {sources}")
        requested = candidates[0][1] if candidates else None
        if not requested and allow_server_fallback and self.server.student_id:
            requested = str(self.server.student_id).strip().upper()
        if not requested:
            raise ValueError("student_id is required for learner-specific operations")
        student_id = str(requested).strip().upper()
        if not conn.execute(
            "SELECT 1 FROM students WHERE student_id=? AND active=1",
            (student_id,),
        ).fetchone():
            raise ValueError(f"Unknown or inactive student_id: {student_id}")
        return student_id

    def _static(self, path: str) -> None:
        name = "index.html" if path in {"/", "/index.html"} else path.removeprefix("/assets/")
        if name not in {"index.html", "app.js", "i18n.js", "styles.css", "favicon.svg"}:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        package = resources.files("english_tracker.web")
        target = package / name
        if not target.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        payload = target.read_bytes()
        content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
        if name.endswith((".html", ".css", ".js", ".svg")):
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
            conn = self._conn(readonly=True)
            public_endpoints = {
                "/api/health",
                "/api/app-config",
                "/api/students",
                "/api/agent/capabilities",
            }
            student_id = (
                None
                if path in public_endpoints
                else self._selected_student(
                    conn,
                    query=query,
                    allow_server_fallback=not (
                        path.startswith("/api/generations")
                        or path.startswith("/api/extraction/batches")
                        or path == "/api/teacher/dashboard"
                    ),
                )
            )
            if path == "/api/health":
                # Health checks run frequently and must stay cheap even when the
                # learning database contains a large local source index.  The
                # exhaustive integrity scan remains available through
                # `cassian data check` and pre-write backups.
                database_probe = (
                    "ok" if conn.execute("SELECT 1").fetchone()[0] == 1 else "attention"
                )
                student_count = conn.execute(
                    "SELECT COUNT(*) FROM students WHERE active=1"
                ).fetchone()[0]
                schema_table_exists = bool(
                    conn.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
                    ).fetchone()
                )
                applied_versions = (
                    [
                        row[0]
                        for row in conn.execute(
                            "SELECT version FROM schema_migrations ORDER BY version"
                        )
                    ]
                    if schema_table_exists
                    else []
                )
                migration = migration_status(conn)
                pending_versions = list(migration["pending_versions"])
                current_version = applied_versions[-1] if applied_versions else None
                latest_packaged_version = (
                    sorted(set(applied_versions + pending_versions))[-1]
                    if applied_versions or pending_versions
                    else None
                )
                self._send_json(
                    {
                        "status": (
                            "ok"
                            if database_probe == "ok" and migration["status"] == "ready"
                            else "attention"
                        ),
                        "product": "Cassian Atlas",
                        "app_version": __version__,
                        "process_id": os.getpid(),
                        "control_plane": "cli",
                        "dashboard_mode": "read_only",
                        "frontend_mode": "read_only",
                        "agent_api_mode": "write_enabled",
                        "interfaces": {
                            "frontend": "read_only",
                            "agent_api": "write_enabled",
                        },
                        "schema": {
                            "status": migration["status"],
                            "current_version": current_version,
                            "latest_packaged_version": latest_packaged_version,
                            "applied_versions": applied_versions,
                            "pending_versions": pending_versions,
                            "migration_required": bool(pending_versions),
                            "checksum_mismatches": migration["checksum_mismatches"],
                            "unknown_applied_versions": migration["unknown_applied_versions"],
                        },
                        "active_student_count": int(student_count),
                        "database": str(self.server.db_path),
                        "question_bank": str(self.server.question_bank),
                        "library_root": str(self.server.library_root),
                        "database_probe": database_probe,
                        "database_probe_mode": "SELECT 1",
                        "integrity_check": "deferred",
                        "integrity_check_mode": "cassian data check",
                        "generated_at": utc_now(),
                    }
                )
            elif path == "/api/app-config":
                config = app_config(conn)
                config["operating_mode"] = {
                    "control_plane": "cli",
                    "dashboard": "read_only",
                    "user_entry": "Codex conversation",
                }
                self._send_json(config)
            elif path == "/api/students":
                self._send_json(student_summaries(conn))
            elif path == "/api/subject-overview":
                self._send_json(
                    subject_overview(
                        conn,
                        student_id,
                        query.get("subject_code", ["english"])[0],
                    )
                )
            elif path == "/api/overview":
                self._send_json(
                    overview(conn, student_id=student_id, question_bank=self.server.question_bank)
                )
            elif path == "/api/home":
                self._send_json(low_friction_summary(conn, student_id))
            elif path == "/api/teacher/dashboard":
                self._send_json(
                    teacher_dashboard(
                        conn,
                        student_id,
                        subject_code=query.get("subject_code", ["english"])[0],
                        as_of=query.get("as_of", [None])[0],
                    )
                )
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
                self._send_json(weighted_mastery_report(conn, student_id))
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
                        (student_id,),
                    )
                ]
                self._send_json({"count": len(rows), "items": rows})
            elif path == "/api/performance/sessions":
                self._send_json(
                    session_performance(
                        conn,
                        student_id,
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
                        student_id,
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
                self._send_json(workflow_summary(conn, student_id=student_id))
            elif path == "/api/agent/capabilities":
                self._send_json(capability_manifest())
            elif path == "/api/agent/dashboard":
                self._send_json(
                    agent_dashboard(
                        conn,
                        student_id=student_id,
                        limit=int(query.get("limit", ["12"])[0]),
                    )
                )
            elif path == "/api/agent/runs":
                self._send_json(
                    list_runs(
                        conn,
                        student_id=student_id,
                        status=query.get("status", [None])[0],
                        limit=int(query.get("limit", ["30"])[0]),
                    )
                )
            elif path.startswith("/api/extraction/batches/") and path.endswith("/review"):
                batch_id = (
                    path.removeprefix("/api/extraction/batches/")
                    .removesuffix("/review")
                    .strip("/")
                )
                if not batch_id or "/" in batch_id:
                    raise ValueError("batch_id is required")
                self._send_json(
                    extraction_review(
                        conn,
                        batch_id,
                        student_id=student_id,
                    )
                )
            elif path.startswith("/api/extraction/batches/"):
                batch_id = path.removeprefix("/api/extraction/batches/").strip("/")
                if not batch_id or "/" in batch_id:
                    raise ValueError("batch_id is required")
                self._send_json(
                    extraction_batch_detail(
                        conn,
                        batch_id,
                        student_id=student_id,
                    )
                )
            elif path == "/api/generations":
                self._send_json(
                    list_generations(
                        conn,
                        student_id=student_id,
                        limit=int(query.get("limit", ["30"])[0]),
                    )
                )
            elif path.startswith("/api/generations/"):
                generation_id = path.removeprefix("/api/generations/").strip("/")
                if not generation_id or "/" in generation_id:
                    raise ValueError("generation_id is required")
                self._send_json(
                    generation_detail(conn, generation_id, student_id=student_id)
                )
            elif path.startswith("/api/context/"):
                audience = path.rsplit("/", 1)[-1]
                self._send_json(
                    context_for(conn, audience, student_id=student_id, question_bank=self.server.question_bank)
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
                self._send_json(weekly_report(conn, student_id, week_start=query.get("week_start", [None])[0]))
            elif path == "/api/reports/trends":
                today = datetime.now().astimezone().date()
                start = query.get("start", [(today - timedelta(days=84)).isoformat()])[0]
                end = query.get("end", [today.isoformat()])[0]
                self._send_json(trend_report(conn, student_id, start=start, end=end))
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
                due = due_reviews(conn, student_id, domain="vocabulary", limit=limit)
                self._send_json({"generated_at": utc_now(), "plan_size": due["count"], "items": due["items"]})
            else:
                self._error(404, "API endpoint not found")
        except (
            ValueError,
            KeyError,
            IngestConflict,
            ExtractionConflict,
            sqlite3.Error,
        ) as exc:
            self._error(400, str(exc))
        except Exception as exc:  # pragma: no cover - defensive local server boundary
            self._error(500, str(exc))
        finally:
            if conn is not None:
                conn.close()

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        query = parse_qs(parsed.query)
        conn = None
        try:
            body = self._body()
            conn = self._conn()
            if path == "/api/students":
                backup = create_backup(self.server.db_path, self.server.data_dir / "backups", "create-student")
                result = create_student(conn, body)
                self._send_json({"status": "created", "student": result, "backup": str(backup) if backup else None}, 201)
                return
            student_id = self._selected_student(
                conn,
                query=query,
                body=body,
                allow_server_fallback=False,
            )
            if path == "/api/sessions":
                payload = dict(body)
                payload["student_id"] = student_id
                backup = create_backup(self.server.db_path, self.server.data_dir / "backups", "web-session")
                self._send_json(
                    {
                        "result": import_session(conn, payload, backup_path=str(backup) if backup else None),
                        "backup": str(backup) if backup else None,
                    },
                    201,
                )
            elif path == "/api/extraction/batches":
                payload = dict(body)
                payload["student_id"] = student_id
                backup = create_backup(
                    self.server.db_path,
                    self.server.data_dir / "backups",
                    "web-extraction-create",
                )
                result = create_extraction_batch(conn, payload)
                self._send_json(
                    {
                        "result": result,
                        "backup": str(backup) if backup else None,
                    },
                    201 if result.get("status") in {"created", "applied"} else 200,
                )
            elif path.startswith("/api/extraction/batches/") and path.endswith("/provider-results"):
                batch_id = (
                    path.removeprefix("/api/extraction/batches/")
                    .removesuffix("/provider-results")
                    .strip("/")
                )
                if not batch_id or "/" in batch_id:
                    raise ValueError("batch_id is required")
                payload = dict(body)
                payload["student_id"] = student_id
                backup = create_backup(
                    self.server.db_path,
                    self.server.data_dir / "backups",
                    "web-extraction-provider-submit",
                )
                result = submit_provider_results(
                    conn,
                    batch_id,
                    payload,
                    student_id=student_id,
                )
                self._send_json(
                    {
                        "result": result,
                        "backup": str(backup) if backup else None,
                    },
                    201 if result.get("status") in {"created", "applied"} else 200,
                )
            elif path.startswith("/api/extraction/batches/") and path.endswith("/decisions"):
                batch_id = (
                    path.removeprefix("/api/extraction/batches/")
                    .removesuffix("/decisions")
                    .strip("/")
                )
                if not batch_id or "/" in batch_id:
                    raise ValueError("batch_id is required")
                payload = dict(body)
                payload["student_id"] = student_id
                backup = create_backup(
                    self.server.db_path,
                    self.server.data_dir / "backups",
                    "web-extraction-decide",
                )
                result = submit_human_decisions(
                    conn,
                    batch_id,
                    payload,
                    student_id=student_id,
                )
                self._send_json(
                    {
                        "result": result,
                        "backup": str(backup) if backup else None,
                    },
                    201 if result.get("status") in {"created", "applied"} else 200,
                )
            elif path.startswith("/api/extraction/batches/") and path.endswith("/commit"):
                batch_id = (
                    path.removeprefix("/api/extraction/batches/")
                    .removesuffix("/commit")
                    .strip("/")
                )
                if not batch_id or "/" in batch_id:
                    raise ValueError("batch_id is required")
                payload = dict(body)
                payload["student_id"] = student_id
                backup = create_backup(
                    self.server.db_path,
                    self.server.data_dir / "backups",
                    "web-extraction-commit",
                )
                result = commit_extraction_batch(
                    conn,
                    batch_id,
                    payload,
                    student_id=student_id,
                    backup_path=str(backup) if backup else None,
                )
                self._send_json(
                    {
                        "result": result,
                        "backup": str(backup) if backup else None,
                    },
                    201 if result.get("status") in {"created", "applied", "committed"} else 200,
                )
            elif path == "/api/agent/route":
                request_text = str(body.get("request_text") or "").strip()
                subject_code = str(body.get("subject_code") or "english").strip().lower()
                student_id, subject_code = require_student_enrollment(
                    conn, student_id, subject_code
                )
                if body.get("register"):
                    payload = dict(body)
                    payload["student_id"] = student_id
                    payload["subject_code"] = subject_code
                    self._send_json(register_run(conn, payload), 201)
                else:
                    self._send_json(
                        plan_route(
                            request_text,
                            student_id=student_id,
                            subject_code=subject_code,
                        )
                    )
            elif path == "/api/agent/runs":
                payload = dict(body)
                payload["student_id"] = student_id
                self._send_json(register_run(conn, payload), 201)
            elif path.startswith("/api/agent/runs/") and path.endswith("/events"):
                run_id = path.removeprefix("/api/agent/runs/").removesuffix("/events").strip("/")
                if not run_id:
                    raise ValueError("run_id is required")
                payload = dict(body)
                payload["student_id"] = student_id
                self._send_json(append_run_event(conn, run_id, payload))
            elif path == "/api/generations":
                payload = dict(body)
                payload["student_id"] = student_id
                backup = create_backup(
                    self.server.db_path,
                    self.server.data_dir / "backups",
                    "generation-start",
                )
                result = start_generation(conn, payload)
                self._send_json(
                    {"result": result, "backup": str(backup) if backup else None},
                    201 if result["status"] == "created" else 200,
                )
            elif path.startswith("/api/generations/"):
                generation_id = path.removeprefix("/api/generations/").strip("/")
                if not generation_id or "/" in generation_id:
                    raise ValueError("generation_id is required")
                payload = dict(body)
                payload.pop("student_id", None)
                backup = create_backup(
                    self.server.db_path,
                    self.server.data_dir / "backups",
                    "generation-update",
                )
                result = update_generation(
                    conn,
                    generation_id,
                    payload,
                    student_id=student_id,
                )
                self._send_json(
                    {"result": result, "backup": str(backup) if backup else None}
                )
            elif path == "/api/assessments":
                payload = dict(body)
                payload["student_id"] = student_id
                backup = create_backup(self.server.db_path, self.server.data_dir / "backups", "web-assessment")
                result = record_assessment(
                    conn,
                    payload,
                    backup_path=str(backup) if backup else None,
                )
                self._send_json(
                    {"result": result, "backup": str(backup) if backup else None},
                    201 if result["status"] == "applied" else 200,
                )
            elif path == "/api/grammar/select-passages":
                target_codes = body.get("target_codes")
                if not isinstance(target_codes, list) or not target_codes:
                    raise ValueError("target_codes must be a non-empty array")
                result = weighted_set_cover(
                    conn,
                    target_codes,
                    student_id=student_id,
                    recent_error_days=int(body.get("recent_error_days", 30)),
                    max_passages=int(body.get("max_passages", 5)),
                    as_of=body.get("as_of"),
                    exclude_passage_ids=body.get("exclude_passage_ids") or [],
                )
                self._send_json(result)
            elif path == "/api/classroom/attempts":
                payload = dict(body)
                payload.setdefault("source_thread", "courseware")
                payload["student_id"] = student_id
                backup = create_backup(self.server.db_path, self.server.data_dir / "backups", "web-classroom-attempts")
                result = import_attempts(conn, payload, backup_path=str(backup) if backup else None)
                self._send_json({"status": "created", "result": result}, 201)
            elif path == "/api/reading/diagnostics":
                payload = dict(body)
                payload["student_id"] = student_id
                backup = create_backup(self.server.db_path, self.server.data_dir / "backups", "web-reading-diagnostics")
                result = record_reading_diagnostics(
                    conn,
                    payload,
                    backup_path=str(backup) if backup else None,
                )
                self._send_json(
                    {"result": result, "backup": str(backup) if backup else None},
                    201 if result["status"] == "applied" else 200,
                )
            elif path == "/api/dictation/results":
                payload = dict(body)
                payload["student_id"] = student_id
                backup = create_backup(self.server.db_path, self.server.data_dir / "backups", "web-dictation")
                result = record_dictation(
                    conn,
                    payload,
                    backup_path=str(backup) if backup else None,
                )
                self._send_json(
                    {"result": result, "backup": str(backup) if backup else None},
                    201 if result["status"] == "created" else 200,
                )
            else:
                self._error(404, "API endpoint not found")
        except (
            ValueError,
            KeyError,
            IngestConflict,
            ExtractionConflict,
            sqlite3.Error,
            json.JSONDecodeError,
        ) as exc:
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
    student_id: str | None = None,
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
