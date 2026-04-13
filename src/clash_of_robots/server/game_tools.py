"""MCP-facing wrappers around the 13 in-process game tools.

Each MCP tool derives the player's viewer (Team.BLUE/RED) from the
connection's slot in its room, looks up the room's authoritative
Session, dispatches to the existing in-process tool layer, and
returns a structured result.

Phase 1a: only one hardcoded "dev room" exists, created by the
`create_dev_game` tool. Phase 1b replaces that with proper lobby /
create_room / join_room flow.

The heavy lifting stays in `server/tools/__init__.py` — these are
thin dispatch wrappers so fog-of-war filtering (1c) can slot into a
single transform that every game tool output passes through.
"""

from __future__ import annotations

import random
from typing import Any

from mcp.server.fastmcp import FastMCP

from clash_of_robots.server.app import App, Connection, _error, _ok
from clash_of_robots.server.engine.scenarios import load_scenario
from clash_of_robots.server.engine.state import Team
from clash_of_robots.server.rooms import RoomConfig, RoomStatus, Slot
from clash_of_robots.server.session import Session, new_session
from clash_of_robots.server.tools import ToolError, call_tool
from clash_of_robots.shared.protocol import ConnectionState, ErrorCode
from clash_of_robots.shared.viewer_filter import (
    ViewerContext,
    filter_state,
    filter_threat_map,
    filter_unit,
    update_ever_seen,
)


# Tools whose dict result is the full state snapshot or a per-unit view;
# these must be passed through the viewer filter before returning.
_FILTERED_STATE_TOOLS = frozenset({"get_state"})
_FILTERED_UNIT_TOOLS = frozenset({"get_unit"})
_FILTERED_THREAT_TOOLS = frozenset({"get_threat_map"})


def _viewer_context(session: Session, viewer: Team) -> ViewerContext:
    return ViewerContext(
        team=viewer,
        fog_mode=session.fog_of_war,  # type: ignore[arg-type]
        ever_seen=session.ever_seen.get(viewer, frozenset()),
    )


def _apply_filter(
    tool_name: str, result: dict, session: Session, viewer: Team
) -> dict:
    """Pass state-revealing tool results through the viewer filter.

    Only tools that return state / unit / threat-map info need filtering;
    action results (move/attack/heal/wait/end_turn) are always safe to
    echo back because they describe the caller's own action.
    """
    if session.fog_of_war == "none":
        return result
    ctx = _viewer_context(session, viewer)
    if tool_name in _FILTERED_STATE_TOOLS:
        return filter_state(session.state, ctx)
    if tool_name in _FILTERED_UNIT_TOOLS:
        filtered = filter_unit(result.get("id", ""), result, session.state, ctx)
        return filtered if filtered is not None else {"error": "unit not visible"}
    if tool_name in _FILTERED_THREAT_TOOLS:
        return filter_threat_map(result, session.state, ctx)
    return result


def _maybe_update_ever_seen(session: Session, result: dict, viewer: Team) -> None:
    """After a half-turn ends, grow this team's ever_seen for classic mode."""
    if session.fog_of_war != "classic":
        return
    if not isinstance(result, dict):
        return
    if result.get("type") == "end_turn":
        session.ever_seen[viewer] = update_ever_seen(
            session.state, viewer, session.ever_seen[viewer]
        )


