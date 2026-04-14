"""Tests for the provider error classifier."""

from __future__ import annotations

import pytest

from silicon_pantheon.client.providers.errors import (
    ProviderError,
    ProviderErrorReason,
    classify,
)


class _FakeHTTPError(Exception):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code


def test_401_is_auth() -> None:
    err = classify(_FakeHTTPError(401, "Invalid API key"))
    assert err.reason == ProviderErrorReason.AUTH
    assert err.is_terminal


def test_403_is_auth_permanent() -> None:
    err = classify(_FakeHTTPError(403, "Permission denied"))
    assert err.reason == ProviderErrorReason.AUTH_PERMANENT
    assert err.is_terminal


def test_402_is_billing() -> None:
    err = classify(_FakeHTTPError(402, "insufficient funds"))
    assert err.reason == ProviderErrorReason.BILLING
    assert err.is_terminal


def test_429_is_rate_limit() -> None:
    err = classify(_FakeHTTPError(429, "rate limit exceeded"))
    assert err.reason == ProviderErrorReason.RATE_LIMIT
    assert not err.is_terminal


def test_503_is_overloaded() -> None:
    err = classify(_FakeHTTPError(503, "overloaded"))
    assert err.reason == ProviderErrorReason.OVERLOADED


def test_404_model_not_found() -> None:
    err = classify(_FakeHTTPError(404, "model does not exist"))
    assert err.reason == ProviderErrorReason.MODEL_NOT_FOUND
    assert err.is_terminal


def test_timeout_class_name() -> None:
    class _MyTimeoutError(Exception):
        pass

    err = classify(_MyTimeoutError("socket disconnected"))
    assert err.reason == ProviderErrorReason.TIMEOUT


def test_unknown_exception_caught() -> None:
    err = classify(RuntimeError("something exotic"))
    assert err.reason == ProviderErrorReason.UNKNOWN
    assert not err.is_terminal


def test_provider_error_passes_through_unchanged() -> None:
    original = ProviderError(ProviderErrorReason.BILLING, "out of credit")
    assert classify(original) is original


def test_original_exception_preserved() -> None:
    raw = _FakeHTTPError(429, "slow down")
    err = classify(raw)
    assert err.original is raw


class _BadRequestErrorWithBody(Exception):
    """Mimics the openai SDK's BadRequestError shape: status_code +
    body={"error": {"message": "..."}} on a 400."""

    def __init__(self, body_message: str) -> None:
        super().__init__(f"Error code: 400 - {body_message}")
        self.status_code = 400
        self.body = {"error": {"message": body_message, "type": "bad_request"}}


def test_400_detail_is_surfaced_to_user() -> None:
    """Regression: "format: bad request" used to be all the user
    saw when the provider rejected a call — no information about
    WHY. Now the SDK's error.body.error.message is appended so
    the TUI footer reads something like
    'format: bad request: tool_calls[0].function.arguments not
    valid JSON'."""
    err = classify(
        _BadRequestErrorWithBody(
            "tool_calls[0].function.arguments is not valid JSON"
        )
    )
    assert err.reason == ProviderErrorReason.FORMAT
    # The concrete reason must be in the surfaced string.
    assert "not valid JSON" in str(err)
    # The category label should still lead so error-classification
    # upstream still works.
    assert str(err).startswith("format:")


def test_400_with_only_str_body_still_includes_detail() -> None:
    """Providers that don't use the openai body shape should still
    surface str(exc) in the detail — fallback path."""
    err = classify(_FakeHTTPError(400, "context length exceeded: 123456 tokens"))
    assert err.reason == ProviderErrorReason.FORMAT
    assert "context length exceeded" in str(err)
