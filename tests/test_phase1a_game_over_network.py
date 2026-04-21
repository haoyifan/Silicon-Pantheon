"""Phase 1a.13 — two clients exercise game tools over MCP+SSE.

Proves the end-to-end path:
  client A  --MCP+SSE-->  server  --in-process-->  engine

without needing a full random-bot orchestrator. The flow is
scripted: connect both, host + join, each side takes one concrete
action, verify state mutations land authoritatively on the server
and the opposing client sees them on its next get_state call.
"""

from __future__ import annotations

import asyncio
import socket
import threading
import time

import pytest
import uvicorn

from silicon_pantheon.client.transport import ServerClient
from silicon_pantheon.server.app import App, build_mcp_server
from silicon_pantheon.shared.protocol import ConnectionState


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def server():
    app = App()
    mcp = build_mcp_server(app)
    port = _free_port()
    starlette_app = mcp.streamable_http_app()
    config = uvicorn.Config(
        app=starlette_app, host="127.0.0.1", port=port, log_level="warning"
    )
    srv = uvicorn.Server(config)
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    deadline = time.time() + 10.0
    while time.time() < deadline and not srv.started:
        time.sleep(0.05)
    if not srv.started:
        raise RuntimeError("uvicorn failed to start")
    try:
        yield f"http://127.0.0.1:{port}/mcp/", app
    finally:
        srv.should_exit = True
        thread.join(timeout=5.0)


def test_two_clients_host_join_and_act(server) -> None:
    url, app = server

    async def go() -> None:
        async with (
            ServerClient.connect(url) as blue,
            ServerClient.connect(url) as red,
        ):
            # 1. Both declare themselves.
            await blue.call("set_player_metadata", display_name="alice", kind="ai")
            await red.call("set_player_metadata", display_name="bob", kind="ai")

            # 2. Blue hosts; Red joins → game starts.
            r = await blue.call("create_dev_game", scenario="01_tiny_skirmish")
            assert r["ok"] is True and r["slot"] == "a"
            r = await red.call("join_dev_game")
            assert r["ok"] is True and r["slot"] == "b"

            # 3. Server should now have both connections IN_GAME.
            for cid in (blue.connection_id, red.connection_id):
                conn = app.get_connection(cid)
                assert conn is not None
                assert conn.state == ConnectionState.IN_GAME

            # 4. Blue reads state, confirms it's blue's turn on turn 1.
            r = await blue.call("get_state")
            assert r["ok"] is True
            gs = r["result"]
            assert gs["turn"] == 1
            assert gs["active_player"] == "blue"

            # 5. Red calling a turn-gated tool in blue's turn should error.
            #    (`move` requires active_player == viewer.)
            r = await red.call("end_turn")
            assert r["ok"] is False  # not your turn

            # 6. Blue ends turn (no moves — still valid if no mid-action units).
            r = await blue.call("end_turn")
            assert r["ok"] is True

            # 7. After end_turn, active_player flips to red and red can act.
            r = await red.call("get_state")
            assert r["ok"] is True
            assert r["result"]["active_player"] == "red"
            r = await red.call("end_turn")
            assert r["ok"] is True

            # 8. One full round completed — turn counter bumped to 2.
            r = await blue.call("get_state")
            assert r["ok"] is True
            assert r["result"]["turn"] == 2

    asyncio.run(go())


def test_game_tool_rejects_in_lobby_connection(server) -> None:
    url, _app = server

    async def go() -> None:
        async with ServerClient.connect(url) as c:
            # Declared → IN_LOBBY. Game tool should refuse on state.
            await c.call("set_player_metadata", display_name="alice", kind="ai")
            r = await c.call("get_state")
            assert r["ok"] is False
            assert r["error"]["code"] == "tool_not_available_in_state"

    asyncio.run(go())


def test_game_tool_rejects_unknown_connection(server) -> None:
    url, _app = server

    async def go() -> None:
        async with ServerClient.connect(url) as c:
            # Fresh connection, never called set_player_metadata.
            # `_dispatch` sees an unknown connection_id and returns
            # NOT_REGISTERED — only set_player_metadata creates connections.
            r = await c.call("get_state")
            assert r["ok"] is False
            assert r["error"]["code"] == "not_registered"

    asyncio.run(go())