def start_game_for_room(app: App, room_id: str) -> None:
    """Promote a room from COUNTING_DOWN to IN_GAME.

    Builds the engine Session from the room's scenario, pins the
    slot->team mapping (deterministic for fixed-assignment rooms,
    coin-flipped for random), and flips every connection seated in
    the room into state IN_GAME. Idempotent if the room has already
    started.
    """
    room = app.rooms.get(room_id)
    if room is None:
        return
    if room.status == RoomStatus.IN_GAME:
        return
    if not room.all_ready():
        return
    state = load_scenario(room.config.scenario)
    state.max_turns = room.config.max_turns
    session = new_session(
        state, scenario=room.config.scenario, fog_of_war=room.config.fog_of_war
    )
    app.sessions[room_id] = session
    if room.config.team_assignment == "fixed":
        host_team = Team.BLUE if room.config.host_team == "blue" else Team.RED
        other = Team.RED if host_team is Team.BLUE else Team.BLUE
        app.slot_to_team[room_id] = {Slot.A: host_team, Slot.B: other}
    else:  # "random"
        coin = random.random() < 0.5
        app.slot_to_team[room_id] = (
            {Slot.A: Team.BLUE, Slot.B: Team.RED}
            if coin
            else {Slot.A: Team.RED, Slot.B: Team.BLUE}
        )
    room.status = RoomStatus.IN_GAME
    for cid, (rid, _slot) in app.conn_to_room.items():
        if rid == room_id:
            c = app.get_connection(cid)
            if c is not None:
                c.state = ConnectionState.IN_GAME


def _viewer_for(conn: Connection, app: App) -> tuple[Any, Team] | None:
    """Resolve (session, viewer) for a connection currently in a game.

    Returns None if the connection isn't in a game or the room/session
    has gone away.
    """
    if conn.state != ConnectionState.IN_GAME:
        return None
    info = app.conn_to_room.get(conn.id)
    if info is None:
        return None
    room_id, slot = info
    session = app.sessions.get(room_id)
    if session is None:
        return None
    # Slot → Team mapping is pinned at game-start time on the App.
    mapping = app.slot_to_team.get(room_id)
    if mapping is None:
        return None
    return session, mapping[slot]


def _dispatch(app: App, connection_id: str, tool_name: str, args: dict) -> dict:
    """Shared body for every game tool wrapper."""
    conn = app.get_connection(connection_id)
    if conn is None:
        return _error(ErrorCode.TOKEN_INVALID, "unknown connection_id")
    if conn.state != ConnectionState.IN_GAME:
        return _error(
            ErrorCode.TOOL_NOT_AVAILABLE_IN_STATE,
            f"game tools require state=in_game (current: {conn.state.value})",
        )
    resolved = _viewer_for(conn, app)
    if resolved is None:
        return _error(ErrorCode.GAME_NOT_STARTED, "no active game for this connection")
    session, viewer = resolved
    try:
        result = call_tool(session, viewer, tool_name, args)
    except ToolError as e:
        return _error(ErrorCode.BAD_INPUT, str(e))
    # Grow ever_seen *before* filtering the response so the viewer sees
    # tiles they just observed at the boundary. Currently only end_turn
    # updates ever_seen; if we later want live memory during a turn we
    # can expand this.
    _maybe_update_ever_seen(session, result, viewer)
    filtered = _apply_filter(tool_name, result, session, viewer)
    return _ok({"result": filtered})


