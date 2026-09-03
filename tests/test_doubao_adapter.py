from __future__ import annotations

import hashlib
import json
import unittest

from english_tracker.providers import (
    DOUBAO_API_KEY_ENV,
    DOUBAO_BASE_URL_ENV,
    DOUBAO_MAX_RETRIES_ENV,
    DOUBAO_MODEL_ENV,
    DOUBAO_RETRY_BASE_ENV,
    DOUBAO_TIMEOUT_ENV,
    DoubaoConfig,
    DoubaoMultimodalAdapter,
    ProviderHttpRequest,
    ProviderHttpResponse,
    provider_cache_key,
)


SECRET = "test-doubao-secret-that-must-never-leak"
IMAGE = b"synthetic-image-crop-with-no-learner-data"


class SequenceTransport:
    def __init__(self, *results: ProviderHttpResponse | Exception):
        self.results = list(results)
        self.calls: list[ProviderHttpRequest] = []

    def __call__(self, request: ProviderHttpRequest) -> ProviderHttpResponse:
        self.calls.append(request)
        if not self.results:
            raise AssertionError("unexpected provider transport call")
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def configured_env(**overrides: str) -> dict[str, str]:
    values = {
        DOUBAO_API_KEY_ENV: SECRET,
        DOUBAO_BASE_URL_ENV: "https://doubao.invalid/v1/chat/completions",
        DOUBAO_MODEL_ENV: "doubao-test-model",
        DOUBAO_TIMEOUT_ENV: "12.5",
        DOUBAO_MAX_RETRIES_ENV: "2",
        DOUBAO_RETRY_BASE_ENV: "0.25",
    }
    values.update(overrides)
    return values


def extraction_request(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "extraction_item_id": "EXT-ITEM-001",
        "question_id": "QUESTION-001",
        "question_locator": "page-1-question-7",
        "question_type": "translation",
        "expected_format": "free_text",
        "prompt_version": "transcription-v1",
        "image_bytes": IMAGE,
        "image_sha256": hashlib.sha256(IMAGE).hexdigest(),
        "mime_type": "image/png",
        "evidence_locator": {
            "page_number": 1,
            "question_label": "7",
            "bbox": [10, 20, 300, 120],
        },
        "idempotency_key": "provider:test:001",
    }
    values.update(overrides)
    return values


def successful_response(
    *,
    raw: str = "The student wrote this sentence.",
    normalized: str | None = "The student wrote this sentence.",
    capture_status: str = "captured",
    extra: dict[str, object] | None = None,
) -> ProviderHttpResponse:
    payload: dict[str, object] = {
        "raw_transcription": raw,
        "capture_status": capture_status,
        "uncertain_spans": [{"start": 4, "end": 11, "text": "student"}],
        "candidate_alternatives": ["The learner wrote this sentence."],
        "confidence": 0.91,
    }
    if normalized is not None:
        payload["normalized_transcription"] = normalized
    if extra:
        payload.update(extra)
    return ProviderHttpResponse(
        status_code=200,
        body=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )


class DoubaoConfigTest(unittest.TestCase):
    def test_only_namespaced_environment_is_read_and_secret_is_never_public(self):
        ignored = DoubaoConfig.from_env(
            {
                "DOUBAO_API_KEY": "generic-key-must-be-ignored",
                "ARK_API_KEY": "vendor-key-must-be-ignored",
                DOUBAO_BASE_URL_ENV: "https://doubao.invalid/v1",
                DOUBAO_MODEL_ENV: "model-a",
            }
        )
        self.assertFalse(ignored.configured)
        self.assertEqual(ignored.unconfigured_reason, "missing_api_key")

        config = DoubaoConfig.from_env(configured_env())
        self.assertTrue(config.configured)
        rendered = repr(config) + json.dumps(config.public_summary())
        self.assertNotIn(SECRET, rendered)
        self.assertNotIn("api_key", config.public_summary())
        self.assertEqual(config.timeout_seconds, 12.5)
        self.assertEqual(config.max_retries, 2)

    def test_config_validation_does_not_echo_secret(self):
        with self.assertRaises(ValueError) as invalid_url:
            DoubaoConfig.from_env(
                configured_env(
                    **{DOUBAO_BASE_URL_ENV: f"https://user:{SECRET}@doubao.invalid/v1"}
                )
            )
        self.assertNotIn(SECRET, str(invalid_url.exception))
        with self.assertRaisesRegex(ValueError, DOUBAO_TIMEOUT_ENV):
            DoubaoConfig.from_env(configured_env(**{DOUBAO_TIMEOUT_ENV: "not-a-number"}))
        with self.assertRaisesRegex(ValueError, DOUBAO_MAX_RETRIES_ENV):
            DoubaoConfig.from_env(configured_env(**{DOUBAO_MAX_RETRIES_ENV: "99"}))


