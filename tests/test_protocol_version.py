"""Tests for the client/server protocol-version handshake."""

from __future__ import annotations

import asyncio
import json

from silicon_pantheon.server.app import App, build_mcp_server
from silicon_pantheon.shared.protocol import (
    MINIMUM_CLIENT_PROTOCOL_VERSION,
    PROTOCOL_VERSION,
    ErrorCode,
)


def _call(mcp, name: str, **kwargs) -> dict:
    blocks = asyncio.run(mcp.call_tool(name, kwargs))
    for block in blocks:
        text = getattr(block, "text", None)
        if text is not None:
            return json.loads(text)
    raise RuntimeError(f"tool {name} returned no text block: {blocks!r}")


def test_server_reports_protocol_version_in_response() -> None:
    mcp = build_mcp_server(App())
    r = _call(
        mcp,
        "set_player_metadata",
        connection_id="c1",
        display_name="alice",
        kind="ai",
    )
    assert r["ok"] is True
    assert r["server_protocol_version"] == PROTOCOL_VERSION
    assert r["minimum_client_protocol_version"] == MINIMUM_CLIENT_PROTOCOL_VERSION


def test_client_with_matching_version_accepted() -> None:
    mcp = build_mcp_server(App())
    r = _call(
        mcp,
        "set_player_metadata",
        connection_id="c1",
        display_name="alice",
        kind="ai",
        client_protocol_version=PROTOCOL_VERSION,
    )
    assert r["ok"] is True


def test_client_below_minimum_refused() -> None:
    """Clients whose protocol version is below the server's minimum
    supported version get CLIENT_TOO_OLD and are expected to upgrade."""
    import silicon_pantheon.server.app as srv_app
    original_min = srv_app.MINIMUM_CLIENT_PROTOCOL_VERSION
    srv_app.MINIMUM_CLIENT_PROTOCOL_VERSION = 5
    try:
        mcp = build_mcp_server(App())
        r = _call(
            mcp,
            "set_player_metadata",
            connection_id="c1",
            display_name="alice",
            kind="ai",
            client_protocol_version=3,
        )
        assert r["ok"] is False
        assert r["error"]["code"] == ErrorCode.CLIENT_TOO_OLD.value
        data = r["error"].get("data") or {}
        assert data.get("minimum_client_protocol_version") == 5
        assert data.get("client_protocol_version") == 3
        assert "upgrade_command" in data
    finally:
        srv_app.MINIMUM_CLIENT_PROTOCOL_VERSION = original_min


def test_client_above_server_version_accepted() -> None:
    """Newer clients talking to older servers are accepted (the
    server stays operational; the client is responsible for not
    using features the server doesn't advertise)."""
    mcp = build_mcp_server(App())
    r = _call(
        mcp,
        "set_player_metadata",
        connection_id="c1",
        display_name="alice",
        kind="ai",
        client_protocol_version=PROTOCOL_VERSION + 5,
    )
    assert r["ok"] is True


def test_client_omitting_version_treated_as_v1() -> None:
    """A client that doesn't send client_protocol_version is treated
    as v1 (the pre-handshake-aware behavior). At MIN=1 that's
    accepted; when MIN > 1 it's rejected — covered by the next test."""
    mcp = build_mcp_server(App())
    r = _call(
        mcp,
        "set_player_metadata",
        connection_id="c1",
        display_name="alice",
        kind="ai",
    )
    assert r["ok"] is True


def test_client_omitting_version_rejected_once_minimum_exceeds_v1() -> None:
    """Regression guard for Phase 4 of the breaking-change rollout:
    once MINIMUM_CLIENT_PROTOCOL_VERSION is raised above 1, a client
    that doesn't send client_protocol_version at all (pre-handshake-
    aware) falls to the effective v1 baseline and gets CLIENT_TOO_OLD,
    not a silent pass. See docs/VERSIONING.md."""
    import silicon_pantheon.server.app as srv_app
    original_min = srv_app.MINIMUM_CLIENT_PROTOCOL_VERSION
    srv_app.MINIMUM_CLIENT_PROTOCOL_VERSION = 2
    try:
        mcp = build_mcp_server(App())
        r = _call(
            mcp,
            "set_player_metadata",
            connection_id="c1",
            display_name="alice",
            kind="ai",
            # client_protocol_version deliberately omitted
        )
        assert r["ok"] is False
        assert r["error"]["code"] == ErrorCode.CLIENT_TOO_OLD.value
        assert r["error"]["data"]["client_protocol_version"] == 1
    finally:
        srv_app.MINIMUM_CLIENT_PROTOCOL_VERSION = original_min