def register_game_tools(mcp: FastMCP, app: App) -> None:
    """Attach the 13 game tools + create_dev_game to an MCP server.

    Each tool has an explicit Python signature so FastMCP can generate
    a proper JSON schema for agents. The dispatch body delegates to the
    in-process tool layer via `_dispatch`.
    """

    # ---- dev-only game creation (Phase 1a) ----

    @mcp.tool()
    def create_dev_game(
        connection_id: str,
        scenario: str = "01_tiny_skirmish",
    ) -> dict:
        """Create a single hardcoded dev game and seat this connection
        in slot A (blue). A second connection can call `join_dev_game`
        to take slot B (red) and start the match.
        """
        conn = app.get_connection(connection_id)
        if conn is None or conn.state != ConnectionState.IN_LOBBY:
            return _error(
                ErrorCode.TOOL_NOT_AVAILABLE_IN_STATE,
                "create_dev_game requires state=in_lobby",
            )
        if conn.player is None:
            return _error(ErrorCode.BAD_INPUT, "set_player_metadata first")
        if app.sessions:
            return _error(ErrorCode.ALREADY_IN_ROOM, "a dev game already exists")
        room, slot = app.rooms.create(
            config=RoomConfig(scenario=scenario), host=conn.player
        )
        app.conn_to_room[connection_id] = (room.id, slot)
        conn.state = ConnectionState.IN_ROOM
        return _ok({"room_id": room.id, "slot": slot.value})

    @mcp.tool()
    def join_dev_game(connection_id: str) -> dict:
        """Join the single hardcoded dev game as slot B (red) and start
        the match immediately (no ready protocol yet — that's Phase 1b).
        """
        conn = app.get_connection(connection_id)
        if conn is None or conn.state != ConnectionState.IN_LOBBY:
            return _error(
                ErrorCode.TOOL_NOT_AVAILABLE_IN_STATE,
                "join_dev_game requires state=in_lobby",
            )
        if conn.player is None:
            return _error(ErrorCode.BAD_INPUT, "set_player_metadata first")
        rooms = app.rooms.list()
        if not rooms:
            return _error(ErrorCode.ROOM_NOT_FOUND, "no dev game to join")
        room = rooms[0]
        result = app.rooms.join(room.id, conn.player)
        if result is None:
            return _error(ErrorCode.ROOM_FULL, "dev game is full")
        _, slot = result
        app.conn_to_room[connection_id] = (room.id, slot)
        # Start the game: build session, pin slot→team mapping, flip both
        # connections' state to IN_GAME.
        state = load_scenario(room.scenario)
        session = new_session(state, scenario=room.scenario)
        app.sessions[room.id] = session
        # Hardcoded mapping for Phase 1a: slot A = blue, slot B = red.
        app.slot_to_team[room.id] = {Slot.A: Team.BLUE, Slot.B: Team.RED}
        for cid, (rid, _slot) in app.conn_to_room.items():
            if rid == room.id:
                c = app.get_connection(cid)
                if c is not None:
                    c.state = ConnectionState.IN_GAME
        return _ok({"room_id": room.id, "slot": slot.value})

    # ---- the 13 game tools, each a thin dispatch wrapper ----

    @mcp.tool()
    def get_state(connection_id: str) -> dict:
        """Get the current full game state visible to you."""
        return _dispatch(app, connection_id, "get_state", {})

    @mcp.tool()
    def get_unit(connection_id: str, unit_id: str) -> dict:
        """Get a single unit's details by id."""
        return _dispatch(app, connection_id, "get_unit", {"unit_id": unit_id})

    @mcp.tool()
    def get_legal_actions(connection_id: str, unit_id: str) -> dict:
        """Get legal moves/attacks/heals/wait for one of your units."""
        return _dispatch(app, connection_id, "get_legal_actions", {"unit_id": unit_id})

    @mcp.tool()
    def simulate_attack(
        connection_id: str,
        attacker_id: str,
        target_id: str,
        from_tile: dict | None = None,
    ) -> dict:
        """Predict attack outcome. Does not mutate state."""
        args: dict = {"attacker_id": attacker_id, "target_id": target_id}
        if from_tile is not None:
            args["from_tile"] = from_tile
        return _dispatch(app, connection_id, "simulate_attack", args)

    @mcp.tool()
    def get_threat_map(connection_id: str) -> dict:
        """Return which enemy units can attack each tile."""
        return _dispatch(app, connection_id, "get_threat_map", {})

    @mcp.tool()
    def get_history(connection_id: str, last_n: int = 10) -> dict:
        """Get recent action history."""
        return _dispatch(app, connection_id, "get_history", {"last_n": last_n})

    @mcp.tool()
    def get_coach_messages(connection_id: str, since_turn: int = 0) -> dict:
        """Drain unread coach messages for your team."""
        return _dispatch(app, connection_id, "get_coach_messages", {"since_turn": since_turn})

    @mcp.tool()
    def move(connection_id: str, unit_id: str, dest: dict) -> dict:
        """Move a ready unit to a destination tile."""
        return _dispatch(app, connection_id, "move", {"unit_id": unit_id, "dest": dest})

    @mcp.tool()
    def attack(connection_id: str, unit_id: str, target_id: str) -> dict:
        """Attack an enemy unit; resolves combat + counter immediately."""
        return _dispatch(
            app, connection_id, "attack", {"unit_id": unit_id, "target_id": target_id}
        )

    @mcp.tool()
    def heal(connection_id: str, healer_id: str, target_id: str) -> dict:
        """Heal an adjacent ally (Mage only)."""
        return _dispatch(
            app, connection_id, "heal", {"healer_id": healer_id, "target_id": target_id}
        )

    @mcp.tool()
    def wait(connection_id: str, unit_id: str) -> dict:
        """End this unit's turn without attacking or healing."""
        return _dispatch(app, connection_id, "wait", {"unit_id": unit_id})

    @mcp.tool()
    def end_turn(connection_id: str) -> dict:
        """Pass control to the opponent."""
        return _dispatch(app, connection_id, "end_turn", {})

    @mcp.tool()
    def send_to_agent(connection_id: str, team: str, text: str) -> dict:
        """(Coach) Queue a message for a team, delivered next turn."""
        return _dispatch(app, connection_id, "send_to_agent", {"team": team, "text": text})

    @mcp.tool()
    def download_replay(connection_id: str) -> dict:
        """Fetch this connection's match replay as JSONL text.

        Available while the connection is IN_GAME (including after
        the game has ended; token stays valid briefly so clients can
        download before state is purged).
        """
        conn = app.get_connection(connection_id)
        if conn is None:
            return _error(ErrorCode.TOKEN_INVALID, "unknown connection_id")
        if conn.state != ConnectionState.IN_GAME:
            return _error(
                ErrorCode.TOOL_NOT_AVAILABLE_IN_STATE,
                "download_replay requires state=in_game",
            )
        info = app.conn_to_room.get(connection_id)
        if info is None:
            return _error(ErrorCode.NOT_IN_ROOM, "connection not seated")
        room_id, _slot = info
        session = app.sessions.get(room_id)
        if session is None:
            return _error(ErrorCode.GAME_NOT_STARTED, "no session for this room")
        # Read the replay file if one was configured; otherwise we
        # reconstruct from the session's in-memory event log (not
        # implemented yet — Phase 1a sessions write nothing).
        if session.replay is None:
            return _error(
                ErrorCode.BAD_INPUT,
                "this match was not configured with a replay writer",
            )
        try:
            with open(session.replay.path, encoding="utf-8") as f:
                body = f.read()
        except OSError as e:
            return _error(ErrorCode.INTERNAL, f"failed to read replay: {e}")
        return _ok({"replay_jsonl": body, "path": str(session.replay.path)})

    @mcp.tool()
    def concede(connection_id: str) -> dict:
        """Resign the match — opponent wins immediately."""
        conn = app.get_connection(connection_id)
        if conn is None or conn.state != ConnectionState.IN_GAME:
            return _error(
                ErrorCode.TOOL_NOT_AVAILABLE_IN_STATE,
                "concede requires state=in_game",
            )
        info = app.conn_to_room.get(connection_id)
        if info is None:
            return _error(ErrorCode.NOT_IN_ROOM, "connection not seated")
        room_id, slot = info
        session = app.sessions.get(room_id)
        if session is None:
            return _error(ErrorCode.GAME_NOT_STARTED, "no session")
        from clash_of_robots.server.engine.state import GameStatus

        team_map = app.slot_to_team.get(room_id, {})
        my_team = team_map.get(slot)
        if my_team is None:
            return _error(ErrorCode.INTERNAL, "no team mapping")
        opponent = my_team.other()
        session.state.status = GameStatus.GAME_OVER
        session.state.winner = opponent
        session.log(
            "concede",
            {"by": my_team.value, "winner": opponent.value},
        )
        return _ok({"winner": opponent.value})
