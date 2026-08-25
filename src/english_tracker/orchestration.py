from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import asdict, dataclass
from typing import Any

from .generation import list_generations
from .util import canonical_json, payload_hash, random_id, utc_now
from .workspace import require_student_enrollment


@dataclass(frozen=True)
class Capability:
    key: str
    skill: str
    name_cn: str
    name_en: str
    responsibility: str
    mode: str
    endpoints: tuple[str, ...]


CAPABILITIES = (
    Capability(
        key="platform-engineering",
        skill="manage-learning-system",
        name_cn="学习系统平台工程",
        name_en="Learning platform engineering",
        responsibility="以 opentutor CLI 为控制平面维护安装、配置、迁移、启动和多学生工作区；看板仅作只读状态投影。",
        mode="engineering_write",
        endpoints=("opentutor", "/api/health", "/api/agent/dashboard"),
    ),
    Capability(
        key="evidence-recording",
        skill="record-learning-evidence",
        name_cn="学习证据入库",
        name_en="Evidence recorder",
        responsibility="保存课堂、作业、阅读、语法、测试和校准成绩；通过幂等接口写入，不直接操作 SQLite。",
        mode="write",
        endpoints=("/api/sessions", "/api/classroom/attempts", "/api/assessments"),
    ),
    Capability(
        key="mistake-diagnosis",
        skill="diagnose-learning-mistakes",
        name_cn="错题与错因诊断",
        name_en="Mistake diagnostician",
        responsibility="读取逐题证据，区分题目考点与学生错因；模型结论只保存为 suggested。",
        mode="read_then_suggest",
        endpoints=(
            "/api/reading/passages/{passage_id}/performance",
            "/api/reading/error-types",
            "/api/reading/diagnostics",
        ),
    ),
    Capability(
        key="practice-selection",
        skill="select-learning-practice",
        name_cn="复习与选题算法",
        name_en="Practice selector",
        responsibility="调用到期队列、薄弱点和完整语篇 weighted set-cover，输出最少且覆盖尽量大的练习。",
        mode="read",
        endpoints=("/api/grammar/select-passages", "/api/dictation/plan", "/api/mastery"),
    ),
    Capability(
        key="courseware-context",
        skill="prepare-courseware-context",
        name_cn="课件上下文准备",
        name_en="Courseware context",
        responsibility="为课件 Agent 提供学生证据、题目知识点、教学方法和完整语篇，不在提示词里重算稳定指标。",
        mode="read",
        endpoints=("/api/context/courseware", "/api/questions", "/api/library/search"),
    ),
    Capability(
        key="dictation-workflow",
        skill="run-dictation-workflow",
        name_cn="单词听写工作流",
        name_en="Dictation workflow",
        responsibility="读取到期词、保留 OCR 原始答案、确定性批改、写入成绩并安排复测。",
        mode="read_write",
        endpoints=("/api/context/dictation", "/api/dictation/plan", "/api/dictation/results"),
    ),
    Capability(
        key="dashboard-sync",
        skill="sync-learning-dashboard",
        name_cn="看板状态同步",
        name_en="Dashboard sync",
        responsibility="登记任务、追加进度事件并让看板显示最近运行；不改写学习证据。",
        mode="operational_write",
        endpoints=("/api/agent/runs", "/api/agent/runs/{run_id}/events"),
    ),
)

CAPABILITY_BY_KEY = {item.key: item for item in CAPABILITIES}

_SIGNALS: dict[str, tuple[str, ...]] = {
    "platform-engineering": (
        "codex first", "codex-first", "cli", "命令行", "命令入口", "控制平面",
        "平台工程", "工程改造", "代码改造", "代码升级", "系统升级", "部署",
        "数据库看板", "数据库的看板", "数据库迁移", "数据模型", "多学生", "不同学生", "新增学生",
        "学生空间", "启动器", "看板只读", "launcher", "migration", "schema",
        "control plane", "read-only dashboard",
    ),
    "dictation-workflow": ("听写", "单词", "词汇", "拼写", "ocr", "dictation"),
    "evidence-recording": (
        "成绩", "得分", "满分", "答对", "答错", "学生答", "原始答案", "用时",
        "录入", "入库", "写入", "保存", "课堂结果", "测试结果", "attempt",
    ),
    "mistake-diagnosis": ("错因", "错题", "为什么错", "误选", "诊断", "阅读复盘", "错误原因"),
    "practice-selection": ("选题", "推荐题", "练习", "覆盖", "完整语篇", "复测", "复习计划", "薄弱点", "set-cover", "set cover"),
    "courseware-context": ("课件", "教案", "讲义", "备课", "教学方法", "课堂材料", "ppt", "courseware"),
    "dashboard-sync": ("看板", "进度", "状态", "汇报", "最近运行", "agent 工作流", "agent工作流"),
}

