#!/usr/bin/env python3
"""Privacy-minimized page-batch handwriting transcription for Volcano Ark.

This script produces provider candidates only. It never grades or commits learning facts.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import mimetypes
import os
import ssl
import sys
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


ENV_NAMES = (
    "OPEN_TUTOR_DOUBAO_API_KEY",
    "OPEN_TUTOR_DOUBAO_BASE_URL",
    "OPEN_TUTOR_DOUBAO_MODEL",
    "OPEN_TUTOR_DOUBAO_TIMEOUT_SECONDS",
    "OPEN_TUTOR_DOUBAO_MAX_RETRIES",
    "OPEN_TUTOR_DOUBAO_RETRY_BASE_SECONDS",
)
DEFAULT_URL = "https://ark.cn-beijing.volces.com/api/v3/chat/completions"
DEFAULT_PROMPT_VERSION = "opentutor-transcription-page-v1"
ALLOWED_MIME = {"image/jpeg", "image/png", "image/webp"}
ALLOWED_RISK = {"R0", "R1", "R2", "R3", "R4"}
ALLOWED_CAPTURE = {
    "captured",
    "captured_blank",
    "not_captured",
    "needs_check",
    "blocked_image_quality",
    "blocked_alignment",
}
FORBIDDEN_FIELDS = {
    "answer",
    "answer_key",
    "correct_answer",
    "standard_answer",
    "standard_answers",
    "student",
    "student_id",
    "student_name",
    "student_display_name",
    "learner",
    "learner_id",
    "score",
    "grade",
    "diagnosis",
    "private_path",
    "local_path",
    "other_provider_result",
    "other_provider_results",
    "peer_provider_result",
    "peer_provider_results",
}


def private_config_path() -> Path:
    profile = os.environ.get("USERPROFILE")
    if not profile:
        raise RuntimeError("USERPROFILE is unavailable")
    return Path(profile) / ".opentutor" / "doubao.env"


def read_env_file(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    if not path.exists():
        return result
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        result[key.strip()] = value.strip()
    return result


def load_config(path: Path) -> dict[str, Any]:
    values = read_env_file(path)
    for name in ENV_NAMES:
        if os.environ.get(name):
            values[name] = os.environ[name]
    base_url = values.get("OPEN_TUTOR_DOUBAO_BASE_URL", DEFAULT_URL).strip()
    parsed = urlparse(base_url)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise ValueError("Doubao base URL must be HTTPS and cannot contain credentials")
    return {
        "api_key": values.get("OPEN_TUTOR_DOUBAO_API_KEY", "").strip(),
        "base_url": base_url,
        "model": values.get("OPEN_TUTOR_DOUBAO_MODEL", "").strip(),
        "timeout": bounded_float(values.get("OPEN_TUTOR_DOUBAO_TIMEOUT_SECONDS", "30"), 0.1, 300),
        "retries": bounded_int(values.get("OPEN_TUTOR_DOUBAO_MAX_RETRIES", "2"), 0, 5),
        "retry_base": bounded_float(values.get("OPEN_TUTOR_DOUBAO_RETRY_BASE_SECONDS", "0.5"), 0, 60),
        "config_path": str(path),
    }


def bounded_float(value: str, minimum: float, maximum: float) -> float:
    number = float(value)
    if not minimum <= number <= maximum:
        raise ValueError(f"numeric setting must be between {minimum} and {maximum}")
    return number


def bounded_int(value: str, minimum: int, maximum: int) -> int:
    number = int(value)
    if not minimum <= number <= maximum:
        raise ValueError(f"integer setting must be between {minimum} and {maximum}")
    return number


def status_payload(config: dict[str, Any]) -> dict[str, Any]:
    missing = []
    if not config["api_key"]:
        missing.append("OPEN_TUTOR_DOUBAO_API_KEY")
    if not config["model"]:
        missing.append("OPEN_TUTOR_DOUBAO_MODEL")
    return {
        "provider": "doubao",
        "configured": not missing,
        "missing": missing,
        "base_url": config["base_url"],
        "model": config["model"] or None,
        "api_key": "configured (hidden)" if config["api_key"] else "missing",
        "config_path": config["config_path"],
    }


def find_forbidden(value: Any, prefix: str = "") -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key).strip().lower().replace("-", "_")
            path = f"{prefix}.{normalized}" if prefix else normalized
            if normalized in FORBIDDEN_FIELDS:
                found.append(path)
            found.extend(find_forbidden(item, path))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found.extend(find_forbidden(item, f"{prefix}[{index}]"))
    return sorted(set(found))


def sanitize_manifest(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("manifest must be a JSON object")
    forbidden = find_forbidden(raw)
    if forbidden:
        raise ValueError("forbidden manifest field(s): " + ", ".join(forbidden))
    raw_items = raw.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        raise ValueError("manifest.items must be a non-empty array")
    if len(raw_items) > 100:
        raise ValueError("one page batch cannot contain more than 100 items")
    clean_items = []
    seen = set()
    for index, item in enumerate(raw_items):
        if not isinstance(item, dict):
            raise ValueError(f"items[{index}] must be an object")
        item_id = str(item.get("item_id") or "").strip()
        if not item_id or item_id in seen:
            raise ValueError(f"items[{index}].item_id must be present and unique")
        seen.add(item_id)
        risk = str(item.get("risk_level") or "").strip().upper()
        if risk not in ALLOWED_RISK:
            raise ValueError(f"items[{index}].risk_level must be R0-R4")
        clean: dict[str, Any] = {
            "item_id": item_id,
            "question_label": str(item.get("question_label") or item_id).strip(),
            "question_type": str(item.get("question_type") or "unknown").strip(),
            "risk_level": risk,
        }
        bbox = item.get("bbox")
        if bbox is not None:
            if not isinstance(bbox, list) or len(bbox) != 4 or any(
                isinstance(number, bool) or not isinstance(number, (int, float)) for number in bbox
            ):
                raise ValueError(f"items[{index}].bbox must contain four numbers")
            clean["bbox"] = bbox
        clean_items.append(clean)
    return {
        "prompt_version": str(raw.get("prompt_version") or DEFAULT_PROMPT_VERSION).strip(),
        "page_label": str(raw.get("page_label") or "page").strip(),
        "items": clean_items,
    }


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def cache_path(config: dict[str, Any], image_hash: str, manifest: dict[str, Any]) -> Path:
    identity = canonical_json(
        {
            "provider": "doubao",
            "model": config["model"],
            "image_sha256": image_hash,
            "manifest": manifest,
        }
    ).encode("utf-8")
    key = sha256_bytes(identity)
    profile = os.environ.get("USERPROFILE")
    if not profile:
        raise RuntimeError("USERPROFILE is unavailable")
    return Path(profile) / ".opentutor" / "provider_cache" / "doubao" / f"{key}.json"


def build_request(config: dict[str, Any], image_bytes: bytes, mime: str, manifest: dict[str, Any]) -> bytes:
    instruction = {
        "task": "transcribe_visible_student_answers_on_one_page",
        "prompt_version": manifest["prompt_version"],
        "page_label": manifest["page_label"],
        "items": manifest["items"],
        "constraints": [
            "Transcribe only visible student writing in each specified answer region.",
            "Do not solve, correct, grade, or infer toward an expected answer.",
            "Keep spelling and grammar errors exactly as written in raw_transcription.",
            "Use captured_blank only when the answer region is visibly blank.",
            "Use needs_check or blocked_image_quality when uncertain.",
            "Return every requested item exactly once and no extra items.",
            "Return one JSON object only.",
        ],
        "response_schema": {
            "items": [
                {
                    "item_id": "string",
                    "raw_transcription": "string",
                    "normalized_transcription": "string",
                    "capture_status": "captured|captured_blank|not_captured|needs_check|blocked_image_quality|blocked_alignment",
                    "uncertain_spans": [],
                    "candidate_alternatives": [],
                    "confidence": "number from 0 to 1",
                }
            ]
        },
    }
    body = {
        "model": config["model"],
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": canonical_json(instruction)},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime};base64,{base64.b64encode(image_bytes).decode('ascii')}"},
                    },
                ],
            }
        ],
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }
    return canonical_json(body).encode("utf-8")


def strip_fence(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```") and stripped.endswith("```"):
        lines = stripped.splitlines()
        if len(lines) >= 3:
            return "\n".join(lines[1:-1]).strip()
    return stripped


def validate_candidate(content: str, manifest: dict[str, Any]) -> list[dict[str, Any]]:
    decoded = json.loads(strip_fence(content))
    if not isinstance(decoded, dict) or not isinstance(decoded.get("items"), list):
        raise ValueError("provider content must contain an items array")
    expected = [item["item_id"] for item in manifest["items"]]
    by_id: dict[str, dict[str, Any]] = {}
    for candidate in decoded["items"]:
        if not isinstance(candidate, dict):
            raise ValueError("provider item must be an object")
        item_id = str(candidate.get("item_id") or "").strip()
        if item_id in by_id:
            raise ValueError(f"provider returned duplicate item_id: {item_id}")
        status = str(candidate.get("capture_status") or "").strip()
        if status not in ALLOWED_CAPTURE:
            raise ValueError(f"invalid capture_status for {item_id}")
        raw = candidate.get("raw_transcription")
        normalized = candidate.get("normalized_transcription")
        if not isinstance(raw, str) or not isinstance(normalized, str):
            raise ValueError(f"provider transcription for {item_id} must be text")
        confidence = candidate.get("confidence", 0)
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
            raise ValueError(f"provider confidence for {item_id} must be from 0 to 1")
        uncertain = candidate.get("uncertain_spans", [])
        alternatives = candidate.get("candidate_alternatives", [])
        if not isinstance(uncertain, list) or not isinstance(alternatives, list):
            raise ValueError(f"provider uncertainty fields for {item_id} must be arrays")
        by_id[item_id] = {
            "item_id": item_id,
            "raw_transcription": raw,
            "normalized_transcription": normalized,
            "capture_status": status,
            "uncertain_spans": uncertain,
            "candidate_alternatives": alternatives,
            "confidence": float(confidence),
        }
    if set(by_id) != set(expected):
        missing = sorted(set(expected) - set(by_id))
        extra = sorted(set(by_id) - set(expected))
        raise ValueError(f"provider item alignment mismatch; missing={missing}, extra={extra}")
    return [by_id[item_id] for item_id in expected]


def call_provider(config: dict[str, Any], body: bytes) -> tuple[dict[str, Any], int]:
    request = Request(
        config["base_url"],
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {config['api_key']}",
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "application/json",
        },
    )
    attempts = config["retries"] + 1
    for attempt in range(1, attempts + 1):
        try:
            with urlopen(request, timeout=config["timeout"], context=ssl.create_default_context()) as response:
                return json.loads(response.read().decode("utf-8")), attempt
        except HTTPError as exc:
            if exc.code in {429, 500, 502, 503, 504} and attempt < attempts:
                time.sleep(config["retry_base"] * (2 ** (attempt - 1)))
                continue
            if exc.code in {401, 403}:
                raise RuntimeError(f"provider authentication failed ({exc.code})") from None
            raise RuntimeError(f"provider HTTP error ({exc.code})") from None
        except (URLError, TimeoutError) as exc:
            if attempt < attempts:
                time.sleep(config["retry_base"] * (2 ** (attempt - 1)))
                continue
            raise RuntimeError(f"provider connection failed: {type(exc).__name__}") from None
    raise RuntimeError("provider attempts exhausted")


def extract_content(response: dict[str, Any]) -> str:
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise ValueError("provider response has no choice")
    message = choices[0].get("message")
    if not isinstance(message, dict) or not isinstance(message.get("content"), str):
        raise ValueError("provider response has no JSON text content")
    return message["content"]


def write_json(payload: dict[str, Any], output: str | None) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if output:
        path = Path(output).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text + "\n", encoding="utf-8")
        print(json.dumps({"status": payload.get("status"), "output": str(path)}, ensure_ascii=False))
    else:
        print(text)


def run_extract(args: argparse.Namespace, config: dict[str, Any]) -> int:
    image_path = Path(args.image).resolve()
    manifest_path = Path(args.manifest).resolve()
    if not image_path.is_file() or not manifest_path.is_file():
        raise FileNotFoundError("image and manifest must both exist")
    image_bytes = image_path.read_bytes()
    if not image_bytes or len(image_bytes) > 20 * 1024 * 1024:
        raise ValueError("image must be between 1 byte and 20 MB")
    mime = mimetypes.guess_type(image_path.name)[0] or ""
    if mime not in ALLOWED_MIME:
        raise ValueError("image must be JPEG, PNG, or WebP")
    manifest = sanitize_manifest(json.loads(manifest_path.read_text(encoding="utf-8-sig")))
    image_hash = sha256_bytes(image_bytes)
    cache = cache_path(config, image_hash, manifest)
    request_body = build_request(config, image_bytes, mime, manifest)
    request_hash = sha256_bytes(request_body)

    if args.dry_run:
        write_json(
            {
                "status": "dry_run",
                "configured": bool(config["api_key"] and config["model"]),
                "provider": "doubao",
                "model": config["model"] or None,
                "prompt_version": manifest["prompt_version"],
                "item_count": len(manifest["items"]),
                "image_sha256": image_hash,
                "request_sha256": request_hash,
                "cache_path": str(cache),
            },
            args.output,
        )
        return 0

    if not config["api_key"] or not config["model"]:
        raise RuntimeError("Doubao is not configured; run configure_doubao.ps1 locally")
    if cache.exists() and not args.force:
        cached = json.loads(cache.read_text(encoding="utf-8"))
        cached["cache_hit"] = True
        write_json(cached, args.output)
        return 0

    response, attempts = call_provider(config, request_body)
    items = validate_candidate(extract_content(response), manifest)
    result = {
        "status": "succeeded",
        "provider": "doubao",
        "model_version": config["model"],
        "prompt_version": manifest["prompt_version"],
        "page_label": manifest["page_label"],
        "source_image_sha256": image_hash,
        "request_sha256": request_hash,
        "response_sha256": sha256_bytes(canonical_json(response).encode("utf-8")),
        "attempt_count": attempts,
        "cache_hit": False,
        "usage": response.get("usage") if isinstance(response.get("usage"), dict) else None,
        "items": items,
    }
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_json(result, args.output)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Doubao page-batch answer transcription candidate tool")
    parser.add_argument("--config", help="private env file; defaults to USERPROFILE/.opentutor/doubao.env")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("status", help="show sanitized configuration status")
    extract = subparsers.add_parser("extract", help="transcribe one page without grading")
    extract.add_argument("--image", required=True)
    extract.add_argument("--manifest", required=True)
    extract.add_argument("--output")
    extract.add_argument("--dry-run", action="store_true")
    extract.add_argument("--force", action="store_true", help="ignore an existing cache candidate")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        config_path = Path(args.config).resolve() if args.config else private_config_path()
        config = load_config(config_path)
        if args.command == "status":
            print(json.dumps(status_payload(config), ensure_ascii=False, indent=2))
            return 0
        return run_extract(args, config)
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": str(exc)[:300]}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