class DoubaoMultimodalAdapterTest(unittest.TestCase):
    def adapter(
        self,
        transport: SequenceTransport,
        *,
        sleeps: list[float] | None = None,
        env: dict[str, str] | None = None,
    ) -> DoubaoMultimodalAdapter:
        recorded = sleeps if sleeps is not None else []
        return DoubaoMultimodalAdapter(
            DoubaoConfig.from_env(env or configured_env()),
            transport=transport,
            sleep=recorded.append,
        )

    def test_missing_api_key_is_explicitly_unconfigured_and_never_calls_transport(self):
        transport = SequenceTransport(successful_response())
        env = configured_env()
        env.pop(DOUBAO_API_KEY_ENV)
        result = self.adapter(transport, env=env).extract(extraction_request())
        self.assertEqual(result["result_status"], "unconfigured")
        self.assertEqual(result["error_summary"], "missing_api_key")
        self.assertEqual(len(result["request_sha256"]), 64)
        self.assertEqual(transport.calls, [])
        self.assertNotIn(SECRET, json.dumps(result))

    def test_success_maps_minimum_provider_result_and_whitelists_outbound_request(self):
        response = successful_response(extra={"echoed_authorization": f"Bearer {SECRET}"})
        transport = SequenceTransport(response)
        adapter = self.adapter(transport)
        request = extraction_request(
            student_id="STU-001",
            batch_internal_note="must-not-be-sent",
            teacher_note="must-not-be-sent",
        )
        result = adapter.extract(request)

        self.assertEqual(result["result_status"], "succeeded")
        self.assertEqual(result["provider"], "doubao")
        self.assertEqual(result["model_version"], "doubao-test-model")
        self.assertEqual(result["prompt_version"], "transcription-v1")
        self.assertEqual(result["extraction_item_id"], "EXT-ITEM-001")
        self.assertEqual(result["question_id"], "QUESTION-001")
        self.assertEqual(result["capture_status"], "captured")
        self.assertEqual(result["raw_transcription"], "The student wrote this sentence.")
        self.assertEqual(result["normalized_transcription"], "The student wrote this sentence.")
        self.assertEqual(result["confidence"], 0.91)
        self.assertEqual(result["attempt_count"], 1)
        self.assertRegex(result["completed_at"], r"^\d{4}-\d{2}-\d{2}T")
        self.assertEqual(len(result["request_sha256"]), 64)
        self.assertEqual(len(result["response_sha256"]), 64)
        self.assertEqual(len(result["cache_key"]), 64)

        outbound = transport.calls[0]
        outbound_payload = json.loads(outbound.body)
        outbound_text = json.dumps(outbound_payload)
        self.assertEqual(outbound.headers["Authorization"], f"Bearer {SECRET}")
        self.assertNotIn(SECRET, repr(outbound))
        self.assertNotIn("STU-001", outbound_text)
        self.assertNotIn("must-not-be-sent", outbound_text)
        self.assertNotIn("standard_answer", outbound_text)
        self.assertNotIn("other_provider_results", outbound_text)
        self.assertNotIn("student_name", outbound_text)
        self.assertNotIn("private_path", outbound_text)
        self.assertEqual(
            result["request_sha256"],
            hashlib.sha256(outbound.body).hexdigest(),
        )
        rendered_result = json.dumps(result)
        self.assertNotIn(SECRET, rendered_result)
        self.assertIn("[REDACTED]", rendered_result)

    def test_forbidden_fields_fail_closed_without_transport(self):
        for field in (
            "standard_answer",
            "other_provider_results",
            "student_name",
            "private_path",
        ):
            with self.subTest(field=field):
                transport = SequenceTransport(successful_response())
                result = self.adapter(transport).extract(
                    extraction_request(**{field: f"sensitive-{field}"})
                )
                self.assertEqual(result["result_status"], "failed")
                self.assertIn(field, result["error_summary"])
                self.assertNotIn(f"sensitive-{field}", result["error_summary"])
                self.assertEqual(transport.calls, [])

        nested_transport = SequenceTransport(successful_response())
        nested = extraction_request(metadata={"standard_answer": "never-send-this"})
        nested_result = self.adapter(nested_transport).extract(nested)
        self.assertEqual(nested_result["result_status"], "failed")
        self.assertEqual(nested_transport.calls, [])

    def test_request_validation_prevents_network_on_bad_image_hash_or_missing_prompt(self):
        for request in (
            extraction_request(image_sha256="0" * 64),
            extraction_request(prompt_version=""),
            extraction_request(mime_type="application/octet-stream"),
        ):
            with self.subTest(request=request):
                transport = SequenceTransport(successful_response())
                result = self.adapter(transport).extract(request)
                self.assertEqual(result["result_status"], "failed")
                self.assertEqual(transport.calls, [])

    def test_openai_compatible_fenced_json_content_is_supported(self):
        candidate = {
            "raw_transcription": " hand written ",
            "normalized_transcription": "hand written",
            "capture_status": "captured",
            "uncertain_spans": [],
            "candidate_alternatives": [],
            "confidence": 0.8,
        }
        body = {
            "choices": [
                {
                    "message": {
                        "content": "```json\n" + json.dumps(candidate) + "\n```"
                    }
                }
            ]
        }
        transport = SequenceTransport(
            ProviderHttpResponse(status_code=200, body=json.dumps(body).encode("utf-8"))
        )
        result = self.adapter(transport).extract(extraction_request())
        self.assertEqual(result["result_status"], "succeeded")
        self.assertEqual(result["raw_transcription"], " hand written ")
        self.assertEqual(result["normalized_transcription"], "hand written")

    def test_long_transcription_is_not_truncated_by_secret_redaction(self):
        long_text = "word " * 250
        transport = SequenceTransport(successful_response(raw=long_text, normalized=long_text))
        result = self.adapter(transport).extract(extraction_request())
        self.assertEqual(result["result_status"], "succeeded")
        self.assertEqual(result["raw_transcription"], long_text)

    def test_timeout_retries_with_injected_sleep_then_reports_timeout(self):
        sleeps: list[float] = []
        transport = SequenceTransport(
            TimeoutError(f"timeout with {SECRET}"),
            TimeoutError("timeout two"),
            TimeoutError("timeout three"),
        )
        result = self.adapter(transport, sleeps=sleeps).extract(extraction_request())
        self.assertEqual(result["result_status"], "timeout")
        self.assertEqual(result["attempt_count"], 3)
        self.assertEqual(len(transport.calls), 3)
        self.assertEqual(sleeps, [0.25, 0.5])
        self.assertNotIn(SECRET, result["error_summary"])

    def test_retryable_timeout_can_recover(self):
        sleeps: list[float] = []
        transport = SequenceTransport(TimeoutError("temporary"), successful_response())
        result = self.adapter(transport, sleeps=sleeps).extract(extraction_request())
        self.assertEqual(result["result_status"], "succeeded")
        self.assertEqual(result["attempt_count"], 2)
        self.assertEqual(sleeps, [0.25])

    def test_rate_limit_and_server_errors_have_bounded_retries(self):
        rate_sleeps: list[float] = []
        rate = SequenceTransport(
            ProviderHttpResponse(429, b'{"error":"slow down"}'),
            ProviderHttpResponse(429, b'{"error":"slow down"}'),
            ProviderHttpResponse(429, b'{"error":"slow down"}'),
        )
        rate_result = self.adapter(rate, sleeps=rate_sleeps).extract(extraction_request())
        self.assertEqual(rate_result["result_status"], "rate_limited")
        self.assertEqual(len(rate.calls), 3)
        self.assertEqual(rate_sleeps, [0.25, 0.5])

        server_sleeps: list[float] = []
        server = SequenceTransport(
            ProviderHttpResponse(503, b"unavailable"),
            ProviderHttpResponse(503, b"unavailable"),
            ProviderHttpResponse(503, b"unavailable"),
        )
        server_result = self.adapter(server, sleeps=server_sleeps).extract(
            extraction_request()
        )
        self.assertEqual(server_result["result_status"], "failed")
        self.assertEqual(server_result["error_summary"], "provider_server_error:503")
        self.assertEqual(len(server.calls), 3)
        self.assertEqual(server_sleeps, [0.25, 0.5])

    def test_auth_errors_are_not_retried_or_misreported_as_success(self):
        for status in (401, 403):
            with self.subTest(status=status):
                transport = SequenceTransport(
                    ProviderHttpResponse(status, f"denied {SECRET}".encode("utf-8")),
                    successful_response(),
                )
                result = self.adapter(transport).extract(extraction_request())
                self.assertEqual(result["result_status"], "failed")
                self.assertEqual(result["error_summary"], f"provider_auth_error:{status}")
                self.assertEqual(len(transport.calls), 1)
                self.assertNotIn(SECRET, json.dumps(result))

    def test_malformed_success_responses_never_become_provider_success(self):
        responses = (
            ProviderHttpResponse(200, b"not-json"),
            ProviderHttpResponse(200, b'{"capture_status":"captured"}'),
            ProviderHttpResponse(
                200,
                b'{"raw_transcription":"x","capture_status":"captured","confidence":2}',
            ),
            ProviderHttpResponse(
                200,
                b'{"raw_transcription":"x","capture_status":"invented","confidence":0.5}',
            ),
        )
        for response in responses:
            with self.subTest(body=response.body):
                transport = SequenceTransport(response, successful_response())
                result = self.adapter(transport).extract(extraction_request())
                self.assertEqual(result["result_status"], "failed")
                self.assertEqual(result["error_summary"], "malformed_provider_response")
                self.assertEqual(len(transport.calls), 1)

    def test_transport_exception_summary_is_redacted_and_not_retried(self):
        transport = SequenceTransport(
            RuntimeError(f"Authorization: Bearer {SECRET}; api_key={SECRET}")
        )
        result = self.adapter(transport).extract(extraction_request())
        self.assertEqual(result["result_status"], "failed")
        self.assertEqual(len(transport.calls), 1)
        self.assertNotIn(SECRET, json.dumps(result))
        self.assertIn("[REDACTED]", result["error_summary"])

    def test_cache_key_changes_with_provider_model_prompt_or_image(self):
        base = {
            "provider": "doubao",
            "model_version": "model-a",
            "prompt_version": "prompt-a",
            "image_sha256": "a" * 64,
            "question_locator": "Q1",
        }
        original = provider_cache_key(**base)
        variants = []
        for key, value in (
            ("provider", "another-provider"),
            ("model_version", "model-b"),
            ("prompt_version", "prompt-b"),
            ("image_sha256", "b" * 64),
        ):
            changed = dict(base)
            changed[key] = value
            variants.append(provider_cache_key(**changed))
        self.assertEqual(len(original), 64)
        self.assertEqual(len(set([original, *variants])), 5)


if __name__ == "__main__":
    unittest.main()
