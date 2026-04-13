"""Tests for the in-memory TokenRegistry."""

from __future__ import annotations

import time

from clash_of_robots.server.auth import TokenIdentity, TokenRegistry


def test_issue_and_resolve() -> None:
    reg = TokenRegistry()
    ident = TokenIdentity(room_id="r1", slot="a")
    token = reg.issue(ident)
    assert reg.resolve(token) == ident


def test_token_is_random_and_long() -> None:
    reg = TokenRegistry()
    t1 = reg.issue(TokenIdentity("r1", "a"))
    t2 = reg.issue(TokenIdentity("r1", "b"))
    assert t1 != t2
    assert len(t1) == 64  # 32 bytes hex-encoded


def test_resolve_unknown_returns_none() -> None:
    reg = TokenRegistry()
    assert reg.resolve("") is None
    assert reg.resolve("not-a-token") is None


def test_revoke() -> None:
    reg = TokenRegistry()
    token = reg.issue(TokenIdentity("r1", "a"))
    assert reg.revoke(token) is True
    assert reg.resolve(token) is None
    # Second revoke is a no-op.
    assert reg.revoke(token) is False


def test_revoke_all_for_room() -> None:
    reg = TokenRegistry()
    reg.issue(TokenIdentity("r1", "a"))
    reg.issue(TokenIdentity("r1", "b"))
    reg.issue(TokenIdentity("r2", "a"))
    n = reg.revoke_all_for(room_id="r1")
    assert n == 2
    assert len(reg) == 1


def test_ttl_expires(monkeypatch) -> None:
    reg = TokenRegistry()
    token = reg.issue(TokenIdentity("r1", "a"), ttl_seconds=60)
    assert reg.resolve(token) is not None
    # Fast-forward clock past expiry.
    real_time = time.time
    monkeypatch.setattr(time, "time", lambda: real_time() + 3600)
    assert reg.resolve(token) is None
    # Purged on access.
    assert len(reg) == 0
