"""MCP server application.

Holds the authoritative in-memory state (connections, rooms, tokens)
and exposes the game + lobby tool surface over MCP streamable HTTP.

Connection identity model
-------------------------

Clients pass a `connection_id` argument on every tool call. The
server treats that argument as the primary key for its per-connection
state table.

Registration gate: only `set_player_metadata` may create a new
Connection for an unknown connection_id. All other tools look up the
connection via `get_connection()` and reject unknown IDs with a
NOT_REGISTERED error. `heartbeat` tolerates unknown IDs (returns
server_time without creating state) so it cannot be used as a
registration backdoor.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from threading import Lock

_CONNECTION_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,128}$")

MAX_CONNECTIONS = 500

from mcp.server.fastmcp import FastMCP

from silicon_pantheon.server.auth import TokenRegistry
from silicon_pantheon.server.engine.state import Team
from silicon_pantheon.server.rooms import RoomRegistry, Slot
from silicon_pantheon.server.session import Session
from silicon_pantheon.shared.player_metadata import PlayerMetadata
from silicon_pantheon.shared.protocol import (
    MINIMUM_CLIENT_PROTOCOL_VERSION,
    PROTOCOL_VERSION,
    UPGRADE_COMMAND_HINT,
    ConnectionState,
    ErrorCode,
)


@dataclass
class Connection:
    """Per-client server-side state."""

    id: str
    state: ConnectionState = ConnectionState.ANONYMOUS
    player: PlayerMetadata | None = None
    token: str | None = None  # per-room token once joined
    # Protocol version this client declared at set_player_metadata.
    # Unset (None) means the client is pre-handshake-aware and should
    # be treated as v1 for compatibility. Used by tool handlers that
    # need a shim window to serve both old-shape and new-shape
    # responses while rolling out a breaking change — see
    # docs/VERSIONING.md, Phase 1 of the breaking-change checklist.
    client_protocol_version: int | None = None
    last_heartbeat_at: float = field(default_factory=time.time)
    # Distinct from last_heartbeat_at: tracks when the connection
    # last issued a "meaningful" game tool (anything other than the
    # bare heartbeat ping). Detects the case where a client's
    # transport + heartbeat task are still alive but the TUI's game
    # loop has died — the heartbeat lies about liveness for the
    # purposes of forfeit-on-silence. Updated by the dispatcher on
    # any tool except `heartbeat`.
    last_game_activity_at: float = field(default_factory=time.time)


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
        """Return the Connection for this id, creating it if new.

        Raises ValueError if *connection_id* contains characters outside
        the allowed set ``[a-zA-Z0-9_-]`` or exceeds 128 characters.
        """
        if not _CONNECTION_ID_RE.match(connection_id):
            raise ValueError(
                "connection_id must match [a-zA-Z0-9_-]{1,128}"
            )
        with self._conn_lock:
            conn = self._connections.get(connection_id)
            if conn is None:
                if len(self._connections) >= MAX_CONNECTIONS:
                    raise ValueError(
                        f"server connection limit ({MAX_CONNECTIONS}) reached"
                    )
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


def _error(code: ErrorCode, message: str, data: dict | None = None) -> dict:
    err: dict = {"code": code.value, "message": message}
    if data:
        err["data"] = data
    return {"ok": False, "error": err}


def _ok(payload: dict | None = None) -> dict:
    out: dict = {"ok": True}
    if payload:
        out.update(payload)
    return out


# ---- FastMCP factory ----


def build_mcp_server(app: App, *, name: str = "silicon-server") -> FastMCP:
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
        client_protocol_version: int | None = None,
    ) -> dict:
        """Declare who you are. Required before lobby operations.

        The optional `client_protocol_version` argument lets the server
        refuse to talk to a client whose wire format is too old to
        understand our responses. Clients above the server's own
        version are accepted (newer-client-on-older-server is the
        usual deploy order and the client is responsible for skipping
        features the server doesn't advertise) — only clients BELOW
        MINIMUM_CLIENT_PROTOCOL_VERSION get rejected with a clear
        upgrade prompt. See docs/versioning.md.
        """
        # Treat a missing client_protocol_version as v1 — that's what
        # the pre-handshake-aware clients effectively spoke. Once
        # MINIMUM_CLIENT_PROTOCOL_VERSION goes above 1, those clients
        # will fail the check below and get CLIENT_TOO_OLD with an
        # upgrade prompt, which is exactly what we want. Also tolerate
        # a string form ("1") — some older callers send it as a
        # stringified version; best effort parse, fall back to v1.
        effective_version: int
        if isinstance(client_protocol_version, int):
            effective_version = client_protocol_version
        else:
            try:
                effective_version = int(client_protocol_version)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                effective_version = 1
        if effective_version < MINIMUM_CLIENT_PROTOCOL_VERSION:
            return _error(
                ErrorCode.CLIENT_TOO_OLD,
                (
                    f"client protocol v{effective_version} is below "
                    f"this server's minimum supported v{MINIMUM_CLIENT_PROTOCOL_VERSION}. "
                    f"Please upgrade the client. {UPGRADE_COMMAND_HINT}"
                ),
                {
                    "client_protocol_version": effective_version,
                    "server_protocol_version": PROTOCOL_VERSION,
                    "minimum_client_protocol_version": MINIMUM_CLIENT_PROTOCOL_VERSION,
                    "upgrade_command": UPGRADE_COMMAND_HINT,
                },
            )
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
        try:
            conn = app.ensure_connection(connection_id)
        except ValueError as e:
            return _error(ErrorCode.BAD_INPUT, str(e))
        conn.player = meta
        # Only update the recorded version when the caller actually
        # supplied one. An explicit re-call that omits the arg (e.g.
        # a future compat shim or a re-auth path) falls to the v1
        # baseline for the MIN check above, but mustn't REGRESS the
        # version stamp on the connection — a handler branching on
        # `conn.client_protocol_version >= 2` would then emit the
        # old-shape response to a client that's actually on v2.
        if client_protocol_version is not None:
            conn.client_protocol_version = effective_version
        if conn.state == ConnectionState.ANONYMOUS:
            conn.state = ConnectionState.IN_LOBBY
        conn.last_heartbeat_at = time.time()
        return _ok(
            {
                "state": conn.state.value,
                "player": meta.to_dict(),
                "server_protocol_version": PROTOCOL_VERSION,
                "minimum_client_protocol_version": MINIMUM_CLIENT_PROTOCOL_VERSION,
            }
        )

    @mcp.tool()
    def heartbeat(connection_id: str) -> dict:
        """Lightweight liveness ping. Returns server time in seconds."""
        conn = app.get_connection(connection_id)
        now = time.time()
        if conn is not None:
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
    from silicon_pantheon.server.game_tools import (
        register_game_tools,
        start_game_for_room,
    )
    from silicon_pantheon.server.lobby_tools import register_lobby_tools

    register_lobby_tools(mcp, app)
    register_game_tools(mcp, app)

    # Install the hook the countdown fires on expiry. `start_game_for_room`
    # builds the engine Session, pins slot->team, and flips connections
    # into IN_GAME.
    app.on_countdown_complete = lambda room_id: start_game_for_room(app, room_id)

    return mcp
