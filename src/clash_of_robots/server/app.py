"""MCP server application.

Holds the authoritative in-memory state (connections, rooms, tokens)
and exposes the game + lobby tool surface over MCP streamable HTTP.

Connection identity model for Phase 1a
---------------------------------------

Proper MCP auth via HTTP headers will land in Phase 1b alongside the
join-room / per-match token flow. For Phase 1a we keep the
state-shape clean by having the client pass a `connection_id`
argument on every tool call. The server treats that argument as the
primary key for its per-connection state table.

This is functionally equivalent to carrying a header token and lets
us validate the tool contract end-to-end; switching to headers in 1b
is a middleware-only change that does not touch tool handlers.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from threading import Lock

from mcp.server.fastmcp import FastMCP

from clash_of_robots.server.auth import TokenRegistry
from clash_of_robots.server.engine.state import Team
from clash_of_robots.server.rooms import RoomRegistry, Slot
from clash_of_robots.server.session import Session
from clash_of_robots.shared.player_metadata import PlayerMetadata
from clash_of_robots.shared.protocol import ConnectionState, ErrorCode


@dataclass
class Connection:
    """Per-client server-side state."""

    id: str
    state: ConnectionState = ConnectionState.ANONYMOUS
    player: PlayerMetadata | None = None
    token: str | None = None  # per-room token once joined
    last_heartbeat_at: float = field(default_factory=time.time)


class App:
    """Root application state. Pass one instance around; MCP tool
    handlers look up connections, rooms, and tokens through it."""

    def __init__(self) -> None:
        self.tokens = TokenRegistry()
        self.rooms = RoomRegistry()
        # Per-room authoritative game session, once the match has started.
        self.sessions: dict[str, Session] = {}
        # Per-room slot → team mapping, pinned at game-start time
        # (deterministic for fixed assignment, coin-flip for random).
        self.slot_to_team: dict[str, dict[Slot, Team]] = {}
        # Reverse index: which room + slot each connection is in.
        # Populated by lobby / dev-game tools; read by game tools to
        # resolve the viewer for an incoming call.
        self.conn_to_room: dict[str, tuple[str, Slot]] = {}
        # Autostart countdown state per room.
        self.autostart_tasks: dict[str, asyncio.Task] = {}
        self.autostart_deadlines: dict[str, float] = {}
        # Hook fired by the lobby when a countdown completes; the
        # game_tools layer installs its "promote room to IN_GAME" callback.
        self.on_countdown_complete: Callable[[str], None] | None = None
        # Per-connection heartbeat bookkeeping (soft-disconnect timers).
        # Keyed by connection_id; the type is imported lazily in the
        # heartbeat module to avoid a circular import.
        self.heartbeat_state: dict[str, object] = {}
        self._connections: dict[str, Connection] = {}
        self._conn_lock = Lock()

    # ---- connection bookkeeping ----

    def ensure_connection(self, connection_id: str) -> Connection:
        """Return the Connection for this id, creating it if new."""
        with self._conn_lock:
            conn = self._connections.get(connection_id)
            if conn is None:
                conn = Connection(id=connection_id)
                self._connections[connection_id] = conn
            return conn

    def get_connection(self, connection_id: str) -> Connection | None:
        with self._conn_lock:
            return self._connections.get(connection_id)

    def drop_connection(self, connection_id: str) -> None:
        with self._conn_lock:
            self._connections.pop(connection_id, None)

    def connection_count(self) -> int:
        with self._conn_lock:
            return len(self._connections)


# ---- helpers used by tool handlers ----


def _error(code: ErrorCode, message: str) -> dict:
    return {"ok": False, "error": {"code": code.value, "message": message}}


def _ok(payload: dict | None = None) -> dict:
    out: dict = {"ok": True}
    if payload:
        out.update(payload)
    return out


# ---- FastMCP factory ----


def build_mcp_server(app: App, *, name: str = "clash-server") -> FastMCP:
    """Register always-available tools on a new FastMCP instance.

    1a exposes only the three state-independent tools:
      - set_player_metadata: anonymous -> in_lobby transition
      - heartbeat: liveness
      - whoami: introspection

    Lobby / room / game tools are added in subsequent sub-phases.
    """
    mcp = FastMCP(name)

    @mcp.tool()
    def set_player_metadata(
        connection_id: str,
        display_name: str,
        kind: str,
        provider: str | None = None,
        model: str | None = None,
        version: str = "1",
    ) -> dict:
        """Declare who you are. Required before lobby operations."""
        try:
            meta = PlayerMetadata.from_dict(
                {
                    "display_name": display_name,
                    "kind": kind,
                    "provider": provider,
                    "model": model,
                    "version": version,
                }
            )
        except ValueError as e:
            return _error(ErrorCode.BAD_INPUT, str(e))
        conn = app.ensure_connection(connection_id)
        conn.player = meta
        if conn.state == ConnectionState.ANONYMOUS:
            conn.state = ConnectionState.IN_LOBBY
        conn.last_heartbeat_at = time.time()
        return _ok({"state": conn.state.value, "player": meta.to_dict()})

    @mcp.tool()
    def heartbeat(connection_id: str) -> dict:
        """Lightweight liveness ping. Returns server time in seconds."""
        conn = app.ensure_connection(connection_id)
        now = time.time()
        conn.last_heartbeat_at = now
        return _ok({"server_time": now})

    @mcp.tool()
    def whoami(connection_id: str) -> dict:
        """Return this connection's current state + player metadata."""
        conn = app.get_connection(connection_id)
        if conn is None:
            return _ok({"state": ConnectionState.ANONYMOUS.value, "player": None})
        return _ok(
            {
                "state": conn.state.value,
                "player": conn.player.to_dict() if conn.player else None,
            }
        )

    # Attach the lobby tool set, then the 13 game tools. Imported locally
    # to avoid circular imports with modules that depend on this one.
    from clash_of_robots.server.game_tools import (
        register_game_tools,
        start_game_for_room,
    )
    from clash_of_robots.server.lobby_tools import register_lobby_tools

    register_lobby_tools(mcp, app)
    register_game_tools(mcp, app)

    # Install the hook the countdown fires on expiry. `start_game_for_room`
    # builds the engine Session, pins slot->team, and flips connections
    # into IN_GAME.
    app.on_countdown_complete = lambda room_id: start_game_for_room(app, room_id)

    return mcp
