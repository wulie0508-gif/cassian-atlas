from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from .db import connect


def create_backup(db_path: str | Path, backup_dir: str | Path, reason: str) -> Path | None:
    source_path = Path(db_path)
    if not source_path.exists() or source_path.stat().st_size == 0:
        return None
    target_dir = Path(backup_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    safe_reason = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in reason)[:40]
    target = target_dir / f"{source_path.stem}_{stamp}_{safe_reason}.sqlite"
    source = connect(source_path, readonly=True)
    destination = sqlite3.connect(target)
    try:
        source.backup(destination)
        result = destination.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            raise RuntimeError(f"Backup integrity check failed: {result}")
    finally:
        destination.close()
        source.close()
    return target
