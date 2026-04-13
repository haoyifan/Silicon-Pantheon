"""MCP lobby tool surface: list / create / preview / join / leave / ready.

Clients in state IN_LOBBY discover rooms with `list_rooms` + `preview_room`,
claim seats with `create_room` / `join_room`, and toggle readiness with
`set_ready`. The server runs a 10s auto-start countdown when both seats
are filled and both players are ready. The countdown is cancelable: any
player unreadying, leaving, or disconnecting resets it.

The countdown is implemented as an asyncio Task owned by the Room; it's
created synchronously in the lobby tool handler but runs on the event
loop FastMCP is using to serve requests, which is the same loop the
game runner will later use for per-turn scheduling.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from mcp.server.fastmcp import FastMCP

from clash_of_odin.server.app import App, _error, _ok
from clash_of_odin.server.engine.scenarios import load_scenario
from clash_of_odin.server.rooms import (
    FogMode,
    Room,
    RoomConfig,
    RoomStatus,
    Slot,
    TeamAssignment,
)
from clash_of_odin.shared.protocol import ConnectionState, ErrorCode

log = logging.getLogger("clash.lobby")

AUTOSTART_DELAY_S = 10.0


# ---- serialization helpers ----


def _serialize_room_summary(room: Room) -> dict[str, Any]:
    """Compact row used by list_rooms / get_room_state."""
    return {
        "room_id": room.id,
        "scenario": room.config.scenario,
        "host_name": room.host_name,
        "status": room.status.value,
        "team_assignment": room.config.team_assignment,
        "fog_of_war": room.config.fog_of_war,
        "max_turns": room.config.max_turns,
        "seats": {
            slot.value: {
                "occupied": seat.player is not None,
                "player": seat.player.to_dict() if seat.player else None,
                "ready": seat.ready,
            }
            for slot, seat in room.seats.items()
        },
        "created_at": room.created_at,
    }


def _serialize_room_preview(room: Room) -> dict[str, Any]:
    """Room summary + scenario map for the preview screen."""
    summary = _serialize_room_summary(room)
    try:
        state = load_scenario(room.config.scenario)
    except Exception as e:  # pragma: no cover - defensive
        summary["scenario_preview"] = {"error": str(e)}
        return summary
    units = [
        {"id": u.id, "owner": u.owner.value, "class": u.class_.value, "pos": u.pos.to_dict()}
        for u in state.units.values()
    ]
    forts = [
        {"pos": t.pos.to_dict(), "owner": t.fort_owner.value if t.fort_owner else None}
        for t in state.board.tiles.values()
        if t.is_fort
    ]
    summary["scenario_preview"] = {
        "width": state.board.width,
        "height": state.board.height,
        "units": units,
        "forts": forts,
        "max_turns": state.max_turns,
    }
    return summary


# ---- registration ----


def register_lobby_tools(mcp: FastMCP, app: App) -> None:
    """Attach the lobby tool set to a FastMCP instance."""

    @mcp.tool()
    def list_rooms(connection_id: str) -> dict:
        """List rooms currently open. Available in any post-anonymous state.

        FINISHED rooms are excluded — they're rubble waiting to be
        vacated and have no relevance to someone picking a match.
        """
        conn = app.get_connection(connection_id)
        if conn is None or conn.state == ConnectionState.ANONYMOUS:
            return _error(
                ErrorCode.TOOL_NOT_AVAILABLE_IN_STATE,
                "set_player_metadata first",
            )
        rooms = [
            _serialize_room_summary(r)
            for r in app.rooms.list()
            if r.status != RoomStatus.FINISHED
        ]
        return _ok({"rooms": rooms})

    @mcp.tool()
    def list_scenarios(connection_id: str) -> dict:
        """Enumerate scenarios available on this server.

        Walks the packaged `games/` directory and returns the
        sub-directory names that have a readable `config.yaml`. The
        client uses this to populate the 'change scenario' dropdown in
        the room screen.
        """
        conn = app.get_connection(connection_id)
        if conn is None or conn.state == ConnectionState.ANONYMOUS:
            return _error(
                ErrorCode.TOOL_NOT_AVAILABLE_IN_STATE,
                "set_player_metadata first",
            )
        from pathlib import Path

        # The scenarios live at repo-root/games. Mirror the logic in
        # engine/scenarios.load_scenario which imports from this path.
        candidates: list[str] = []
        games_root = Path("games")
        if games_root.is_dir():
            for sub in sorted(games_root.iterdir()):
                if sub.is_dir() and (sub / "config.yaml").is_file():
                    candidates.append(sub.name)
        return _ok({"scenarios": candidates})

    @mcp.tool()
    async def update_room_config(
        connection_id: str,
        scenario: str | None = None,
        team_assignment: str | None = None,
        host_team: str | None = None,
        fog_of_war: str | None = None,
        max_turns: int | None = None,
    ) -> dict:
        """Host-only: tweak room config while still in the lobby.

        Only fields passed (non-None) are updated. Any change resets
        both seats' ready flags — if readiness was previously agreed
        upon, the config shift might change the deal. Fails outside
        the pre-game states (COUNTING_DOWN, IN_GAME, FINISHED).
        """
        conn = app.get_connection(connection_id)
        if conn is None or conn.state != ConnectionState.IN_ROOM:
            return _error(
                ErrorCode.TOOL_NOT_AVAILABLE_IN_STATE,
                "update_room_config requires state=in_room",
            )
        info = app.conn_to_room.get(connection_id)
        if info is None:
            return _error(ErrorCode.NOT_IN_ROOM, "connection not seated")
        room_id, slot = info
        room = app.rooms.get(room_id)
        if room is None:
            return _error(ErrorCode.ROOM_NOT_FOUND, f"room {room_id} vanished")
        if slot != Slot.A:
            return _error(
                ErrorCode.BAD_INPUT, "only the host (slot A) can update room config"
            )
        if room.status not in (
            RoomStatus.WAITING_FOR_PLAYERS,
            RoomStatus.WAITING_READY,
        ):
            return _error(
                ErrorCode.TOOL_NOT_AVAILABLE_IN_STATE,
                f"room is in {room.status.value}; config locked",
            )

        # Validate every proposed field before mutating so we don't
        # end up with a partial update on reject.
        if scenario is not None:
            try:
                load_scenario(scenario)
            except Exception as e:
                return _error(ErrorCode.BAD_INPUT, f"scenario load failed: {e}")
        if team_assignment is not None and team_assignment not in ("fixed", "random"):
            return _error(
                ErrorCode.BAD_INPUT, "team_assignment must be 'fixed' or 'random'"
            )
        if host_team is not None and host_team not in ("blue", "red"):
            return _error(ErrorCode.BAD_INPUT, "host_team must be 'blue' or 'red'")
        if fog_of_war is not None and fog_of_war not in (
            "none",
            "classic",
            "line_of_sight",
        ):
            return _error(
                ErrorCode.BAD_INPUT,
                "fog_of_war must be 'none' | 'classic' | 'line_of_sight'",
            )

        if scenario is not None:
            room.config.scenario = scenario
        if team_assignment is not None:
            room.config.team_assignment = team_assignment  # type: ignore[assignment]
        if host_team is not None:
            room.config.host_team = host_team  # type: ignore[assignment]
        if fog_of_war is not None:
            room.config.fog_of_war = fog_of_war  # type: ignore[assignment]
        if max_turns is not None:
            if max_turns < 1 or max_turns > 200:
                return _error(
                    ErrorCode.BAD_INPUT, "max_turns must be between 1 and 200"
                )
            room.config.max_turns = max_turns

        # Config change resets readiness so both sides explicitly
        # re-agree on the new terms.
        for seat in room.seats.values():
            seat.ready = False
        room.recompute_status()
        log.info(
            "update_room_config: room=%s scenario=%s fog=%s teams=%s host_team=%s",
            room_id,
            room.config.scenario,
            room.config.fog_of_war,
            room.config.team_assignment,
            room.config.host_team,
        )
        return _ok({})

    @mcp.tool()
    def preview_room(connection_id: str, room_id: str) -> dict:
        """Show scenario map + seat occupancy for a room."""
        conn = app.get_connection(connection_id)
        if conn is None or conn.state == ConnectionState.ANONYMOUS:
            return _error(
                ErrorCode.TOOL_NOT_AVAILABLE_IN_STATE,
                "set_player_metadata first",
            )
        room = app.rooms.get(room_id)
        if room is None:
            return _error(ErrorCode.ROOM_NOT_FOUND, f"no such room: {room_id}")
        return _ok({"room": _serialize_room_preview(room)})

    @mcp.tool()
    def create_room(
        connection_id: str,
        scenario: str,
        max_turns: int = 20,
        team_assignment: str = "fixed",
        host_team: str = "blue",
        fog_of_war: str = "none",
        turn_time_limit_s: int = 180,
    ) -> dict:
        """Create a new room, seating the caller in slot A as the host.

        Transitions the caller from IN_LOBBY to IN_ROOM. Fails if the
        caller isn't IN_LOBBY or already has a room, if the scenario
        doesn't load, or if the config fields don't validate.
        """
        conn = app.get_connection(connection_id)
        if conn is None or conn.state != ConnectionState.IN_LOBBY:
            return _error(
                ErrorCode.TOOL_NOT_AVAILABLE_IN_STATE,
                "create_room requires state=in_lobby",
            )
        if conn.player is None:
            return _error(ErrorCode.BAD_INPUT, "set_player_metadata first")
        if team_assignment not in ("fixed", "random"):
            return _error(
                ErrorCode.BAD_INPUT,
                "team_assignment must be 'fixed' or 'random'",
            )
        if host_team not in ("blue", "red"):
            return _error(ErrorCode.BAD_INPUT, "host_team must be 'blue' or 'red'")
        if fog_of_war not in ("none", "classic", "line_of_sight"):
            return _error(
                ErrorCode.BAD_INPUT,
                "fog_of_war must be 'none' | 'classic' | 'line_of_sight'",
            )
        try:
            load_scenario(scenario)
        except Exception as e:
            return _error(ErrorCode.BAD_INPUT, f"scenario load failed: {e}")
        config = RoomConfig(
            scenario=scenario,
            max_turns=max_turns,
            team_assignment=team_assignment,  # type: ignore[arg-type]
            host_team=host_team,  # type: ignore[arg-type]
            fog_of_war=fog_of_war,  # type: ignore[arg-type]
            turn_time_limit_s=turn_time_limit_s,
        )
        room, slot = app.rooms.create(config=config, host=conn.player)
        app.conn_to_room[connection_id] = (room.id, slot)
        conn.state = ConnectionState.IN_ROOM
        return _ok({"room_id": room.id, "slot": slot.value})

    @mcp.tool()
    def join_room(connection_id: str, room_id: str) -> dict:
        """Take the open seat in an existing room."""
        conn = app.get_connection(connection_id)
        if conn is None or conn.state != ConnectionState.IN_LOBBY:
            return _error(
                ErrorCode.TOOL_NOT_AVAILABLE_IN_STATE,
                "join_room requires state=in_lobby",
            )
        if conn.player is None:
            return _error(ErrorCode.BAD_INPUT, "set_player_metadata first")
        room = app.rooms.get(room_id)
        if room is None:
            return _error(ErrorCode.ROOM_NOT_FOUND, f"no such room: {room_id}")
        result = app.rooms.join(room_id, conn.player)
        if result is None:
            return _error(ErrorCode.ROOM_FULL, "room is full")
        _, slot = result
        app.conn_to_room[connection_id] = (room_id, slot)
        conn.state = ConnectionState.IN_ROOM
        return _ok({"room_id": room_id, "slot": slot.value})

    @mcp.tool()
    async def leave_room(connection_id: str) -> dict:
        """Vacate this connection's seat and return the caller to the lobby.

        Accepts from IN_ROOM (pre-game) OR IN_GAME (mid-match or
        post-match). Mid-match departures are treated as a hard exit —
        the opponent will auto-concede via the heartbeat sweeper if
        they don't press anything. Post-match departures are the
        normal 'back to lobby' flow.
        """
        conn = app.get_connection(connection_id)
        if conn is None or conn.state not in (
            ConnectionState.IN_ROOM,
            ConnectionState.IN_GAME,
        ):
            return _error(
                ErrorCode.TOOL_NOT_AVAILABLE_IN_STATE,
                "leave_room requires state=in_room or in_game",
            )
        info = app.conn_to_room.pop(connection_id, None)
        if info is None:
            return _error(ErrorCode.NOT_IN_ROOM, "connection not seated")
        room_id, slot = info
        _cancel_countdown(app, room_id)
        app.rooms.leave(room_id, slot)
        conn.state = ConnectionState.IN_LOBBY
        # If the leave deleted the room (post-match + last player
        # out), clean up the companion per-room maps so memory doesn't
        # pile up across matches.
        if app.rooms.get(room_id) is None:
            app.sessions.pop(room_id, None)
            app.slot_to_team.pop(room_id, None)
            log.info("room %s fully cleaned up", room_id)
        return _ok({})

    @mcp.tool()
    async def get_room_state(connection_id: str) -> dict:
        """Show the caller's current room, seats, readiness, and countdown."""
        conn = app.get_connection(connection_id)
        if conn is None or conn.state not in (
            ConnectionState.IN_ROOM,
            ConnectionState.IN_GAME,
        ):
            return _error(
                ErrorCode.TOOL_NOT_AVAILABLE_IN_STATE,
                "get_room_state requires state=in_room or in_game",
            )
        info = app.conn_to_room.get(connection_id)
        if info is None:
            return _error(ErrorCode.NOT_IN_ROOM, "connection not seated")
        room_id, _slot = info
        room = app.rooms.get(room_id)
        if room is None:
            return _error(ErrorCode.ROOM_NOT_FOUND, f"room {room_id} vanished")

        # Belt-and-suspenders: if we're polled after the deadline has
        # passed but the background _run_countdown task hasn't fired
        # (FastMCP dispatch / event-loop context quirks have been known
        # to drop the task), promote the room inline. At worst a client
        # observes the transition one poll later than it would have.
        _maybe_promote_on_deadline(app, room_id)

        # Re-read the room after potential inline promotion.
        room = app.rooms.get(room_id)
        if room is None:
            return _error(ErrorCode.ROOM_NOT_FOUND, f"room {room_id} vanished")
        summary = _serialize_room_summary(room)
        countdown = app.autostart_deadlines.get(room_id)
        if countdown is not None:
            import time

            summary["autostart_in_s"] = max(0.0, countdown - time.time())
        return _ok({"room": summary})

    @mcp.tool()
    async def set_ready(connection_id: str, ready: bool) -> dict:
        """Toggle this connection's readiness.

        When both seats are filled and both ready, the server starts a
        10s countdown after which the match begins. The countdown is
        cancelled if either player unreadies, leaves, or disconnects.
        """
        conn = app.get_connection(connection_id)
        if conn is None or conn.state != ConnectionState.IN_ROOM:
            return _error(
                ErrorCode.TOOL_NOT_AVAILABLE_IN_STATE,
                "set_ready requires state=in_room",
            )
        info = app.conn_to_room.get(connection_id)
        if info is None:
            return _error(ErrorCode.NOT_IN_ROOM, "connection not seated")
        room_id, slot = info
        room = app.rooms.get(room_id)
        if room is None:
            return _error(ErrorCode.ROOM_NOT_FOUND, f"room {room_id} vanished")
        seat = room.seats[slot]
        seat.ready = ready

        log.info(
            "set_ready: room=%s cid=%s slot=%s ready=%s all_ready=%s seats=%s",
            room_id,
            connection_id[:8],
            slot.value,
            ready,
            room.all_ready(),
            {s.value: (room.seats[s].ready, room.seats[s].player is not None) for s in room.seats},
        )

        response: dict[str, Any] = {}
        if room.all_ready():
            _start_countdown(app, room_id)
            deadline = app.autostart_deadlines.get(room_id)
            if deadline is not None:
                import time

                response["autostart_in_s"] = max(0.0, deadline - time.time())
            room.status = RoomStatus.COUNTING_DOWN
        else:
            _cancel_countdown(app, room_id)
            room.recompute_status()
        return _ok(response)


