"""Direct in-process tests of App + its FastMCP tool handlers.

Avoids network transport by invoking the handlers through the tool
manager API that FastMCP exposes. The HTTP+SSE glue is a separate
integration test once the full tool surface is in place.
"""

from __future__ import annotations

import asyncio

from silicon_pantheon.server.app import App, build_mcp_server
from silicon_pantheon.shared.protocol import ConnectionState, ErrorCode


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


def test_heartbeat_does_not_create_connection() -> None:
    """Heartbeat must not auto-create connections (registration backdoor)."""
    app = App()
    mcp = build_mcp_server(app)
    _call(mcp, "heartbeat", connection_id="ghost")
    assert app.get_connection("ghost") is None
    assert app.connection_count() == 0


def test_heartbeat_updates_last_heartbeat_at() -> None:
    app = App()
    mcp = build_mcp_server(app)
    # Register first — heartbeat no longer creates connections.
    _call(mcp, "set_player_metadata", connection_id="c1",
          display_name="Tester", kind="human")
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


def test_create_room_inherits_max_turns_from_scenario_when_unspecified() -> None:
    """Regression: silicon-join doesn't pass max_turns when calling
    create_room. The previous default of 20 silently overrode any
    scenario that declared a smaller cap (Hormuz uses 10), and the
    win plugin's `if state.turn > state.max_turns` red-wins-by-
    timeout fired at turn 21 instead of 11. Matches ran way past
    intent. Default is now None → snap to scenario's value."""
    app = App()
    mcp = build_mcp_server(app)

    _call(
        mcp, "set_player_metadata",
        connection_id="c1", display_name="alice", kind="ai",
        provider="anthropic", model="claude-haiku-4-5",
    )
    out = _call(
        mcp, "create_room",
        connection_id="c1",
        scenario="13_hormuz",
        # max_turns deliberately omitted — should pick up the
        # scenario's declared 10.
    )
    assert out["ok"] is True
    room_id = out["room_id"]
    room = app.rooms.get(room_id)
    assert room is not None
    assert room.config.max_turns == 16, (
        f"Hormuz declares max_turns=16 in YAML but room got "
        f"{room.config.max_turns} — the create_room default isn't "
        f"honoring the scenario"
    )


def test_create_room_explicit_max_turns_wins_over_scenario_default() -> None:
    """Explicit override still works."""
    app = App()
    mcp = build_mcp_server(app)
    _call(
        mcp, "set_player_metadata",
        connection_id="c2", display_name="bob", kind="ai",
        provider="anthropic", model="claude-haiku-4-5",
    )
    out = _call(
        mcp, "create_room",
        connection_id="c2",
        scenario="13_hormuz",
        max_turns=15,
    )
    assert out["ok"] is True
    room = app.rooms.get(out["room_id"])
    assert room is not None
    assert room.config.max_turns == 15


def test_update_room_config_scenario_change_resnaps_max_turns() -> None:
    """Switching scenario mid-room without an explicit max_turns
    should snap to the new scenario's declared cap, not keep the
    previous one."""
    app = App()
    mcp = build_mcp_server(app)
    _call(
        mcp, "set_player_metadata",
        connection_id="c3", display_name="cathy", kind="ai",
        provider="anthropic", model="claude-haiku-4-5",
    )
    # Start with 01_tiny_skirmish (declared max_turns is its own).
    out = _call(
        mcp, "create_room",
        connection_id="c3",
        scenario="01_tiny_skirmish",
    )
    assert out["ok"] is True
    room = app.rooms.get(out["room_id"])
    initial = room.config.max_turns
    # Switch to Hormuz; expect max_turns to become 16.
    out2 = _call(
        mcp, "update_room_config",
        connection_id="c3",
        scenario="13_hormuz",
    )
    assert out2["ok"] is True, out2
    assert room.config.max_turns == 16, (
        f"scenario switch from {initial}-cap → Hormuz didn't update "
        f"max_turns; still {room.config.max_turns}"
    )


# ---- orphaned connection state recovery ----


