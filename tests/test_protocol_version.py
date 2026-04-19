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


def test_client_omitting_version_still_accepted() -> None:
    """Clients that don't send client_protocol_version are tolerated
    (keeps older clients working until MINIMUM_CLIENT is raised past
    the version they speak)."""
    mcp = build_mcp_server(App())
    r = _call(
        mcp,
        "set_player_metadata",
        connection_id="c1",
        display_name="alice",
        kind="ai",
    )
    assert r["ok"] is True
