from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import socket
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol
from urllib.parse import urlparse

from .util import canonical_json, payload_hash, utc_now


DOUBAO_PROVIDER = "doubao"
DOUBAO_ENV_PREFIX = "OPEN_TUTOR_DOUBAO_"
DOUBAO_API_KEY_ENV = f"{DOUBAO_ENV_PREFIX}API_KEY"
DOUBAO_BASE_URL_ENV = f"{DOUBAO_ENV_PREFIX}BASE_URL"
DOUBAO_MODEL_ENV = f"{DOUBAO_ENV_PREFIX}MODEL"
DOUBAO_TIMEOUT_ENV = f"{DOUBAO_ENV_PREFIX}TIMEOUT_SECONDS"
DOUBAO_MAX_RETRIES_ENV = f"{DOUBAO_ENV_PREFIX}MAX_RETRIES"
DOUBAO_RETRY_BASE_ENV = f"{DOUBAO_ENV_PREFIX}RETRY_BASE_SECONDS"

RESULT_STATUSES = {"succeeded", "failed", "unconfigured", "timeout", "rate_limited"}
CAPTURE_STATUSES = {
    "captured",
    "captured_blank",
    "not_captured",
    "needs_check",
    "blocked_image_quality",
    "blocked_alignment",
}
FORBIDDEN_REQUEST_FIELDS = {
    "standard_answer",
    "standard_answers",
    "answer_key",
    "other_provider_result",
    "other_provider_results",
    "peer_provider_result",
    "peer_provider_results",
    "student_name",
    "student_display_name",
    "private_path",
    "local_path",
}
SAFE_LOCATOR_FIELDS = {
    "bbox",
    "crop",
    "page",
    "page_number",
    "question_label",
    "region_id",
    "source_image_id",
}
ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp"}


class ProviderTransport(Protocol):
    def __call__(self, request: "ProviderHttpRequest") -> "ProviderHttpResponse": ...


@dataclass(frozen=True, slots=True)
class ProviderHttpRequest:
    url: str
    timeout_seconds: float
    body: bytes = field(repr=False)
    headers: Mapping[str, str] = field(repr=False)
    method: str = "POST"

    def __repr__(self) -> str:
        return (
            "ProviderHttpRequest("
            f"url={self.url!r}, timeout_seconds={self.timeout_seconds!r}, "
            f"body_bytes={len(self.body)}, method={self.method!r})"
        )


@dataclass(frozen=True, slots=True)
class ProviderHttpResponse:
    status_code: int
    body: bytes = field(repr=False)
    headers: Mapping[str, str] = field(default_factory=dict, repr=False)


@dataclass(frozen=True, slots=True)
class DoubaoConfig:
    """Environment-backed provider settings with a deliberately private credential."""

    base_url: str
    model: str
    timeout_seconds: float = 30.0
    max_retries: int = 2
    retry_base_seconds: float = 0.5
    _api_key: str = field(default="", repr=False, compare=False)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "DoubaoConfig":
        env = os.environ if environ is None else environ
        api_key = str(env.get(DOUBAO_API_KEY_ENV) or "").strip()
        base_url = str(env.get(DOUBAO_BASE_URL_ENV) or "").strip()
        model = str(env.get(DOUBAO_MODEL_ENV) or "").strip()
        timeout = _number_setting(env, DOUBAO_TIMEOUT_ENV, 30.0, minimum=0.1, maximum=300.0)
        retries = _integer_setting(env, DOUBAO_MAX_RETRIES_ENV, 2, minimum=0, maximum=5)
        retry_base = _number_setting(
            env,
            DOUBAO_RETRY_BASE_ENV,
            0.5,
            minimum=0.0,
            maximum=60.0,
        )
        if base_url:
            parsed = urlparse(base_url)
            if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
                raise ValueError(f"{DOUBAO_BASE_URL_ENV} must be an HTTPS URL without embedded credentials")
        return cls(
            base_url=base_url,
            model=model,
            timeout_seconds=timeout,
            max_retries=retries,
            retry_base_seconds=retry_base,
            _api_key=api_key,
        )

    @property
    def configured(self) -> bool:
        return bool(self._api_key and self.base_url and self.model)

    @property
    def unconfigured_reason(self) -> str | None:
        if not self._api_key:
            return "missing_api_key"
        if not self.base_url:
            return "missing_base_url"
        if not self.model:
            return "missing_model"
        return None

    def public_summary(self) -> dict[str, object]:
        return {
            "provider": DOUBAO_PROVIDER,
            "configured": self.configured,
            "base_url": self.base_url or None,
            "model": self.model or None,
            "timeout_seconds": self.timeout_seconds,
            "max_retries": self.max_retries,
            "retry_base_seconds": self.retry_base_seconds,
            "unconfigured_reason": self.unconfigured_reason,
        }


