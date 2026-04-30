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

Threading / synchronisation model
---------------------------------

See docs/THREADING.md for the full policy. Short version:

* `App._state_lock` is a `threading.RLock` that guards every mutable
  field on App, every Room field (via `app.rooms._rooms`), and every
  field on a Connection EXCEPT `last_heartbeat_at` (a single float
  store, GIL-atomic, deliberately lock-free so the heartbeat tool is
  ~free).
* `Session.lock` is a `threading.Lock` that guards all per-match game
  state and telemetry.
* `ReplayWriter._lock` and `ThoughtsLogWriter._lock` are leaves.
* Acquisition order (strict): `_state_lock` → `session.lock` →
  writer locks. Never reverse. Never hold a lower lock while
  acquiring a higher one.
* Never `await` anything while holding a lock — the lock is a
  `threading.Lock`/`RLock`, which would block the asyncio event loop.
* Never call user-registered hooks with `_state_lock` held. Hooks
  fire under `session.lock` (documented) and MUST NOT acquire any
  other server lock.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from threading import RLock
from typing import Annotated

from pydantic import Field as PydanticField

_CONNECTION_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,128}$")

MAX_CONNECTIONS = 500

from mcp.server.fastmcp import FastMCP
from mcp.types import Tool as MCPTool

from silicon_pantheon.server.auth import TokenRegistry
from silicon_pantheon.server.engine.state import Team


# Tools that are part of the client harness / lobby flow, not the
# AI agent's gameplay surface.  Hidden from tools/list so agents
# see only the tools they should call.  Still callable by name —
# the client harness invokes them directly.
_HARNESS_TOOLS: set[str] = {
    # identity / lifecycle
    "set_player_metadata",
    "heartbeat",
    "whoami",
    # lobby navigation
    "list_rooms",
    "list_scenarios",
    "describe_scenario",
    "get_scenario_bundle",
    "get_leaderboard",
    "get_model_details",
    "preview_room",
    "create_room",
    "join_room",
    "kick_player",
    "leave_room",
    "get_room_state",
    "set_ready",
    "update_room_config",
    # dev-only
    "create_dev_game",
    "join_dev_game",
    # coach (human-driven, not agent-driven)
    "send_to_agent",
    # client harness side-channels
    "record_thought",
    "download_replay",
}