def test_leave_room_mid_game_auto_concedes(server) -> None:
    """Regression: leave_room on an IN_GAME room used to leave a zombie
    room where the opponent was stranded. Fix: auto-concede the leaver's
    team so the opponent wins and the room transitions FINISHED.

    Observed in production 2026-04-20: Minion's worker hit an xAI
    provider timeout mid-match, its `_disconnect()` called leave_room,
    and the room ``ca828db0`` sat in_game for hours with the opponent
    polling get_state waiting for blue to move.
    """
    url, app = server
    from silicon_pantheon.server.engine.state import GameStatus
    from silicon_pantheon.server.rooms import RoomStatus

    async def go() -> None:
        async with (
            ServerClient.connect(url) as blue,
            ServerClient.connect(url) as red,
        ):
            await blue.call("set_player_metadata", display_name="alice", kind="ai")
            await red.call("set_player_metadata", display_name="bob", kind="ai")
            r = await blue.call("create_dev_game", scenario="01_tiny_skirmish")
            assert r["ok"] is True
            r = await red.call("join_dev_game")
            assert r["ok"] is True

            # Both connections are IN_GAME with a live session.
            # (join_dev_game is a dev shortcut that does NOT flip
            # room.status to IN_GAME — it only installs a session
            # and sets conn.state. The leave_room auto-concede trigger
            # checks conn.state + live session, not room.status, so
            # this still qualifies as a mid-game leave.)
            room_id = app.conn_to_room[blue.connection_id][0]
            blue_conn = app.get_connection(blue.connection_id)
            assert blue_conn is not None
            assert blue_conn.state == ConnectionState.IN_GAME
            session = app.sessions.get(room_id)
            assert session is not None
            assert session.state.status != GameStatus.GAME_OVER

            # Blue hard-exits mid-game via leave_room.
            r = await blue.call("leave_room")
            assert r["ok"] is True

            # The game must be marked GAME_OVER with red (the opponent)
            # as winner — auto-concede triggered.
            assert session.state.status == GameStatus.GAME_OVER
            assert session.state.winner.value == "red", (
                f"blue left mid-match → red should win by concede; "
                f"got winner={session.state.winner}"
            )
            # Room must have transitioned to FINISHED so the lobby
            # doesn't show it as in_game forever.
            room = app.rooms.get(room_id)
            # Room may or may not have been deleted depending on
            # whether red left too; if still present, status must be
            # FINISHED (not IN_GAME / not WAITING_*).
            if room is not None:
                assert room.status == RoomStatus.FINISHED, (
                    f"room should be FINISHED after mid-game leave; "
                    f"got {room.status}"
                )
            # Blue is back in lobby.
            conn = app.get_connection(blue.connection_id)
            assert conn is not None
            assert conn.state == ConnectionState.IN_LOBBY

    asyncio.run(go())


def test_leave_room_after_opponent_auto_conceded(server) -> None:
    """Regression: when the opponent was already auto-conceded by the
    heartbeat sweeper (their seat vacated, session marked GAME_OVER),
    pupil-grok calling leave_room used to crash with 'session vanished
    before game_over check for room=...'. Observed 2026-04-20 on the
    27_battle_of_zion_dock match: Napoleon's get_legal_actions hung
    server-side for 90s, blocking its heartbeat. Server auto-conceded
    Napoleon → blue wins. When the surviving pupil-grok client
    called leave_room, the debug-mode invariant tripped.

    Fix: Phase 4 of leave_room guards the _note_game_over_if_needed
    call on session existence — if Phase 3 already deleted the room
    (both seats gone, session popped), the bookkeeping is already
    done and the follow-up call is correctly a no-op."""
    url, app = server
    from silicon_pantheon.server.engine.state import GameStatus, Team
    from silicon_pantheon.server.rooms import Slot
    from silicon_pantheon.server.tools.mutations import concede as _concede_tool

    async def go() -> None:
        async with (
            ServerClient.connect(url) as blue,
            ServerClient.connect(url) as red,
        ):
            await blue.call("set_player_metadata", display_name="alice", kind="ai")
            await red.call("set_player_metadata", display_name="bob", kind="ai")
            await blue.call("create_dev_game", scenario="01_tiny_skirmish")
            await red.call("join_dev_game")

            room_id = app.conn_to_room[blue.connection_id][0]
            session = app.sessions[room_id]
            # Simulate the opponent-already-auto-conceded state: red
            # is vacated from the room and the session is marked
            # GAME_OVER (blue wins). Mirrors what heartbeat._auto_concede
            # does when a client's heartbeat dies.
            with session.lock:
                _concede_tool(session, Team.RED)  # red (Napoleon) loses
                assert session.state.status == GameStatus.GAME_OVER
                assert session.state.winner == Team.BLUE
            with app.state_lock():
                app.conn_to_room.pop(red.connection_id, None)
                app.rooms.leave(room_id, Slot.B)
                red_conn = app.get_connection(red.connection_id)
                if red_conn is not None:
                    red_conn.state = ConnectionState.IN_LOBBY

            # Now blue (the survivor) calls leave_room. Pre-fix this
            # crashed in debug mode because Phase 3 deleted the last
            # seat, popped app.sessions, and Phase 4 then hit the
            # session-vanished invariant.
            r = await blue.call("leave_room")
            assert r["ok"] is True, f"leave_room after auto-concede failed: {r}"
            # Room fully cleaned up.
            assert app.rooms.get(room_id) is None
            assert app.sessions.get(room_id) is None

    asyncio.run(go())


def test_leave_room_pre_game_unchanged(server) -> None:
    """Sanity: the pre-game leave_room path (solo host, no match) is
    unchanged by the auto-concede fix — it still evicts the opponent
    from a waiting room and deletes it cleanly."""
    url, app = server

    async def go() -> None:
        async with ServerClient.connect(url) as blue:
            await blue.call("set_player_metadata", display_name="alice", kind="ai")
            r = await blue.call(
                "create_room", scenario="01_tiny_skirmish", fog_of_war="none"
            )
            room_id = r["room_id"]
            r = await blue.call("leave_room")
            assert r["ok"] is True
            # Empty room deleted.
            assert app.rooms.get(room_id) is None
            # No stray session/slot_to_team.
            assert app.sessions.get(room_id) is None

    asyncio.run(go())
