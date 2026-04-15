"""Unit tests for the Codex OAuth flow + token storage.

The interactive PKCE flow needs a real browser + auth server, so we
test it end-to-end with a stubbed-out callback that posts the auth
code directly to the local listener. Token exchange + refresh hit
mocked HTTP via httpx.MockTransport.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import httpx
import pytest

from silicon_pantheon.client.providers.codex import oauth as codex_oauth


# ---- fixtures ---------------------------------------------------------


@pytest.fixture
def tmp_creds_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect CREDENTIALS_PATH to a tmp file so tests don't touch
    the real ~/.silicon-pantheon."""
    p = tmp_path / "codex-oauth.json"
    monkeypatch.setattr(codex_oauth, "CREDENTIALS_PATH", p)
    return p


# ---- pure logic --------------------------------------------------------


def test_pkce_pair_generates_valid_challenge():
    """code_challenge MUST be base64url(sha256(verifier)) without
    padding — RFC 7636. If we got this wrong, OpenAI's auth server
    would reject the code-exchange step."""
    import base64
    import hashlib

    verifier, challenge = codex_oauth._generate_pkce_pair()
    assert 43 <= len(verifier) <= 128
    expected = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode("ascii")
    assert challenge == expected


def test_authorize_url_includes_required_params():
    """Server must see response_type, client_id, redirect_uri, scope,
    code_challenge, code_challenge_method=S256, and state."""
    url = codex_oauth._build_authorize_url(
        code_challenge="abc",
        state="state-xyz",
        redirect_uri="http://127.0.0.1:1455/auth/callback",
    )
    for required in (
        "response_type=code",
        f"client_id={codex_oauth.CLIENT_ID}",
        "code_challenge=abc",
        "code_challenge_method=S256",
        "state=state-xyz",
        "redirect_uri=http%3A%2F%2F127.0.0.1%3A1455%2Fauth%2Fcallback",
    ):
        assert required in url, f"missing {required!r} in {url}"


def test_credentials_is_expired():
    """Slack window pre-empts the actual expiry so we refresh before
    the token rejects mid-call."""
    now = 1_000_000.0
    fresh = codex_oauth.CodexCredentials(
        access_token="t", refresh_token="r", expires_at=now + 3600
    )
    assert not fresh.is_expired(now=now)
    expired = codex_oauth.CodexCredentials(
        access_token="t", refresh_token="r", expires_at=now - 1
    )
    assert expired.is_expired(now=now)
    # 60s slack: token expires in 30s → considered expired now.
    near_expiry = codex_oauth.CodexCredentials(
        access_token="t", refresh_token="r", expires_at=now + 30
    )
    assert near_expiry.is_expired(now=now)


def test_persistence_roundtrip(tmp_creds_path):
    creds = codex_oauth.CodexCredentials(
        access_token="acc-123",
        refresh_token="ref-456",
        expires_at=time.time() + 3600,
        account_id="acct-789",
    )
    codex_oauth.save_credentials(creds)
    loaded = codex_oauth.load_credentials()
    assert loaded is not None
    assert loaded.access_token == "acc-123"
    assert loaded.refresh_token == "ref-456"
    assert loaded.account_id == "acct-789"
    # File perms — bearer token, must be 0600.
    assert oct(tmp_creds_path.stat().st_mode)[-3:] == "600"


def test_load_returns_none_when_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(
        codex_oauth, "CREDENTIALS_PATH", tmp_path / "missing.json"
    )
    assert codex_oauth.load_credentials() is None


def test_load_returns_none_when_malformed(tmp_creds_path):
    tmp_creds_path.write_text("{ not json", encoding="utf-8")
    assert codex_oauth.load_credentials() is None


# ---- token exchange / refresh with mocked HTTP -----------------------