class GameFastMCP(FastMCP):
    """FastMCP subclass that hides harness/lobby tools from tools/list.

    AI agents only see the gameplay tools they should call during a
    match.  The hidden tools remain callable by name — the client
    harness invokes them directly.
    """

    async def list_tools(self) -> list[MCPTool]:
        tools = self._tool_manager.list_tools()
        return [
            MCPTool(
                name=info.name,
                title=info.title,
                description=info.description,
                inputSchema=info.parameters,
                outputSchema=info.output_schema,
                annotations=info.annotations,
                icons=info.icons,
                _meta=info.meta,
            )
            for info in tools
            if info.name not in _HARNESS_TOOLS
        ]
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
    handlers look up connections, rooms, and tokens through it.

    All mutable fields below are guarded by `self._state_lock`
    (a `threading.RLock`). Multi-step operations on these fields
    (e.g. `leave_room` touching `conn_to_room`, `sessions`, and
    `rooms` atomically) should wrap the whole sequence in
    `with app.state_lock():`. Single-op accesses can use the
    convenience methods below (`get_session`, `pop_session`, …),
    each of which takes the lock briefly.

    `last_heartbeat_at` on Connection is the deliberate exception —
    a single-float store, GIL-atomic, so the heartbeat tool doesn't
    pay lock contention on every ping.
    """

    def __init__(self) -> None:
        # ── state_lock: guards every field under this divider ──
        # RLock so convenience methods can nest inside user-held
        # `with app.state_lock():` blocks.
        self._state_lock = RLock()
        self.rooms = RoomRegistry()
        self.sessions: dict[str, Session] = {}
        self.slot_to_team: dict[str, dict[Slot, Team]] = {}
        self.conn_to_room: dict[str, tuple[str, Slot]] = {}
        self.autostart_tasks: dict[str, asyncio.Task] = {}
        self.autostart_deadlines: dict[str, float] = {}
        self._connections: dict[str, Connection] = {}
        # Per-connection heartbeat bookkeeping (soft-disconnect timers).
        # Keyed by connection_id; the type is imported lazily in the
        # heartbeat module to avoid a circular import.
        self.heartbeat_state: dict[str, object] = {}
        # ── not guarded by state_lock ──
        # TokenRegistry has its own internal lock. Safe to call
        # without holding state_lock.
        self.tokens = TokenRegistry()
        # on_countdown_complete is set once at build_mcp_server time
        # and never mutated afterwards; no lock needed for reads.
        self.on_countdown_complete: Callable[[str], None] | None = None

    # ---- state_lock context manager ----

    def state_lock(self) -> RLock:
        """Context manager for multi-step atomic operations on App state.

        Use this when a handler needs to read/mutate multiple App
        fields as one atomic unit (e.g. ``leave_room`` pops
        ``conn_to_room``, mutates a ``Room``, pops ``sessions``,
        all under one lock). For single-shot reads/writes prefer
        the convenience methods (``get_connection``,
        ``get_session``, etc.) which each take the lock briefly.

        Returns the underlying RLock. Recursive acquisition from the
        same thread is legal (so a convenience method called from
        within a `with app.state_lock():` block doesn't deadlock).
        """
        return self._state_lock

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
        with self._state_lock:
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
        with self._state_lock:
            return self._connections.get(connection_id)

    def drop_connection(self, connection_id: str) -> None:
        with self._state_lock:
            self._connections.pop(connection_id, None)

    def connection_count(self) -> int:
        with self._state_lock:
            return len(self._connections)

    # ---- session / room mapping helpers ----
    #
    # These are convenience wrappers for one-shot reads/writes. For
    # multi-step sequences that must be atomic, callers should open
    # a `with app.state_lock():` block and use the raw dicts directly
    # — the RLock means re-entering through these methods is fine.

    def get_session(self, room_id: str) -> Session | None:
        with self._state_lock:
            return self.sessions.get(room_id)

    def set_session(self, room_id: str, session: Session) -> None:
        with self._state_lock:
            self.sessions[room_id] = session

    def pop_session(self, room_id: str) -> Session | None:
        with self._state_lock:
            return self.sessions.pop(room_id, None)

    def get_room_for_conn(self, cid: str) -> tuple[str, Slot] | None:
        with self._state_lock:
            return self.conn_to_room.get(cid)

    def set_room_for_conn(self, cid: str, room_id: str, slot: Slot) -> None:
        with self._state_lock:
            self.conn_to_room[cid] = (room_id, slot)

    def pop_room_for_conn(self, cid: str) -> tuple[str, Slot] | None:
        with self._state_lock:
            return self.conn_to_room.pop(cid, None)

    def get_slot_to_team(self, room_id: str) -> dict[Slot, Team] | None:
        with self._state_lock:
            return self.slot_to_team.get(room_id)

    def set_slot_to_team(
        self, room_id: str, mapping: dict[Slot, Team]
    ) -> None:
        with self._state_lock:
            self.slot_to_team[room_id] = mapping

    def pop_slot_to_team(self, room_id: str) -> dict[Slot, Team] | None:
        with self._state_lock:
            return self.slot_to_team.pop(room_id, None)

    def get_heartbeat_state(self, cid: str) -> object | None:
        with self._state_lock:
            return self.heartbeat_state.get(cid)

    def set_heartbeat_state(self, cid: str, state: object) -> None:
        with self._state_lock:
            self.heartbeat_state[cid] = state

    def pop_heartbeat_state(self, cid: str) -> object | None:
        with self._state_lock:
            return self.heartbeat_state.pop(cid, None)


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
    # json_response=True: POST /mcp returns a plain JSON body with
    # Content-Length instead of a chunked SSE stream. Pcap evidence
    # on 2026-04-22 showed that the SSE path races the chunked-
    # encoding terminator against the MCP client's eager close, so
    # every POST ended with a FIN-before-terminator abort. Caddy
    # couldn't keep-alive upstream; every tool call paid a fresh
    # TCP+TLS handshake under load, producing the 5 s "call SLOW"
    # cluster seen in client logs. Switching to json_response
    # delivers the same content with clean HTTP framing and lets
    # connection pooling actually work.
    #
    # The SSE path is still used for GET /mcp (server-initiated
    # notifications), which we don't use in practice — but keeping
    # it intact means nothing else changes. The MCP SDK clients we
    # use handle both modes transparently. See
    # ~/dev/transport-resilience-plan.md for the full analysis.
    mcp = GameFastMCP(name, json_response=True)

    @mcp.tool()
    def set_player_metadata(
        connection_id: Annotated[str, PydanticField(description="Your server session identifier.")],
        display_name: Annotated[str, PydanticField(description="Player name shown to other players.")],
        kind: Annotated[str, PydanticField(description="'human' or 'agent'.")],
        provider: Annotated[str | None, PydanticField(description="AI provider name (e.g. 'anthropic', 'openai'). Required when kind='agent'.", default=None)] = None,
        model: Annotated[str | None, PydanticField(description="Model ID (e.g. 'claude-sonnet-4-6'). Optional.", default=None)] = None,
        version: Annotated[str, PydanticField(description="Client software version string.", default="1")] = "1",
        client_protocol_version: Annotated[int | None, PydanticField(description="Wire-format protocol version integer. Clients below the server minimum are rejected.", default=None)] = None,
    ) -> dict:
        """Mutating. Register your identity with the server. Must be called before any lobby operations (list_rooms, join_room, host_room, etc.). display_name is your player name shown to others. kind must be 'human' or 'agent'. provider is the AI provider name (e.g. 'anthropic', 'openai') — required when kind='agent', ignored for humans. model is the specific model ID (e.g. 'claude-sonnet-4-6'). version is the client software version string. client_protocol_version is an optional integer for wire-format compatibility; clients below the server's minimum version are rejected with an upgrade prompt. Can be called again to update metadata. Returns the confirmed player profile."""
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
        with app.state_lock():
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
            elif conn.state in (
                ConnectionState.IN_GAME,
                ConnectionState.IN_ROOM,
            ) and connection_id not in app.conn_to_room:
                # Orphaned: the connection thinks it's in a game/room
                # but there's no room mapping (room was cleaned up by
                # the heartbeat sweeper or a prior disconnect where
                # leave_room failed on a dead transport). Reset so the
                # client can create/join again instead of being stuck
                # in an unrecoverable state.
                conn.state = ConnectionState.IN_LOBBY
            conn.last_heartbeat_at = time.time()
            state_value = conn.state.value
        return _ok(
            {
                "state": state_value,
                "player": meta.to_dict(),
                "server_protocol_version": PROTOCOL_VERSION,
                "minimum_client_protocol_version": MINIMUM_CLIENT_PROTOCOL_VERSION,
            }
        )

    @mcp.tool()
    def heartbeat(
        connection_id: Annotated[str, PydanticField(description="Your server session identifier.")],
    ) -> dict:
        """Read-only. Lightweight liveness ping called automatically by the client every ~10 seconds. Returns the current server time in seconds (Unix epoch). The server uses heartbeats to detect disconnected clients and clean up abandoned rooms. Not typically called by agents directly — the client harness handles it."""
        import logging as _logging
        import time as _time
        _log = _logging.getLogger("silicon")
        _t0 = _time.monotonic()
        conn = app.get_connection(connection_id)
        now = time.time()
        if conn is not None:
            prev = conn.last_heartbeat_at
            conn.last_heartbeat_at = now
            _log.info(
                "heartbeat cid=%s idle_before=%.1fs state=%s",
                connection_id[:8], now - prev, conn.state.value,
            )
        else:
            _log.info(
                "heartbeat cid=%s no_conn (server has no record of this cid)",
                connection_id[:8],
            )
        _dt = _time.monotonic() - _t0
        if _dt > 0.2:
            # Only logs if the server-side handler itself was slow
            # — which means the event loop was blocked for >200ms
            # by another coroutine. Exactly the signal we want to
            # catch "transport hang" investigations early.
            _log.warning(
                "heartbeat SLOW cid=%s dt=%.2fs — event loop blocked?",
                connection_id[:8], _dt,
            )
        return _ok({"server_time": now})

    @mcp.tool()
    def whoami(
        connection_id: Annotated[str, PydanticField(description="Your server session identifier.")],
    ) -> dict:
        """Read-only. Return this connection's current lifecycle state (ANONYMOUS, IN_LOBBY, IN_ROOM, or IN_GAME) and player metadata (display_name, kind, provider, model). Use this to check whether set_player_metadata has been called and what state the connection is in before issuing lobby or game commands."""
        with app.state_lock():
            conn = app._connections.get(connection_id)  # noqa: SLF001
            if conn is None:
                return _ok({"state": ConnectionState.ANONYMOUS.value, "player": None})
            state_value = conn.state.value
            player_dict = conn.player.to_dict() if conn.player else None
        return _ok({"state": state_value, "player": player_dict})

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
