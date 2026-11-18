"""Exercise the local Ollama adapter without opening a listening socket."""

import json
import os
import unittest
from unittest.mock import patch

import requests

from custom_build.memory import embedding

MODEL = "qwen3-embedding:4b"
DIGEST = "0123456789abcdef" * 4
TEXT = "Synthetic private probe."
VECTOR = [0.125] * 2560


def reply(body, *, status=200, location=None):
    response = requests.Response()
    response.status_code = status
    response._content = json.dumps(body).encode("utf-8")
    response._content_consumed = True
    if location:
        response.headers["Location"] = location
    return response


class LocalEmbeddingTests(unittest.TestCase):
    def assert_failure(self, operation, response, code):
        kwargs = {"side_effect": response} if isinstance(response, BaseException) else {"return_value": response}
        with (
            patch.object(requests.adapters.HTTPAdapter, "send", **kwargs) as sent,
            self.assertLogs("custom_build.failures", level="WARNING") as logs,
            self.assertRaises(embedding.EmbeddingError) as caught,
        ):
            operation()
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(str(caught.exception), code)
        self.assertTrue(any(json.loads(r.getMessage())["code"] == code for r in logs.records))
        self.assertNotIn(TEXT, "\n".join(logs.output))
        sent.assert_called_once()

    def test_build_reports_exact_digest_for_the_selected_tag(self):
        models = {"models": [
            {"name": "qwen3-embedding:0.6b", "digest": "f" * 64},
            {"name": MODEL, "digest": DIGEST},
        ]}
        with patch.object(requests.adapters.HTTPAdapter, "send", return_value=reply(models)) as sent:
            self.assertEqual(embedding.model_build(timeout=2), f"{MODEL}@sha256:{DIGEST}")
        request, = sent.call_args.args
        self.assertEqual((request.method, request.url), ("GET", "http://127.0.0.1:11434/api/tags"))
        self.assertEqual(sent.call_args.kwargs["timeout"], 2)

    def test_one_native_vector_uses_only_loopback_without_environment_proxy(self):
        answer = reply({"model": MODEL, "embeddings": [VECTOR]})
        with (
            patch.dict(os.environ, {"HTTP_PROXY": "http://proxy.example.invalid:1234", "NO_PROXY": ""}),
            patch.object(requests.adapters.HTTPAdapter, "send", return_value=answer) as sent,
        ):
            self.assertEqual(embedding.embed(TEXT, timeout=3), VECTOR)
        sent.assert_called_once()
        request, = sent.call_args.args
        self.assertEqual((request.method, request.url), ("POST", "http://127.0.0.1:11434/api/embed"))
        self.assertEqual(json.loads(request.body), {"model": MODEL, "input": TEXT, "truncate": False})
        self.assertEqual(sent.call_args.kwargs["timeout"], 3)
        self.assertFalse(sent.call_args.kwargs["proxies"])

    def test_refuses_a_redirect_without_sending_the_input_again(self):
        redirect = reply({"error": TEXT}, status=307, location="http://example.invalid/")
        self.assert_failure(lambda: embedding.embed(TEXT), redirect, "embed.unavailable")

    def test_network_failures_are_named_without_logging_client_details(self):
        for failure in (requests.exceptions.Timeout(TEXT), requests.exceptions.ConnectionError(TEXT)):
            with self.subTest(error=type(failure).__name__):
                self.assert_failure(embedding.model_build, failure, "embed.unavailable")

    def test_unusable_model_list_does_not_produce_an_identity(self):
        for body in (
            {"models": []},
            {"models": [{"name": MODEL, "digest": "malformed"}]},
            {"models": [{"name": MODEL, "digest": DIGEST}, {"name": MODEL, "digest": "f" * 64}]},
            {"models": "wrong type"},
        ):
            with self.subTest(body=body):
                self.assert_failure(embedding.model_build, reply(body), "embed.unavailable")

    def test_invalid_json_and_non_success_status_are_named(self):
        malformed = reply({})
        malformed._content = TEXT.encode()
        for response in (malformed, reply({"error": TEXT}, status=503)):
            with self.subTest(status=response.status_code):
                self.assert_failure(lambda: embedding.embed(TEXT), response, "embed.unavailable")

    def test_refuses_a_different_model_or_vector_size(self):
        for body in (
            {"model": "qwen3-embedding:0.6b", "embeddings": [VECTOR]},
            {"model": MODEL, "embeddings": [VECTOR[:-1]]},
        ):
            with self.subTest(model=body["model"]):
                self.assert_failure(lambda: embedding.embed(TEXT), reply(body), "embed.model_mismatch")

    def test_rejects_missing_multiple_or_invalid_vectors(self):
        for vectors in ([], [VECTOR, VECTOR], [None]):
            with self.subTest(count=len(vectors)):
                self.assert_failure(lambda: embedding.embed(TEXT), reply({"model": MODEL, "embeddings": vectors}),
                                    "embed.unavailable")
        for value in (True, "0.125", None, float("nan"), float("inf"), 10**400):
            with self.subTest(value=value):
                bad_vector = [value, *VECTOR[1:]]
                self.assert_failure(lambda: embedding.embed(TEXT), reply({"model": MODEL, "embeddings": [bad_vector]}),
                                    "embed.unavailable")
