from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RECOVERED_MARKER = "\u627e\u56de\u7684\u6587\u4ef6"
BLOCKED_SUFFIXES = {
    ".sqlite", ".sqlite3", ".db", ".pdf", ".doc", ".docx", ".xls", ".xlsx",
    ".ppt", ".pptx", ".png", ".jpg", ".jpeg", ".webp", ".heic", ".tif", ".tiff",
    ".wav", ".mp3", ".m4a", ".mp4", ".mov", ".zip", ".7z", ".rar", ".env", ".log",
    ".csv", ".tsv", ".jsonl", ".db-wal", ".db-shm", ".sqlite-wal", ".sqlite-shm",
}
LOCAL_USER_MARKER = "hua" + "wei"
PRIVATE_PATTERNS = {
    "Windows user path": re.compile(r"[A-Za-z]:\\Users\\[^\\\s]+", re.IGNORECASE),
    "private recovered-file path": re.compile(r"[A-Za-z]:\\[^\r\n]*" + RECOVERED_MARKER, re.IGNORECASE),
    "private learner marker": re.compile("胡" + "楠"),
    "private learner romanization": re.compile("hu" + r"[\s_-]*" + "nan", re.IGNORECASE),
    "private database marker": re.compile("hu" + "nan" + "_learning", re.IGNORECASE),
    "split private Windows user path": re.compile(
        r"C:\\\\Users\\\\[\"']?\s*\+\s*[\"']" + LOCAL_USER_MARKER,
        re.IGNORECASE,
    ),
}
SECRET_PATTERNS = {
    "literal bearer credential": re.compile(r"(?i)bearer\s+(?!\[?redacted\]?|\{|<)[A-Za-z0-9._~+/=-]{20,}"),
    "OpenAI-style secret": re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    "JWT credential": re.compile(r"\beyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
    "AWS access key": re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    "GitHub personal token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
    "Feishu resource token": re.compile(
        r"\b(?:bascn|fldcn)(?!Synthetic|Anonymous|Different)[A-Za-z0-9_-]{12,}\b",
        re.IGNORECASE,
    ),
    "Feishu application id": re.compile(r"\bcli_[A-Za-z0-9]{12,}\b", re.IGNORECASE),
    "Slack token": re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b", re.IGNORECASE),
    "literal assigned credential": re.compile(
        r"(?i)(?:api[_-]?key|app[_-]?secret|client[_-]?secret|password|refresh[_-]?token|"
        r"access[_-]?token|tenant[_-]?access[_-]?token)"
        r"\s*[\"']?\s*[:=]\s*[\"']?(?!<|\[|\{|test-|generic-|vendor-|placeholder\b|private\b|configured\b|missing\b)"
        r"[A-Za-z0-9._~+/=-]{20,}"
    ),
}
PUBLIC_BINARY_ASSETS = {
    Path("site/assets/social-card.png"): "bf0affa9ce89e963d75162badd9da5695b9d67be72a12f170938d2c61eb9390c",
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tracked_files() -> list[Path]:
    output = subprocess.check_output(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=ROOT,
    )
    return [ROOT / item.decode("utf-8") for item in output.split(b"\0") if item]


def tracked_ignored_files() -> list[str]:
    output = subprocess.check_output(
        ["git", "ls-files", "-ci", "--exclude-standard", "-z"],
        cwd=ROOT,
    )
    return [item.decode("utf-8", errors="replace") for item in output.split(b"\0") if item]


def scan_text(text: str, location: str, violations: list[str]) -> None:
    for label, pattern in {**PRIVATE_PATTERNS, **SECRET_PATTERNS}.items():
        if pattern.search(text):
            violations.append(f"{label}: {location}")


def decode_probable_text(data: bytes) -> str | None:
    if b"\0" in data:
        return None
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None


def history_revisions(*, include_all_refs: bool) -> list[str]:
    if include_all_refs:
        return ["--all"]
    output = subprocess.check_output(
        [
            "git",
            "for-each-ref",
            "--format=%(refname)",
            "refs/heads",
            "refs/tags",
        ],
        cwd=ROOT,
        text=True,
    )
    # HEAD keeps detached CI checkouts in scope. Provider-owned pull-request
    # refs and cache refs are intentionally excluded from the release gate;
    # use --all-refs for a forensic scan that includes them.
    return ["HEAD", *sorted({item for item in output.splitlines() if item})]


def scan_reachable_history(
    violations: list[str], *, include_all_refs: bool = False
) -> int:
    shallow = subprocess.check_output(
        ["git", "rev-parse", "--is-shallow-repository"],
        cwd=ROOT,
        text=True,
    ).strip()
    if shallow == "true":
        violations.append("history audit requires a full clone, but this repository is shallow")
        return 0

    revisions = history_revisions(include_all_refs=include_all_refs)
    output = subprocess.check_output(
        ["git", "rev-list", "--objects", *revisions], cwd=ROOT
    )
    scanned = 0
    seen: set[tuple[str, str]] = set()
    for raw_line in output.splitlines():
        object_id, separator, raw_path = raw_line.partition(b" ")
        if not separator:
            continue
        path_text = raw_path.decode("utf-8", errors="replace")
        object_text = object_id.decode("ascii")
        identity = (object_text, path_text)
        if identity in seen:
            continue
        seen.add(identity)
        scan_text(path_text, f"history-path:{path_text}", violations)
        object_type = subprocess.check_output(
            ["git", "cat-file", "-t", object_text],
            cwd=ROOT,
            text=True,
        ).strip()
        if object_type != "blob":
            continue
        suffix = Path(path_text).suffix.lower()
        blob = subprocess.run(
            ["git", "cat-file", "-p", object_text],
            cwd=ROOT,
            check=True,
            capture_output=True,
        ).stdout
        historical_path = Path(path_text)
        if suffix in BLOCKED_SUFFIXES:
            expected_hash = PUBLIC_BINARY_ASSETS.get(historical_path)
            if expected_hash is None:
                violations.append(f"historical blocked content type: {path_text}")
            elif hashlib.sha256(blob).hexdigest() != expected_hash:
                violations.append(f"historical allowlisted asset hash mismatch: {path_text}")
            continue
        if len(blob) > 2_000_000:
            violations.append(f"historical blob larger than 2 MB: {path_text}")
            continue
        text = decode_probable_text(blob)
        if text is None:
            continue
        scanned += 1
        scan_text(text, f"history:{path_text}", violations)

    commit_metadata = subprocess.check_output(
        ["git", "log", "--format=%H%n%an%n%ae%n%s%n%b%x1e", *revisions],
        cwd=ROOT,
    ).decode("utf-8", errors="replace")
    scan_text(commit_metadata, "history:commit-metadata", violations)
    tag_metadata = subprocess.check_output(
        ["git", "for-each-ref", "refs/tags", "--format=%(refname)%0a%(taggername)%0a%(taggeremail)%0a%(contents)%0a"],
        cwd=ROOT,
    ).decode("utf-8", errors="replace")
    scan_text(tag_metadata, "history:tag-metadata", violations)
    return scanned


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit the public release tree for private content and credentials.")
    parser.add_argument(
        "--history",
        action="store_true",
        help="also scan text blobs reachable from HEAD plus local branches and tags",
    )
    parser.add_argument(
        "--all-refs",
        action="store_true",
        help="with --history, include provider-owned PR/cache refs for forensic review",
    )
    args = parser.parse_args()
    if args.all_refs and not args.history:
        parser.error("--all-refs requires --history")
    return args


def main() -> int:
    args = parse_args()
    violations: list[str] = []
    files = tracked_files()
    for ignored in tracked_ignored_files():
        violations.append(f"tracked file is covered by .gitignore: {ignored}")
    for path in files:
        relative = path.relative_to(ROOT)
        scan_text(relative.as_posix(), f"path:{relative.as_posix()}", violations)
        if path.suffix.lower() in BLOCKED_SUFFIXES:
            expected_hash = PUBLIC_BINARY_ASSETS.get(relative)
            if expected_hash is None:
                violations.append(f"blocked content type: {relative}")
            elif sha256_file(path) != expected_hash:
                violations.append(f"allowlisted public asset hash mismatch: {relative}")
        if path.stat().st_size > 2_000_000:
            violations.append(f"tracked file larger than 2 MB: {relative}")
            continue
        text = decode_probable_text(path.read_bytes())
        if text is None:
            continue
        scan_text(text, str(relative), violations)
    history_count = (
        scan_reachable_history(violations, include_all_refs=args.all_refs)
        if args.history
        else 0
    )
    if violations:
        print("Privacy audit failed:")
        print("\n".join(f"- {item}" for item in sorted(set(violations))))
        return 1
    history_note = f", {history_count} historical text blobs" if args.history else ""
    print(f"Privacy audit passed: {len(files)} tracked files{history_note}, no private data, credentials, or source materials detected.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