@pytest.mark.asyncio
async def test_exchange_code_for_tokens_success(monkeypatch):
    """Happy path: 200 with full token body → CodexCredentials."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = request.content.decode()
        return httpx.Response(
            200,
            json={
                "access_token": "acc",
                "refresh_token": "ref",
                "expires_in": 3600,
                "id_token": "id",
                "account_id": "acct",
            },
        )

    transport = httpx.MockTransport(handler)
    # Patch httpx.AsyncClient to use our transport.
    real_async = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient",
        lambda **kw: real_async(transport=transport, **{k: v for k, v in kw.items() if k != "transport"}),
    )

    creds = await codex_oauth._exchange_code_for_tokens(
        code="auth-code", code_verifier="verifier",
        redirect_uri="http://127.0.0.1:1455/auth/callback",
    )
    assert creds.access_token == "acc"
    assert creds.refresh_token == "ref"
    assert creds.account_id == "acct"
    assert creds.expires_at > time.time() + 3500
    assert "auth-code" in captured["body"]
    assert "verifier" in captured["body"]


@pytest.mark.asyncio
async def test_refresh_access_token_success(tmp_creds_path, monkeypatch):
    """Refresh: server returns new access + (possibly new) refresh."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.content.decode()
        assert "grant_type=refresh_token" in body
        assert "refresh_token=ref-old" in body
        return httpx.Response(
            200,
            json={
                "access_token": "acc-new",
                "refresh_token": "ref-new",
                "expires_in": 7200,
            },
        )

    transport = httpx.MockTransport(handler)
    real_async = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient",
        lambda **kw: real_async(transport=transport, **{k: v for k, v in kw.items() if k != "transport"}),
    )

    old = codex_oauth.CodexCredentials(
        access_token="acc-old", refresh_token="ref-old",
        expires_at=time.time() - 1,
    )
    new = await codex_oauth.refresh_access_token(old)
    assert new.access_token == "acc-new"
    assert new.refresh_token == "ref-new"  # rotated
    # And it's persisted to disk.
    loaded = codex_oauth.load_credentials()
    assert loaded is not None
    assert loaded.access_token == "acc-new"


@pytest.mark.asyncio
async def test_refresh_failure_raises(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_grant"})

    transport = httpx.MockTransport(handler)
    real_async = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient",
        lambda **kw: real_async(transport=transport, **{k: v for k, v in kw.items() if k != "transport"}),
    )

    old = codex_oauth.CodexCredentials(
        access_token="acc", refresh_token="ref",
        expires_at=time.time() - 1,
    )
    with pytest.raises(codex_oauth.CodexAuthError):
        await codex_oauth.refresh_access_token(old)


@pytest.mark.asyncio
async def test_ensure_fresh_returns_cached_when_valid(tmp_creds_path):
    """Fresh credentials in disk → return as-is, no refresh call."""
    creds = codex_oauth.CodexCredentials(
        access_token="cached", refresh_token="r",
        expires_at=time.time() + 3600,
    )
    codex_oauth.save_credentials(creds)
    token = await codex_oauth.ensure_fresh_access_token()
    assert token == "cached"


@pytest.mark.asyncio
async def test_ensure_fresh_refreshes_when_expired(tmp_creds_path, monkeypatch):
    """Expired creds → refresh via mocked endpoint → return new
    token, write through to disk."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "access_token": "fresh",
                "refresh_token": "r2",
                "expires_in": 3600,
            },
        )
    transport = httpx.MockTransport(handler)
    real_async = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient",
        lambda **kw: real_async(transport=transport, **{k: v for k, v in kw.items() if k != "transport"}),
    )

    expired = codex_oauth.CodexCredentials(
        access_token="stale", refresh_token="r1",
        expires_at=time.time() - 60,
    )
    codex_oauth.save_credentials(expired)
    token = await codex_oauth.ensure_fresh_access_token()
    assert token == "fresh"
    # And on disk.
    loaded = codex_oauth.load_credentials()
    assert loaded is not None and loaded.access_token == "fresh"


@pytest.mark.asyncio
async def test_ensure_fresh_no_credentials_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(
        codex_oauth, "CREDENTIALS_PATH", tmp_path / "nope.json"
    )
    with pytest.raises(codex_oauth.CodexAuthError):
        await codex_oauth.ensure_fresh_access_token()


# ---- callback listener (port-scoped end-to-end) -----------------------


@pytest.mark.asyncio
async def test_callback_listener_captures_code():
    """Spin up the callback server on a free port, fire a fake browser
    redirect at it, assert we extracted the code + state."""
    import socket

    # Pick a free port to avoid clashing with anything else on the
    # box (or another test running concurrently).
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    expected_state = "state-1"
    listener_task = asyncio.create_task(
        codex_oauth._wait_for_callback(
            expected_state=expected_state, port=port, timeout_s=5.0,
        )
    )
    # Give the listener a moment to bind.
    await asyncio.sleep(0.1)

    async with httpx.AsyncClient() as client:
        r = await client.get(
            f"http://127.0.0.1:{port}/auth/callback?code=THE_CODE&state={expected_state}"
        )
        assert r.status_code == 200
        assert "Logged in" in r.text

    result = await listener_task
    assert result.code == "THE_CODE"
    assert result.state == expected_state
    assert result.error is None


@pytest.mark.asyncio
async def test_callback_listener_times_out():
    import socket

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    result = await codex_oauth._wait_for_callback(
        expected_state="x", port=port, timeout_s=0.5,
    )
    assert result.error == "timeout"
    assert result.code is None
