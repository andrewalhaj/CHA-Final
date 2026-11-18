"""Validate one native embedding from the loopback Ollama service."""

import re
import sys

import requests

from .. import SOURCE, failures

MODEL = "qwen3-embedding:4b"
DIMENSIONS = 2560
OLLAMA = "http://127.0.0.1:11434"


class EmbeddingError(Exception):
    """A catalogued embedding failure with no request or response content."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _failure(code: str) -> EmbeddingError:
    failures.record(None, code, "failed", source=SOURCE)
    return EmbeddingError(code)


def _request(method: str, path: str, *, timeout: float, body: dict | None = None) -> dict:
    with requests.Session() as session:
        session.trust_env = False
        try:
            response = session.request(method, OLLAMA + path, json=body, timeout=timeout, allow_redirects=False)
        except requests.RequestException:
            raise _failure("embed.unavailable") from None

    if response.status_code != 200:
        raise _failure("embed.unavailable")
    try:
        data = response.json()
    except requests.exceptions.JSONDecodeError:
        raise _failure("embed.unavailable") from None
    if not isinstance(data, dict):
        raise _failure("embed.unavailable")
    return data


def model_build(*, timeout: float = 5.0) -> str:
    """The exact installed SHA-256 build for the selected model tag."""
    data = _request("GET", "/api/tags", timeout=timeout)
    models = data.get("models")
    if not isinstance(models, list):
        raise _failure("embed.unavailable")
    matches = [model.get("digest") for model in models if isinstance(model, dict) and model.get("name") == MODEL]
    if len(matches) != 1 or not isinstance(matches[0], str) or not re.fullmatch("[0-9a-f]{64}", matches[0]):
        raise _failure("embed.unavailable")
    return f"{MODEL}@sha256:{matches[0]}"


def embed(text: str, *, timeout: float = 5.0) -> list[float]:
    """One finite native-size vector, using the exact selected model and full input."""
    data = _request("POST", "/api/embed", timeout=timeout, body={"model": MODEL, "input": text, "truncate": False})
    model = data.get("model")
    vectors = data.get("embeddings")
    if not isinstance(model, str) or not isinstance(vectors, list) or len(vectors) != 1:
        raise _failure("embed.unavailable")
    vector = vectors[0]
    if not isinstance(vector, list):
        raise _failure("embed.unavailable")
    if model != MODEL or len(vector) != DIMENSIONS:
        raise _failure("embed.model_mismatch")
    if any(type(value) not in (int, float) or not -sys.float_info.max <= value <= sys.float_info.max for value in vector):
        raise _failure("embed.unavailable")
    return vector