def provider_cache_key(
    *,
    provider: str,
    model_version: str,
    prompt_version: str,
    image_sha256: str,
    question_locator: object | None = None,
) -> str:
    """Return the stable provider cache key without including credentials or image bytes."""

    return payload_hash(
        {
            "provider": provider,
            "model_version": model_version,
            "prompt_version": prompt_version,
            "image_sha256": image_sha256.lower(),
            "question_locator": question_locator,
        }
    )


class DoubaoMultimodalAdapter:
    """Fail-closed, transport-injected Doubao transcription boundary.

    The adapter has no default network transport. A caller must deliberately inject one,
    which makes tests and unconfigured installations incapable of external calls.
    """

    def __init__(
        self,
        config: DoubaoConfig,
        *,
        transport: ProviderTransport,
        sleep: Callable[[float], None],
    ) -> None:
        self._config = config
        self._transport = transport
        self._sleep = sleep

    def extract(self, request: Mapping[str, Any]) -> dict[str, Any]:
        context = _request_context(request, self._config)
        if not self._config.configured:
            return self._result(
                context,
                result_status="unconfigured",
                error_summary=self._config.unconfigured_reason or "provider_unconfigured",
            )
        try:
            outbound = self._build_request(request, context)
        except (TypeError, ValueError) as exc:
            return self._result(
                context,
                result_status="failed",
                error_summary=_sanitize_error(str(exc), self._config._api_key),
            )

        request_sha256 = hashlib.sha256(outbound.body).hexdigest()
        last_response_sha256: str | None = None
        attempts = self._config.max_retries + 1
        for attempt_number in range(1, attempts + 1):
            try:
                response = self._transport(outbound)
            except (TimeoutError, socket.timeout) as exc:
                if attempt_number < attempts:
                    self._sleep(self._retry_delay(attempt_number))
                    continue
                return self._result(
                    context,
                    result_status="timeout",
                    request_sha256=request_sha256,
                    attempt_count=attempt_number,
                    error_summary=_sanitize_error(
                        f"provider_timeout: {exc}",
                        self._config._api_key,
                    ),
                )
            except Exception as exc:  # transport implementations define their own safe exception types
                return self._result(
                    context,
                    result_status="failed",
                    request_sha256=request_sha256,
                    attempt_count=attempt_number,
                    error_summary=_sanitize_error(
                        f"provider_transport_error: {exc}",
                        self._config._api_key,
                    ),
                )

            last_response_sha256 = hashlib.sha256(response.body).hexdigest()
            status = int(response.status_code)
            if status == 429:
                if attempt_number < attempts:
                    self._sleep(self._retry_delay(attempt_number))
                    continue
                return self._result(
                    context,
                    result_status="rate_limited",
                    request_sha256=request_sha256,
                    response_sha256=last_response_sha256,
                    attempt_count=attempt_number,
                    error_summary="provider_rate_limited",
                )
            if 500 <= status <= 599:
                if attempt_number < attempts:
                    self._sleep(self._retry_delay(attempt_number))
                    continue
                return self._result(
                    context,
                    result_status="failed",
                    request_sha256=request_sha256,
                    response_sha256=last_response_sha256,
                    attempt_count=attempt_number,
                    error_summary=f"provider_server_error:{status}",
                )
            if status in {401, 403}:
                return self._result(
                    context,
                    result_status="failed",
                    request_sha256=request_sha256,
                    response_sha256=last_response_sha256,
                    attempt_count=attempt_number,
                    error_summary=f"provider_auth_error:{status}",
                )
            if not 200 <= status <= 299:
                return self._result(
                    context,
                    result_status="failed",
                    request_sha256=request_sha256,
                    response_sha256=last_response_sha256,
                    attempt_count=attempt_number,
                    error_summary=f"provider_http_error:{status}",
                )
            try:
                decoded, candidate = _decode_success(response.body)
                mapped = _map_candidate(candidate)
            except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError, KeyError):
                return self._result(
                    context,
                    result_status="failed",
                    request_sha256=request_sha256,
                    response_sha256=last_response_sha256,
                    attempt_count=attempt_number,
                    error_summary="malformed_provider_response",
                )
            return self._result(
                context,
                result_status="succeeded",
                request_sha256=request_sha256,
                response_sha256=last_response_sha256,
                attempt_count=attempt_number,
                raw_output=_redact_value(decoded, self._config._api_key),
                **mapped,
            )

        # The loop always returns. Keep a fail-closed fallback for defensive completeness.
        return self._result(
            context,
            result_status="failed",
            request_sha256=request_sha256,
            response_sha256=last_response_sha256,
            attempt_count=attempts,
            error_summary="provider_attempts_exhausted",
        )

    def _build_request(
        self,
        request: Mapping[str, Any],
        context: Mapping[str, Any],
    ) -> ProviderHttpRequest:
        forbidden = _find_forbidden_fields(request)
        if forbidden:
            raise ValueError(f"forbidden provider request field(s): {', '.join(forbidden)}")
        if not context.get("prompt_version"):
            raise ValueError("prompt_version is required")
        if not context.get("question_id"):
            raise ValueError("extraction_item_id or question_id is required")
        image = request.get("image_bytes")
        if not isinstance(image, (bytes, bytearray, memoryview)) or not image:
            raise ValueError("image_bytes must contain a non-empty synthetic or source image crop")
        image_bytes = bytes(image)
        actual_image_sha = hashlib.sha256(image_bytes).hexdigest()
        supplied_image_sha = str(request.get("image_sha256") or "").strip().lower()
        if supplied_image_sha and supplied_image_sha != actual_image_sha:
            raise ValueError("image_sha256 does not match image_bytes")
        mime_type = str(request.get("mime_type") or "image/jpeg").strip().lower()
        if mime_type not in ALLOWED_IMAGE_TYPES:
            raise ValueError("mime_type is not an allowed image media type")
        locator = _safe_locator(request.get("evidence_locator"))
        instruction = {
            "task": "transcribe_student_answer",
            "prompt_version": context["prompt_version"],
            "question_locator": context["question_locator"],
            "question_type": str(request.get("question_type") or "unknown"),
            "expected_format": str(request.get("expected_format") or "free_text"),
            "evidence_locator": locator,
            "constraints": [
                "Transcribe only what is visibly written.",
                "Do not infer, grade, or correct toward an answer key.",
                "Return JSON only.",
            ],
            "response_fields": [
                "raw_transcription",
                "normalized_transcription",
                "capture_status",
                "uncertain_spans",
                "candidate_alternatives",
                "confidence",
            ],
        }
        body = {
            "model": self._config.model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": canonical_json(instruction)},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": (
                                    f"data:{mime_type};base64,"
                                    f"{base64.b64encode(image_bytes).decode('ascii')}"
                                )
                            },
                        },
                    ],
                }
            ],
            "temperature": 0,
            "response_format": {"type": "json_object"},
        }
        headers = {
            "Authorization": f"Bearer {self._config._api_key}",
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "application/json",
        }
        idempotency_key = str(request.get("idempotency_key") or "").strip()
        if idempotency_key:
            headers["X-OpenTutor-Idempotency-Key"] = idempotency_key
        return ProviderHttpRequest(
            url=self._config.base_url,
            timeout_seconds=self._config.timeout_seconds,
            body=canonical_json(body).encode("utf-8"),
            headers=headers,
        )

    def _retry_delay(self, attempt_number: int) -> float:
        return self._config.retry_base_seconds * (2 ** max(0, attempt_number - 1))

    def _result(
        self,
        context: Mapping[str, Any],
        *,
        result_status: str,
        request_sha256: str | None = None,
        response_sha256: str | None = None,
        error_summary: str | None = None,
        attempt_count: int = 0,
        raw_output: object | None = None,
        raw_transcription: str | None = None,
        normalized_transcription: str | None = None,
        capture_status: str = "needs_check",
        uncertain_spans: list[Any] | None = None,
        candidate_alternatives: list[Any] | None = None,
        confidence: float = 0.0,
    ) -> dict[str, Any]:
        if result_status not in RESULT_STATUSES:
            raise ValueError(f"unsupported provider result status: {result_status}")
        result = {
            "result_status": result_status,
            "provider": DOUBAO_PROVIDER,
            "model_version": self._config.model or None,
            "prompt_version": context.get("prompt_version"),
            "extraction_item_id": context.get("extraction_item_id"),
            "question_id": context.get("question_id"),
            "question_locator": context.get("question_locator"),
            "raw_transcription": raw_transcription,
            "normalized_transcription": normalized_transcription,
            "capture_status": capture_status,
            "uncertain_spans": uncertain_spans or [],
            "candidate_alternatives": candidate_alternatives or [],
            "confidence": confidence,
            "evidence_locator": context.get("evidence_locator") or {},
            "request_sha256": request_sha256 or context.get("request_fingerprint"),
            "response_sha256": response_sha256,
            "cache_key": context.get("cache_key"),
            "error_summary": _sanitize_error(error_summary, self._config._api_key),
            "raw_output": raw_output,
            "attempt_count": attempt_count,
            "completed_at": utc_now(),
        }
        return _redact_value(result, self._config._api_key)