_ORDER = (
    "platform-engineering",
    "dictation-workflow",
    "evidence-recording",
    "mistake-diagnosis",
    "courseware-context",
    "practice-selection",
    "dashboard-sync",
)


def capability_manifest() -> dict[str, Any]:
    return {
        "schema_version": "agent-capabilities-v1",
        "architecture": {
            "router_skill": "route-learning-task",
            "principle": "The router plans and tracks work; specialist skills own execution; the dashboard reads the run ledger.",
            "control_plane": "The opentutor CLI is the control plane; the dashboard is a read-only projection for status and evidence.",
            "database_boundary": "Learning facts use audited domain endpoints. Agent run events never replace learning evidence.",
        },
        "capabilities": [asdict(item) for item in CAPABILITIES],
    }


def _score(text: str, capability_key: str) -> int:
    return sum(2 if " " in signal else 1 for signal in _SIGNALS[capability_key] if signal in text)


def _step(capability_key: str, order: int) -> dict[str, Any]:
    item = CAPABILITY_BY_KEY[capability_key]
    return {
        "order": order,
        "capability_key": item.key,
        "skill": f"${item.skill}",
        "name_cn": item.name_cn,
        "mode": item.mode,
        "endpoints": list(item.endpoints),
    }


def plan_route(request_text: str, *, student_id: str, subject_code: str = "english") -> dict[str, Any]:
    text = re.sub(r"\s+", " ", str(request_text or "").strip().casefold())
    if not text:
        raise ValueError("request_text is required")
    scores = {key: _score(text, key) for key in _SIGNALS}
    selected: list[str] = []

    if scores["platform-engineering"]:
        selected.append("platform-engineering")
    elif scores["dictation-workflow"]:
        selected.append("dictation-workflow")
    else:
        for key in ("evidence-recording", "mistake-diagnosis", "courseware-context", "practice-selection"):
            if scores[key]:
                selected.append(key)

    if not selected:
        selected.append("dashboard-sync" if scores["dashboard-sync"] else "courseware-context")

    if scores["dashboard-sync"] and selected == ["dashboard-sync"]:
        pass
    elif "dashboard-sync" in selected:
        selected.remove("dashboard-sync")

    # Evidence must exist before an attempt-specific diagnosis. Courseware context
    # is gathered before the selector consumes targets and recent error evidence.
    selected = [key for key in _ORDER if key in selected]
    if "courseware-context" in selected and "practice-selection" in selected:
        selected.remove("practice-selection")
        selected.append("practice-selection")

    primary = max(selected, key=lambda key: (scores.get(key, 0), -selected.index(key)))
    confidence_points = scores.get(primary, 0)
    confidence = "high" if confidence_points >= 3 else "medium" if confidence_points >= 1 else "low"
    return {
        "schema_version": "agent-route-v1",
        "student_id": student_id,
        "subject_code": subject_code,
        "intent": primary,
        "primary_capability": primary,
        "confidence": confidence,
        "execution_mode": "single_skill" if len(selected) == 1 else "specialist_pipeline",
        "steps": [_step(key, index) for index, key in enumerate(selected, 1)],
        "dashboard_policy": "Register once, then append started/progress/completed or failed events.",
        "human_attention": "Only request input for missing source facts, ambiguous student identity, or irreversible judgment.",
    }


def _require_student_subject(
    conn: sqlite3.Connection,
    student_id: str,
    subject_code: str,
) -> tuple[str, str]:
    """Use the workspace enrollment boundary for every persisted Agent run."""
    return require_student_enrollment(conn, student_id, subject_code)


