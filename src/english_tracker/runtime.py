from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, MutableMapping


CONFIG_ENV = "OPEN_TUTOR_CONFIG"
CONFIG_VERSION = 1
CONFIG_KEYS = ("data_dir", "db_name", "question_bank", "library_root", "project_root")
PATH_KEYS = {"data_dir", "question_bank", "library_root", "project_root"}
ENV_BY_KEY = {
    "data_dir": "ENGLISH_TRACKER_DATA_DIR",
    "db_name": "ENGLISH_TRACKER_DB_NAME",
    "question_bank": "ENGLISH_TRACKER_QUESTION_BANK",
    "library_root": "ENGLISH_TRACKER_LIBRARY_ROOT",
    "project_root": "OPEN_TUTOR_PROJECT_ROOT",
}


@dataclass(frozen=True)
class RuntimeConfig:
    path: Path | None
    source: str
    values: dict[str, str]
    exists: bool


def default_config_path(environ: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    profile = env.get("USERPROFILE") or env.get("HOME")
    root = Path(profile).expanduser() if profile else Path.home()
    return (root / ".opentutor" / "config.json").resolve()


def discover_config_path(
    explicit: str | Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> tuple[Path | None, str]:
    env = os.environ if environ is None else environ
    if explicit:
        return Path(explicit).expanduser().resolve(), "command_line"
    if env.get(CONFIG_ENV):
        return Path(env[CONFIG_ENV]).expanduser().resolve(), "environment"
    default = default_config_path(env)
    if default.exists():
        return default, "default"
    return None, "none"


def _expand_percent_variables(value: str, environ: Mapping[str, str]) -> str:
    return re.sub(
        r"%([^%]+)%",
        lambda match: environ.get(match.group(1), match.group(0)),
        value,
    )


def _resolved_path(value: str, *, base: Path, environ: Mapping[str, str]) -> str:
    expanded = _expand_percent_variables(value, environ)
    expanded = os.path.expandvars(expanded)
    path = Path(expanded).expanduser()
    if not path.is_absolute():
        path = base / path
    return str(path.resolve())


def _validate_raw(raw: object) -> dict[str, str]:
    if not isinstance(raw, dict):
        raise ValueError("OpenTutor config must be a JSON object")
    unknown = sorted(set(raw) - set(CONFIG_KEYS) - {"config_version"})
    if unknown:
        raise ValueError(f"Unknown OpenTutor config key(s): {', '.join(unknown)}")
    version = raw.get("config_version", CONFIG_VERSION)
    if version != CONFIG_VERSION:
        raise ValueError(
            f"Unsupported OpenTutor config_version {version!r}; expected {CONFIG_VERSION}"
        )
    values: dict[str, str] = {}
    for key in CONFIG_KEYS:
        if key not in raw:
            continue
        value = raw[key]
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"OpenTutor config '{key}' must be a non-empty string")
        values[key] = value.strip()
    db_name = values.get("db_name")
    if db_name and (
        Path(db_name).name != db_name
        or not db_name.lower().endswith((".sqlite", ".sqlite3", ".db"))
    ):
        raise ValueError("OpenTutor config 'db_name' must be a plain SQLite filename")
    return values


def load_runtime_config(
    explicit: str | Path | None = None,
    *,
    allow_missing: bool = False,
    environ: Mapping[str, str] | None = None,
) -> RuntimeConfig:
    env = os.environ if environ is None else environ
    path, source = discover_config_path(explicit, environ=env)
    if path is None:
        return RuntimeConfig(path=None, source=source, values={}, exists=False)
    if not path.exists():
        if allow_missing:
            return RuntimeConfig(path=path, source=source, values={}, exists=False)
        raise ValueError(f"OpenTutor config not found: {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid OpenTutor config JSON at {path}: {exc}") from exc
    values = _validate_raw(raw)
    resolved = {
        key: _resolved_path(value, base=path.parent, environ=env)
        if key in PATH_KEYS
        else value
        for key, value in values.items()
    }
    return RuntimeConfig(path=path, source=source, values=resolved, exists=True)


def apply_runtime_config(
    config: RuntimeConfig,
    *,
    environ: MutableMapping[str, str] | None = None,
) -> None:
    env = os.environ if environ is None else environ
    if config.path is not None:
        env[CONFIG_ENV] = str(config.path)
    for key, value in config.values.items():
        env[ENV_BY_KEY[key]] = value


def effective_runtime_values(
    config: RuntimeConfig,
    *,
    explicit_data_dir: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, str | None]:
    env = os.environ if environ is None else environ
    values: dict[str, str | None] = {
        key: env.get(env_name) for key, env_name in ENV_BY_KEY.items()
    }
    if explicit_data_dir:
        values["data_dir"] = str(Path(explicit_data_dir).expanduser().resolve())
    if not values["db_name"]:
        values["db_name"] = "learning.sqlite"
    return values


def config_summary(
    config: RuntimeConfig,
    *,
    explicit_data_dir: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, object]:
    values = effective_runtime_values(
        config,
        explicit_data_dir=explicit_data_dir,
        environ=environ,
    )
    required = ("data_dir", "question_bank", "library_root")
    missing = [key for key in required if not values.get(key)]
    return {
        "status": "ready" if not missing else "incomplete",
        "config_path": str(config.path) if config.path else None,
        "config_source": config.source,
        "config_exists": config.exists,
        "config_version": CONFIG_VERSION,
        "values": values,
        "missing": missing,
    }


def set_config_value(path: Path, key: str, value: str) -> RuntimeConfig:
    if key not in CONFIG_KEYS:
        raise ValueError(f"Unknown OpenTutor config key: {key}")
    value = str(value).strip()
    if not value:
        raise ValueError("OpenTutor config values cannot be empty")
    raw: dict[str, object] = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid OpenTutor config JSON at {path}: {exc}") from exc
        if not isinstance(loaded, dict):
            raise ValueError("OpenTutor config must be a JSON object")
        raw.update(loaded)
    raw["config_version"] = CONFIG_VERSION
    raw[key] = value
    _validate_raw(raw)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(raw, ensure_ascii=False, indent=2) + "\n"
    fd, temporary = tempfile.mkstemp(prefix="config-", suffix=".json.tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
        os.replace(temporary, path)
    except Exception:
        try:
            Path(temporary).unlink(missing_ok=True)
        finally:
            raise
    return load_runtime_config(path)
