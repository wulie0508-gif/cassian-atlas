from __future__ import annotations

import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RECOVERED_MARKER = "\u627e\u56de\u7684\u6587\u4ef6"
BLOCKED_SUFFIXES = {
    ".sqlite", ".sqlite3", ".db", ".pdf", ".doc", ".docx", ".xls", ".xlsx",
    ".ppt", ".pptx", ".png", ".jpg", ".jpeg", ".wav", ".mp3", ".zip", ".7z", ".rar",
}
PRIVATE_PATTERNS = {
    "Windows user path": re.compile(r"[A-Za-z]:\\Users\\[^\\\s]+", re.IGNORECASE),
    "private recovered-file path": re.compile(r"[A-Za-z]:\\[^\r\n]*" + RECOVERED_MARKER, re.IGNORECASE),
    "private learner marker": re.compile("胡" + "楠"),
    "private database marker": re.compile("hunan" + "_learning", re.IGNORECASE),
}
TEXT_SUFFIXES = {".py", ".md", ".json", ".yaml", ".yml", ".toml", ".sql", ".txt", ".js", ".css", ".html", ".svg", ".ps1", ".cmd", ".sh"}


def tracked_files() -> list[Path]:
    output = subprocess.check_output(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=ROOT,
    )
    return [ROOT / item.decode("utf-8") for item in output.split(b"\0") if item]


def main() -> int:
    violations: list[str] = []
    for path in tracked_files():
        relative = path.relative_to(ROOT)
        if path.suffix.lower() in BLOCKED_SUFFIXES:
            violations.append(f"blocked content type: {relative}")
        if path.stat().st_size > 2_000_000:
            violations.append(f"tracked file larger than 2 MB: {relative}")
        if path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        text = path.read_text(encoding="utf-8-sig")
        for label, pattern in PRIVATE_PATTERNS.items():
            if pattern.search(text):
                violations.append(f"{label}: {relative}")
    if violations:
        print("Privacy audit failed:")
        print("\n".join(f"- {item}" for item in violations))
        return 1
    print(f"Privacy audit passed: {len(tracked_files())} tracked files, no private data or source materials detected.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