# ---- countdown machinery ----


def _start_countdown(app: App, room_id: str) -> None:
    """(Re)start a 10s autostart countdown for this room."""
    import time

    _cancel_countdown(app, room_id)
    deadline = time.time() + AUTOSTART_DELAY_S
    app.autostart_deadlines[room_id] = deadline
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    log.info(
        "start_countdown: room=%s deadline=%.2f delay=%.2f loop=%s",
        room_id,
        deadline,
        AUTOSTART_DELAY_S,
        loop,
    )
    try:
        task = asyncio.create_task(_run_countdown(app, room_id))
    except Exception as e:
        log.exception("start_countdown: create_task failed: %s", e)
        return
    app.autostart_tasks[room_id] = task


def _cancel_countdown(app: App, room_id: str) -> None:
    task = app.autostart_tasks.pop(room_id, None)
    if task is not None and not task.done():
        log.info("cancel_countdown: room=%s (task was running)", room_id)
        task.cancel()
    elif room_id in app.autostart_deadlines:
        log.info("cancel_countdown: room=%s (no live task)", room_id)
    app.autostart_deadlines.pop(room_id, None)


async def _run_countdown(app: App, room_id: str) -> None:
    log.info("run_countdown: room=%s sleeping %.2fs", room_id, AUTOSTART_DELAY_S)
    try:
        await asyncio.sleep(AUTOSTART_DELAY_S)
    except asyncio.CancelledError:
        log.info("run_countdown: room=%s cancelled", room_id)
        return
    log.info("run_countdown: room=%s slept through; checking room", room_id)
    room = app.rooms.get(room_id)
    if room is None or not room.all_ready():
        log.info(
            "run_countdown: room=%s not promoting (room=%s all_ready=%s)",
            room_id,
            room,
            room and room.all_ready(),
        )
        return
    if app.on_countdown_complete is not None:
        log.info("run_countdown: room=%s firing on_countdown_complete", room_id)
        app.on_countdown_complete(room_id)
    else:
        log.warning("run_countdown: room=%s on_countdown_complete is None", room_id)


def _maybe_promote_on_deadline(app: App, room_id: str) -> None:
    """Synchronous safety net: if the autostart deadline has passed,
    promote the room even if the background _run_countdown task didn't
    fire. Called from the polling-read path so every client's next poll
    (≤1s cadence) will observe the promotion.
    """
    import time

    deadline = app.autostart_deadlines.get(room_id)
    if deadline is None:
        return
    now = time.time()
    if now < deadline:
        return
    room = app.rooms.get(room_id)
    if room is None or not room.all_ready():
        log.info(
            "maybe_promote: room=%s deadline passed but not all ready; clearing",
            room_id,
        )
        app.autostart_deadlines.pop(room_id, None)
        return
    log.info(
        "maybe_promote: room=%s fallback firing (deadline %.2fs ago)",
        room_id,
        now - deadline,
    )
    # Clear deadline so we don't double-promote.
    app.autostart_deadlines.pop(room_id, None)
    task = app.autostart_tasks.pop(room_id, None)
    if task is not None and not task.done():
        task.cancel()
    if app.on_countdown_complete is not None:
        app.on_countdown_complete(room_id)