def test_leave_room_resets_orphaned_in_game_to_in_lobby() -> None:
    """A connection stuck in IN_GAME with no conn_to_room entry (room
    cleaned up by heartbeat sweeper) should be reset to IN_LOBBY by
    leave_room instead of returning an error."""
    app = App()
    mcp = build_mcp_server(app)
    _call(
        mcp, "set_player_metadata",
        connection_id="c1", display_name="orphan", kind="ai",
    )
    conn = app.get_connection("c1")
    assert conn is not None
    # Simulate orphaned state: force IN_GAME with no room mapping.
    conn.state = ConnectionState.IN_GAME
    assert "c1" not in app.conn_to_room

    out = _call(mcp, "leave_room", connection_id="c1")
    assert out["ok"] is True
    assert conn.state == ConnectionState.IN_LOBBY


def test_leave_room_resets_orphaned_in_room_to_in_lobby() -> None:
    """Same as above but for IN_ROOM state."""
    app = App()
    mcp = build_mcp_server(app)
    _call(
        mcp, "set_player_metadata",
        connection_id="c1", display_name="orphan", kind="ai",
    )
    conn = app.get_connection("c1")
    assert conn is not None
    conn.state = ConnectionState.IN_ROOM
    assert "c1" not in app.conn_to_room

    out = _call(mcp, "leave_room", connection_id="c1")
    assert out["ok"] is True
    assert conn.state == ConnectionState.IN_LOBBY


def test_set_player_metadata_resets_orphaned_in_game_to_in_lobby() -> None:
    """Re-auth via set_player_metadata should detect an orphaned IN_GAME
    connection (no conn_to_room entry) and reset to IN_LOBBY."""
    app = App()
    mcp = build_mcp_server(app)
    _call(
        mcp, "set_player_metadata",
        connection_id="c1", display_name="worker", kind="ai",
    )
    conn = app.get_connection("c1")
    assert conn is not None
    conn.state = ConnectionState.IN_GAME
    assert "c1" not in app.conn_to_room

    out = _call(
        mcp, "set_player_metadata",
        connection_id="c1", display_name="worker", kind="ai",
    )
    assert out["ok"] is True
    assert out["state"] == ConnectionState.IN_LOBBY.value
    assert conn.state == ConnectionState.IN_LOBBY


def test_set_player_metadata_keeps_in_game_when_room_exists() -> None:
    """If the connection IS in a room (conn_to_room has an entry),
    set_player_metadata must NOT reset to IN_LOBBY."""
    app = App()
    mcp = build_mcp_server(app)
    _call(
        mcp, "set_player_metadata",
        connection_id="c1", display_name="active", kind="ai",
    )
    conn = app.get_connection("c1")
    assert conn is not None
    conn.state = ConnectionState.IN_GAME
    from silicon_pantheon.server.rooms import Slot
    app.conn_to_room["c1"] = ("room-xyz", Slot.A)

    out = _call(
        mcp, "set_player_metadata",
        connection_id="c1", display_name="active", kind="ai",
    )
    assert out["ok"] is True
    assert out["state"] == ConnectionState.IN_GAME.value
    assert conn.state == ConnectionState.IN_GAME

    # Clean up
    del app.conn_to_room["c1"]


def test_orphaned_connection_can_create_room_after_recovery() -> None:
    """End-to-end: orphaned IN_GAME → set_player_metadata resets →
    create_room succeeds."""
    app = App()
    mcp = build_mcp_server(app)
    _call(
        mcp, "set_player_metadata",
        connection_id="c1", display_name="bot", kind="ai",
    )
    conn = app.get_connection("c1")
    assert conn is not None
    conn.state = ConnectionState.IN_GAME

    # Re-auth recovers to IN_LOBBY.
    out = _call(
        mcp, "set_player_metadata",
        connection_id="c1", display_name="bot", kind="ai",
    )
    assert out["state"] == ConnectionState.IN_LOBBY.value

    # Now create_room should work.
    out = _call(
        mcp, "create_room",
        connection_id="c1",
        scenario="01_tiny_skirmish",
    )
    assert out["ok"] is True
    assert "room_id" in out