def _number_setting(
    env: Mapping[str, str],
    key: str,
    default: float,
    *,
    minimum: float,
    maximum: float,
) -> float:
    raw = env.get(key)
    if raw in (None, ""):
        return default
    try:
        value = float(str(raw).strip())
    except ValueError as exc:
        raise ValueError(f"{key} must be numeric") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{key} must be between {minimum:g} and {maximum:g}")
    return value


def _integer_setting(
    env: Mapping[str, str],
    key: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    raw = env.get(key)
    if raw in (None, ""):
        return default
    try:
        value = int(str(raw).strip())
    except ValueError as exc:
        raise ValueError(f"{key} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{key} must be between {minimum} and {maximum}")
    return value


def _request_context(request: Mapping[str, Any], config: DoubaoConfig) -> dict[str, Any]:
    if not isinstance(request, Mapping):
        request = {}
    image = request.get("image_bytes")
    if isinstance(image, (bytes, bytearray, memoryview)):
        image_sha = hashlib.sha256(bytes(image)).hexdigest()
    else:
        supplied = str(request.get("image_sha256") or "").strip().lower()
        image_sha = supplied if re.fullmatch(r"[0-9a-f]{64}", supplied) else ""
    prompt_version = str(request.get("prompt_version") or "").strip()
    extraction_item_id = str(request.get("extraction_item_id") or "").strip() or None
    question_id = str(request.get("question_id") or extraction_item_id or "").strip() or None
    question_locator = request.get("question_locator") or question_id
    if not isinstance(question_locator, (str, int, float)) or isinstance(question_locator, bool):
        question_locator = question_id
    try:
        locator = _safe_locator(request.get("evidence_locator"))
    except (TypeError, ValueError):
        # Context construction must remain safe even when the provider is unconfigured.
        # Configured calls validate the locator again before any transport is invoked.
        locator = {}
    cache_key = provider_cache_key(
        provider=DOUBAO_PROVIDER,
        model_version=config.model,
        prompt_version=prompt_version,
        image_sha256=image_sha,
        question_locator=question_locator,
    )
    request_fingerprint = payload_hash(
        {
            "provider": DOUBAO_PROVIDER,
            "model_version": config.model,
            "prompt_version": prompt_version,
            "image_sha256": image_sha,
            "question_id": question_id,
            "question_locator": question_locator,
            "evidence_locator": locator,
        }
    )
    return {
        "prompt_version": prompt_version,
        "extraction_item_id": extraction_item_id,
        "question_id": question_id,
        "question_locator": question_locator,
        "evidence_locator": locator,
        "image_sha256": image_sha,
        "cache_key": cache_key,
        "request_fingerprint": request_fingerprint,
    }


def _safe_locator(value: object) -> object:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError("evidence_locator must be an object")
    result: dict[str, Any] = {}
    for key, item in value.items():
        normalized = str(key).strip().lower().replace("-", "_")
        if normalized not in SAFE_LOCATOR_FIELDS:
            continue
        if normalized in {"bbox", "crop"}:
            if not isinstance(item, (list, tuple)) or not all(
                isinstance(number, (int, float)) and not isinstance(number, bool)
                for number in item
            ):
                raise ValueError(f"evidence_locator.{normalized} must be a numeric array")
            result[normalized] = list(item)
        elif isinstance(item, (str, int, float)) and not isinstance(item, bool):
            result[normalized] = item
    return result


def _find_forbidden_fields(value: object, prefix: str = "") -> list[str]:
    found: list[str] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = str(key).strip().lower().replace("-", "_")
            path = f"{prefix}.{normalized}" if prefix else normalized
            if normalized in FORBIDDEN_REQUEST_FIELDS:
                found.append(path)
            found.extend(_find_forbidden_fields(item, path))
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            found.extend(_find_forbidden_fields(item, f"{prefix}[{index}]"))
    return sorted(set(found))


def _decode_success(body: bytes) -> tuple[object, Mapping[str, Any]]:
    decoded = json.loads(body.decode("utf-8"))
    if not isinstance(decoded, Mapping):
        raise ValueError("provider response must be an object")
    candidate: object = decoded.get("result") or decoded.get("data") or decoded
    choices = decoded.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0]
        if not isinstance(first, Mapping):
            raise ValueError("provider choice must be an object")
        message = first.get("message")
        if not isinstance(message, Mapping):
            raise ValueError("provider message is missing")
        content = message.get("content")
        if not isinstance(content, str):
            raise ValueError("provider message content must be JSON text")
        candidate = json.loads(_strip_json_fence(content))
    if not isinstance(candidate, Mapping):
        raise ValueError("provider transcription payload must be an object")
    return decoded, candidate