def test_client_sending_stringified_version_is_parsed() -> None:
    """Legacy callers that sent client_protocol_version as a
    stringified number ('1') should still be interpreted correctly
    rather than silently falling back to v1-via-parse-failure."""
    mcp = build_mcp_server(App())
    r = _call(
        mcp,
        "set_player_metadata",
        connection_id="c1",
        display_name="alice",
        kind="ai",
        client_protocol_version="1",
    )
    assert r["ok"] is True


def test_connection_records_client_protocol_version() -> None:
    """The server must retain the client's version on the Connection
    object so later tool handlers can branch their wire shape during
    a compat-shim window."""
    app = App()
    mcp = build_mcp_server(app)
    r = _call(
        mcp,
        "set_player_metadata",
        connection_id="c1",
        display_name="alice",
        kind="ai",
        client_protocol_version=PROTOCOL_VERSION,
    )
    assert r["ok"] is True
    conn = app.get_connection("c1")
    assert conn is not None
    assert conn.client_protocol_version == PROTOCOL_VERSION


def test_reauth_without_version_does_not_regress_stored_version() -> None:
    """A client that first sends v2 and later re-calls
    set_player_metadata WITHOUT the version arg (e.g. through an
    older code path) must not have its stored version downgraded —
    tool handlers that branch on `>= 2` would otherwise start
    emitting the old-shape response to a v2 client."""
    app = App()
    mcp = build_mcp_server(app)
    # First call: explicit v2.
    r1 = _call(
        mcp,
        "set_player_metadata",
        connection_id="c1",
        display_name="alice",
        kind="ai",
        client_protocol_version=2,
    )
    assert r1["ok"] is True
    conn = app.get_connection("c1")
    assert conn is not None
    assert conn.client_protocol_version == 2
    # Second call: no version arg (tolerated but should NOT downgrade).
    r2 = _call(
        mcp,
        "set_player_metadata",
        connection_id="c1",
        display_name="alice-updated",
        kind="ai",
    )
    assert r2["ok"] is True
    assert conn.client_protocol_version == 2, (
        "unversioned re-call must not regress the recorded version"
    )


def test_server_without_version_field_rejected_when_min_server_raised() -> None:
    """Client-side guard: if a server response lacks
    server_protocol_version entirely (legacy or buggy server) AND the
    client's MINIMUM_SERVER_PROTOCOL_VERSION is >= 1, the client
    treats it as v0 and raises server_too_old — not a silent pass."""
    # We can exercise the client's check directly without a running
    # server by simulating the parsed response.
    from silicon_pantheon.client.tui.screens.login import (
        VersionMismatchError,
    )
    # Mimic the client's guard logic from _connect_and_declare.
    import silicon_pantheon.shared.protocol as proto

    def client_guard(r: dict, min_server: int) -> None:
        result = r.get("result") or r
        try:
            server_version = int(result.get("server_protocol_version") or 0)
        except (TypeError, ValueError):
            server_version = 0
        if server_version < min_server:
            raise VersionMismatchError(
                kind="server_too_old",
                message=(
                    f"Server is on protocol v{server_version} but this client "
                    f"requires at least v{min_server}."
                ),
                data={
                    "server_protocol_version": server_version,
                    "minimum_server_protocol_version": min_server,
                },
            )

    # Missing field, MIN=1 → must raise.
    import pytest
    with pytest.raises(VersionMismatchError) as exc:
        client_guard({"ok": True}, min_server=1)
    assert exc.value.kind == "server_too_old"
    assert exc.value.data["server_protocol_version"] == 0
    # Explicit v0, MIN=1 → must raise.
    with pytest.raises(VersionMismatchError):
        client_guard({"ok": True, "server_protocol_version": 0}, min_server=1)
    # v1 meets MIN=1 → passes silently.
    client_guard({"ok": True, "server_protocol_version": 1}, min_server=1)
    # v2 meets MIN=1 → passes silently.
    client_guard({"ok": True, "server_protocol_version": 2}, min_server=1)
