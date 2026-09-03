from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from . import __version__
from .db import database_path, ensure_private_layout
from .util import utc_now


def _state_path(data_dir: Path) -> Path:
    return data_dir / "logs" / "opentutor-server.json"


def _read_state(data_dir: Path) -> dict[str, Any] | None:
    path = _state_path(data_dir)
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"invalid": True}
    return value if isinstance(value, dict) else {"invalid": True}


def _write_state(data_dir: Path, value: dict[str, Any]) -> None:
    path = _state_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    os.replace(temporary, path)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        # os.kill(pid, 0) is not a reliable existence probe on Windows and can
        # report recently exited processes as alive.  Query the process handle
        # and its exit code instead so stale server state is recoverable.
        import ctypes

        process_query_limited_information = 0x1000
        still_active = 259
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(
            process_query_limited_information,
            False,
            int(pid),
        )
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return False
            return exit_code.value == still_active
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _health(host: str, port: int, timeout: float = 1.5) -> dict[str, Any] | None:
    url = f"http://{host}:{port}/api/health"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            value = json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _same_path(left: object, right: Path) -> bool:
    if not left:
        return False
    try:
        return os.path.normcase(str(Path(str(left)).resolve())) == os.path.normcase(
            str(right.resolve())
        )
    except OSError:
        return False


def server_status(data_dir: Path, *, host: str = "127.0.0.1", port: int = 8788) -> dict[str, Any]:
    expected_database = database_path(data_dir)
    state = _read_state(data_dir)
    health = _health(host, port)
    url = f"http://{host}:{port}"
    if state is None:
        return {
            "status": "unmanaged" if health else "stopped",
            "url": url,
            "managed": False,
            "health": health,
            "database": str(expected_database),
        }
    if state.get("invalid"):
        return {
            "status": "incompatible" if health else "stale",
            "url": url,
            "managed": False,
            "reason": "invalid_server_state_file",
            "health": health,
            "database": str(expected_database),
        }
    try:
        pid = int(state.get("pid", 0))
    except (TypeError, ValueError):
        pid = 0
    if not _pid_alive(pid):
        return {
            "status": "incompatible" if health else "stale",
            "url": url,
            "managed": False,
            "pid": pid or None,
            "reason": "recorded_process_not_running",
            "health": health,
            "database": str(expected_database),
        }
    if not health:
        return {
            "status": "unhealthy",
            "url": url,
            "managed": True,
            "pid": pid,
            "database": str(expected_database),
        }
    mismatches: list[str] = []
    if health.get("process_id") is not None and int(health["process_id"]) != pid:
        mismatches.append("process_id")
    if health.get("app_version") and health["app_version"] != __version__:
        mismatches.append("app_version")
    if health.get("database") and not _same_path(health["database"], expected_database):
        mismatches.append("database")
    schema = health.get("schema")
    if isinstance(schema, dict) and schema.get("migration_required"):
        mismatches.append("schema")
    if health.get("status") != "ok":
        mismatches.append("health")
    return {
        "status": "incompatible" if mismatches else "running",
        "url": url,
        "managed": not mismatches,
        "pid": pid,
        "version": health.get("app_version"),
        "database": str(expected_database),
        "mismatches": mismatches,
        "health": health,
    }


def start_server(
    data_dir: Path,
    *,
    config_path: Path | None,
    project_root: str | None,
    student_id: str | None,
    host: str = "127.0.0.1",
    port: int = 8788,
    timeout: float = 60,
) -> dict[str, Any]:
    ensure_private_layout(data_dir)
    before = server_status(data_dir, host=host, port=port)
    if before["status"] == "running":
        return {**before, "result": "already_running"}
    if before["status"] in {"unmanaged", "incompatible", "unhealthy"}:
        raise RuntimeError(
            "A server is already using this runtime or port but cannot be safely reused. "
            "Inspect `cassian server status`; stop the old process before starting this version."
        )
    state_file = _state_path(data_dir)
    if before["status"] == "stale":
        state_file.unlink(missing_ok=True)
    command = [sys.executable, "-m", "english_tracker"]
    if config_path:
        command.extend(["--config", str(config_path)])
    command.extend(["serve", "--host", host, "--port", str(port)])
    if student_id:
        command.extend(["--student", student_id])
    working_directory = None
    if project_root:
        candidate = Path(project_root).expanduser().resolve()
        if not candidate.is_dir():
            raise ValueError(f"Configured project_root is not a directory: {candidate}")
        working_directory = str(candidate)
    log_path = data_dir / "logs" / "opentutor-server.log"
    log_handle = log_path.open("a", encoding="utf-8")
    popen_options: dict[str, Any] = {
        "cwd": working_directory,
        "env": os.environ.copy(),
        "stdin": subprocess.DEVNULL,
        "stdout": log_handle,
        "stderr": subprocess.STDOUT,
        "close_fds": True,
    }
    if os.name == "nt":
        popen_options["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
        )
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = subprocess.SW_HIDE
        popen_options["startupinfo"] = startupinfo
    else:
        popen_options["start_new_session"] = True
    try:
        process = subprocess.Popen(command, **popen_options)
    finally:
        log_handle.close()
    _write_state(
        data_dir,
        {
            "pid": process.pid,
            "host": host,
            "port": port,
            "database": str(database_path(data_dir)),
            "app_version": __version__,
            "config_path": str(config_path) if config_path else None,
            "started_at": utc_now(),
        },
    )
    deadline = time.monotonic() + max(1.0, timeout)
    while time.monotonic() < deadline:
        if process.poll() is not None:
            state_file.unlink(missing_ok=True)
            raise RuntimeError(
                f"Cassian Atlas server exited during startup with code {process.returncode}; "
                f"inspect {log_path}"
            )
        current = server_status(data_dir, host=host, port=port)
        if current["status"] == "running":
            return {**current, "result": "started", "log": str(log_path)}
        if current["status"] == "incompatible":
            process.terminate()
            state_file.unlink(missing_ok=True)
            raise RuntimeError(
                "The process answered health checks with a different version, database, PID, or schema"
            )
        time.sleep(0.25)
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
    state_file.unlink(missing_ok=True)
    raise RuntimeError(f"Cassian Atlas server did not become healthy within {timeout:g} seconds")


def stop_server(
    data_dir: Path,
    *,
    host: str = "127.0.0.1",
    port: int = 8788,
    timeout: float = 15,
    force: bool = False,
) -> dict[str, Any]:
    before = server_status(data_dir, host=host, port=port)
    state_file = _state_path(data_dir)
    if before["status"] in {"stopped", "stale"}:
        state_file.unlink(missing_ok=True)
        return {**before, "result": "already_stopped"}
    if before["status"] in {"unmanaged", "incompatible"}:
        raise RuntimeError(
            "Refusing to stop a process that is not verified against this runtime's PID, version, and database"
        )
    if before["status"] == "unhealthy" and not force:
        raise RuntimeError(
            "The managed PID is alive but health verification failed; inspect it or rerun with --force"
        )
    pid = int(before["pid"])
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + max(1.0, timeout)
    while time.monotonic() < deadline and _pid_alive(pid):
        time.sleep(0.2)
    if _pid_alive(pid):
        raise RuntimeError(f"Cassian Atlas server PID {pid} did not stop within {timeout:g} seconds")
    state_file.unlink(missing_ok=True)
    return {
        "status": "stopped",
        "result": "stopped",
        "pid": pid,
        "url": before["url"],
        "database": before["database"],
    }