def _strip_json_fence(value: str) -> str:
    text = value.strip()
    if text.startswith("```") and text.endswith("```"):
        lines = text.splitlines()
        if len(lines) >= 3:
            return "\n".join(lines[1:-1]).strip()
    return text


def _map_candidate(candidate: Mapping[str, Any]) -> dict[str, Any]:
    capture_status = str(candidate.get("capture_status") or "").strip()
    if capture_status not in CAPTURE_STATUSES:
        raise ValueError("invalid capture_status")
    raw = candidate.get("raw_transcription")
    if raw is None:
        raise ValueError("raw_transcription is required")
    if not isinstance(raw, str):
        raise TypeError("raw_transcription must be text")
    if capture_status == "captured" and not raw:
        raise ValueError("captured transcription cannot be empty")
    normalized = candidate.get("normalized_transcription")
    if normalized is None:
        normalized = raw.strip()
    if not isinstance(normalized, str):
        raise TypeError("normalized_transcription must be text")
    uncertain = candidate.get("uncertain_spans", [])
    alternatives = candidate.get("candidate_alternatives", [])
    if not isinstance(uncertain, list) or not isinstance(alternatives, list):
        raise TypeError("uncertain_spans and candidate_alternatives must be arrays")
    confidence = candidate.get("confidence", 0.0)
    if isinstance(confidence, bool):
        raise TypeError("confidence must be numeric")
    confidence = float(confidence)
    if not 0 <= confidence <= 1:
        raise ValueError("confidence must be between zero and one")
    return {
        "raw_transcription": raw,
        "normalized_transcription": normalized,
        "capture_status": capture_status,
        "uncertain_spans": uncertain,
        "candidate_alternatives": alternatives,
        "confidence": confidence,
    }


