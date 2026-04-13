"""Direct in-process tests of App + its FastMCP tool handlers.

Avoids network transport by invoking the handlers through the tool
manager API that FastMCP exposes. The HTTP+SSE glue is a separate
integration test once the full tool surface is in place.
"""

from __future__ import annotations

import asyncio

from clash_of_robots.server.app import App, build_mcp_server
from clash_of_robots.shared.protocol import ConnectionState, ErrorCode


def _call(mcp, name: str, **kwargs) -> dict:
    """Invoke a FastMCP-registered tool and return its structured payload.

    FastMCP returns a list of content blocks (TextContent with a JSON
    body) in the installed version; parse it back into a dict so
    tests can assert on the structured return shape.
    """
    import json

    blocks = asyncio.run(mcp.call_tool(name, kwargs))
    for block in blocks:
        text = getattr(block, "text", None)
        if text is not None:
            return json.loads(text)
    raise RuntimeError(f"tool {name} returned no text block: {blocks!r}")


def test_set_player_metadata_transitions_to_lobby() -> None:
    app = App()
    mcp = build_mcp_server(app)
    out = _call(
        mcp,
        "set_player_metadata",
        connection_id="c1",
        display_name="alice",
        kind="ai",
        provider="anthropic",
        model="claude-haiku-4-5",
    )
    assert out["ok"] is True
    assert out["state"] == ConnectionState.IN_LOBBY.value
    assert out["player"]["display_name"] == "alice"

    conn = app.get_connection("c1")
    assert conn is not None
    assert conn.state == ConnectionState.IN_LOBBY


def test_set_player_metadata_bad_input() -> None:
    app = App()
    mcp = build_mcp_server(app)
    out = _call(
        mcp,
        "set_player_metadata",
        connection_id="c1",
        display_name="",
        kind="ai",
    )
    assert out["ok"] is False
    assert out["error"]["code"] == ErrorCode.BAD_INPUT.value


def test_whoami_before_metadata_is_anonymous() -> None:
    app = App()
    mcp = build_mcp_server(app)
    out = _call(mcp, "whoami", connection_id="c-fresh")
    assert out["ok"] is True
    assert out["state"] == ConnectionState.ANONYMOUS.value
    assert out["player"] is None


def test_heartbeat_returns_server_time() -> None:
    app = App()
    mcp = build_mcp_server(app)
    out = _call(mcp, "heartbeat", connection_id="c1")
    assert out["ok"] is True
    assert isinstance(out["server_time"], float)


def test_heartbeat_updates_last_heartbeat_at() -> None:
    app = App()
    mcp = build_mcp_server(app)
    _call(mcp, "heartbeat", connection_id="c1")
    conn1 = app.get_connection("c1")
    assert conn1 is not None
    t1 = conn1.last_heartbeat_at

    # Sleep a tiny bit so the timestamp differs.
    import time as _time

    _time.sleep(0.01)
    _call(mcp, "heartbeat", connection_id="c1")
    conn2 = app.get_connection("c1")
    assert conn2 is not None
    assert conn2.last_heartbeat_at > t1