def register_run(conn: sqlite3.Connection, payload: dict[str, Any]) -> dict[str, Any]:
    request_text = str(payload.get("request_text") or "").strip()
    student_id = str(payload.get("student_id") or "").strip()
    subject_code = str(payload.get("subject_code") or "english").strip()
    source_thread = str(payload.get("source_thread") or "orchestrator").strip()
    if not request_text or not student_id:
        raise ValueError("request_text and student_id are required")
    student_id, subject_code = _require_student_subject(conn, student_id, subject_code)
    route = plan_route(request_text, student_id=student_id, subject_code=subject_code)
    request_digest = payload_hash({"request_text": request_text, "student_id": student_id, "subject_code": subject_code})
    idempotency_key = str(payload.get("idempotency_key") or random_id("AGENT-IDEM")).strip()
    existing = conn.execute("SELECT * FROM agent_runs WHERE idempotency_key=?", (idempotency_key,)).fetchone()
    if existing:
        if existing["request_hash"] != request_digest:
            raise ValueError("idempotency_key already belongs to a different agent request")
        return {"status": "duplicate", "run": _run_detail(conn, existing["run_id"])}

    now = utc_now()
    run_id = str(payload.get("run_id") or random_id("RUN")).strip()
    title = str(payload.get("title") or request_text[:80]).strip()[:120]
    with conn:
        conn.execute(
            """
            INSERT INTO agent_runs(
              run_id,idempotency_key,student_id,subject_code,source_thread,request_hash,
              request_excerpt,intent,primary_capability,route_json,title,status,created_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                run_id, idempotency_key, student_id, subject_code, source_thread, request_digest,
                request_text[:500], route["intent"], route["primary_capability"], canonical_json(route),
                title, "planned", now, now,
            ),
        )
        conn.execute(
            """
            INSERT INTO agent_run_events(
              event_id,run_id,sequence_no,event_type,capability_key,actor,message,payload_json,created_at
            ) VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (
                random_id("RUNEVT"), run_id, 1, "planned", route["primary_capability"],
                source_thread, "Task routed to specialist capability.", canonical_json({"route": route}), now,
            ),
        )
    return {"status": "created", "run": _run_detail(conn, run_id)}