def _sanitize_error(value: object, secret: str) -> str | None:
    if value in (None, ""):
        return None
    text = str(value)
    if secret:
        text = text.replace(secret, "[REDACTED]")
    text = re.sub(r"(?i)bearer\s+[^\s,;]+", "Bearer [REDACTED]", text)
    text = re.sub(
        r"(?i)(api[_-]?key|access[_-]?token|authorization)(\s*[:=]\s*)[^\s,;]+",
        r"\1\2[REDACTED]",
        text,
    )
    return text[:300]


def _redact_value(value: Any, secret: str) -> Any:
    if isinstance(value, str):
        return _redact_text(value, secret)
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            normalized = str(key).strip().lower().replace("-", "_")
            if normalized in {"api_key", "access_token", "authorization", "secret"}:
                result[str(key)] = "[REDACTED]"
            else:
                result[str(key)] = _redact_value(item, secret)
        return result
    if isinstance(value, list):
        return [_redact_value(item, secret) for item in value]
    if isinstance(value, tuple):
        return [_redact_value(item, secret) for item in value]
    return value


def _redact_text(value: str, secret: str) -> str:
    text = value
    if secret:
        text = text.replace(secret, "[REDACTED]")
    text = re.sub(r"(?i)bearer\s+[^\s,;]+", "Bearer [REDACTED]", text)
    text = re.sub(
        r"(?i)(api[_-]?key|access[_-]?token|authorization)(\s*[:=]\s*)[^\s,;]+",
        r"\1\2[REDACTED]",
        text,
    )
    return text