def append_run_event(conn: sqlite3.Connection, run_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    student_id = str(payload.get("student_id") or "").strip().upper()
    if not student_id:
        raise ValueError("student_id is required for agent run events")
    row = conn.execute("SELECT * FROM agent_runs WHERE run_id=?", (run_id,)).fetchone()
    if not row:
        raise ValueError(f"Unknown run_id: {run_id}")
    if student_id != row["student_id"]:
        raise ValueError(
            f"Run {run_id} belongs to student {row['student_id']}, not {student_id}"
        )
    _require_student_subject(conn, row["student_id"], row["subject_code"])

    idempotency_key = str(payload.get("idempotency_key") or "").strip() or None
    event_request = {key: value for key, value in payload.items() if key != "idempotency_key"}
    event_request_hash = payload_hash(event_request)
    if idempotency_key:
        existing = conn.execute(
            "SELECT run_id,payload_json FROM agent_run_events WHERE idempotency_key=?",
            (idempotency_key,),
        ).fetchone()
        if existing:
            try:
                stored = json.loads(existing["payload_json"] or "null")
            except json.JSONDecodeError:
                stored = None
            if (
                existing["run_id"] != run_id
                or not isinstance(stored, dict)
                or stored.get("_request_hash") != event_request_hash
            ):
                raise ValueError("idempotency_key already belongs to a different run event")
            return {"status": "duplicate", "run": _run_detail(conn, run_id)}

    terminal_statuses = {"completed", "failed", "cancelled"}
    if row["status"] in terminal_statuses:
        raise ValueError(
            f"Run {run_id} is already terminal ({row['status']}) and cannot accept new events"
        )
    event_type = str(payload.get("event_type") or "").strip()
    if not event_type:
        raise ValueError("event_type is required")
    allowed = {"started", "progress", "needs_input", "completed", "failed", "cancelled"}
    if event_type not in allowed:
        raise ValueError(f"event_type must be one of: {', '.join(sorted(allowed))}")
    status = "in_progress" if event_type in {"started", "progress"} else event_type
    message = str(payload.get("message") or event_type.replace("_", " ")).strip()[:1000]
    actor = str(payload.get("actor") or "specialist").strip()[:80]
    capability_key = str(payload.get("capability_key") or row["primary_capability"]).strip()
    if capability_key not in CAPABILITY_BY_KEY:
        raise ValueError(f"Unknown capability_key: {capability_key}")
    now = utc_now()
    sequence = conn.execute(
        "SELECT COALESCE(MAX(sequence_no),0)+1 FROM agent_run_events WHERE run_id=?",
        (run_id,),
    ).fetchone()[0]
    started_at = row["started_at"] or (now if status == "in_progress" else None)
    completed_at = now if status in {"completed", "failed", "cancelled"} else None
    summary = str(payload.get("summary") or row["summary"] or "").strip()[:2000] or None
    result_ref = str(payload.get("result_ref") or row["result_ref"] or "").strip()[:500] or None
    details = payload.get("details")
    event_payload = (
        {
            "_idempotency_key": idempotency_key,
            "_request_hash": event_request_hash,
            "details": details,
        }
        if idempotency_key
        else details
    )
    with conn:
        conn.execute(
            """
            INSERT INTO agent_run_events(
              event_id,run_id,sequence_no,event_type,capability_key,actor,message,payload_json,
              idempotency_key,created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            (
                random_id("RUNEVT"), run_id, sequence, event_type, capability_key, actor,
                message, canonical_json(event_payload) if event_payload is not None else None,
                idempotency_key, now,
            ),
        )
        conn.execute(
            """
            UPDATE agent_runs SET status=?,summary=?,result_ref=?,started_at=?,updated_at=?,completed_at=?
            WHERE run_id=?
            """,
            (status, summary, result_ref, started_at, now, completed_at, run_id),
        )
    return {"status": "updated", "run": _run_detail(conn, run_id)}


def _run_detail(conn: sqlite3.Connection, run_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM agent_runs WHERE run_id=?", (run_id,)).fetchone()
    if not row:
        raise ValueError(f"Unknown run_id: {run_id}")
    result = dict(row)
    result["route"] = json.loads(result.pop("route_json"))
    result.pop("request_hash", None)
    result["events"] = [
        dict(item)
        for item in conn.execute(
            """
            SELECT event_id,sequence_no,event_type,capability_key,actor,message,
                   idempotency_key,created_at
            FROM agent_run_events WHERE run_id=? ORDER BY sequence_no
            """,
            (run_id,),
        )
    ]
    return result


def run_detail(
    conn: sqlite3.Connection,
    run_id: str,
    *,
    student_id: str,
) -> dict[str, Any]:
    """Return one run only when it belongs to the explicitly selected learner."""
    row = conn.execute(
        "SELECT student_id,subject_code FROM agent_runs WHERE run_id=? AND student_id=?",
        (run_id, student_id),
    ).fetchone()
    if not row:
        raise ValueError(f"Unknown run_id for student {student_id}: {run_id}")
    _require_student_subject(conn, row["student_id"], row["subject_code"])
    return _run_detail(conn, run_id)


def list_runs(
    conn: sqlite3.Connection,
    *,
    student_id: str,
    status: str | None = None,
    limit: int = 30,
) -> dict[str, Any]:
    clauses = ["student_id=?"]
    params: list[Any] = [student_id]
    if status:
        clauses.append("status=?")
        params.append(status)
    params.append(max(1, min(int(limit), 100)))
    rows = [
        dict(row)
        for row in conn.execute(
            f"""
            SELECT run_id,student_id,subject_code,source_thread,intent,primary_capability,
                   title,status,summary,result_ref,created_at,started_at,updated_at,completed_at
            FROM agent_runs WHERE {' AND '.join(clauses)}
            ORDER BY updated_at DESC,run_id DESC LIMIT ?
            """,
            params,
        )
    ]
    return {"count": len(rows), "items": rows}


def agent_dashboard(conn: sqlite3.Connection, *, student_id: str, limit: int = 12) -> dict[str, Any]:
    recent = list_runs(conn, student_id=student_id, limit=limit)
    recent_generations = list_generations(conn, student_id=student_id, limit=limit)
    counts = {
        row["status"]: row["count"]
        for row in conn.execute(
            "SELECT status,COUNT(*) count FROM agent_runs WHERE student_id=? GROUP BY status",
            (student_id,),
        )
    }
    generation_counts = {
        row["status"]: row["count"]
        for row in conn.execute(
            """
            SELECT status,COUNT(*) count
            FROM artifact_generation_runs WHERE student_id=? GROUP BY status
            """,
            (student_id,),
        )
    }
    stale_generations = conn.execute(
        """
        SELECT COUNT(*) FROM artifact_generation_runs
        WHERE student_id=? AND stale_reason IS NOT NULL
        """,
        (student_id,),
    ).fetchone()[0]
    return {
        "generated_at": utc_now(),
        "router": {
            "skill": "$route-learning-task",
            "status": "ready",
            "rule": "Classify once, invoke only the smallest specialist chain, and record progress automatically.",
        },
        "summary": {
            "active": counts.get("planned", 0) + counts.get("in_progress", 0),
            "needs_input": counts.get("needs_input", 0),
            "completed": counts.get("completed", 0),
            "failed": counts.get("failed", 0),
            "generation_active": generation_counts.get("planned", 0)
            + generation_counts.get("in_progress", 0),
            "generation_completed": generation_counts.get("completed", 0),
            "generation_failed": generation_counts.get("failed", 0),
            "generation_stale": int(stale_generations),
        },
        "capabilities": [asdict(item) for item in CAPABILITIES if item.key != "dashboard-sync"],
        "recent_runs": recent["items"],
        "recent_generations": recent_generations["items"],
    }
