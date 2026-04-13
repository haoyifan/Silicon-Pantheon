"""Tests for the heartbeat sweeper + disconnect state machine.

Calls `run_sweep_once(app, now=...)` directly so transitions are
deterministic and don't require sleeping through real grace windows.
"""

from __future__ import annotations

import time

from clash_of_robots.server.app import App, Connection
from clash_of_robots.server.engine.scenarios import load_scenario
from clash_of_robots.server.engine.state import GameStatus, Team
from clash_of_robots.server.heartbeat import (
    HEARTBEAT_GRACE_S,
    IN_GAME_HARD_CONCEDE_S,
    IN_GAME_SOFT_NOTICE_S,
    IN_ROOM_EVICT_S,
    run_sweep_once,
)
from clash_of_robots.server.rooms import RoomConfig, Slot
from clash_of_robots.server.session import new_session
from clash_of_robots.shared.player_metadata import PlayerMetadata
from clash_of_robots.shared.protocol import ConnectionState


def _seat(app: App, cid: str, team: str, state: ConnectionState) -> Connection:
    conn = app.ensure_connection(cid)
    conn.player = PlayerMetadata(display_name=cid, kind="ai")
    conn.state = state
    return conn


def test_fresh_connection_not_disconnected() -> None:
    app = App()
    conn = _seat(app, "c1", "a", ConnectionState.IN_LOBBY)
    conn.last_heartbeat_at = time.time()
    run_sweep_once(app, now=time.time())
    assert app.get_connection("c1") is not None


def test_in_lobby_evicted_after_grace() -> None:
    """Two sweeps: first marks soft-disconnect, second evicts."""
    app = App()
    t0 = 1_000_000.0
    conn = _seat(app, "c1", "a", ConnectionState.IN_LOBBY)
    conn.last_heartbeat_at = t0 - (HEARTBEAT_GRACE_S + 1)
    run_sweep_once(app, now=t0)  # → soft
    assert app.get_connection("c1") is not None
    run_sweep_once(app, now=t0 + HEARTBEAT_GRACE_S + 1)  # → evicted
    assert app.get_connection("c1") is None


def test_in_room_soft_then_evict_vacates_seat() -> None:
    app = App()
    t0 = 1_000_000.0
    host = PlayerMetadata(display_name="alice", kind="ai")
    room, slot = app.rooms.create(
        config=RoomConfig(scenario="01_tiny_skirmish"), host=host
    )
    cid = "c1"
    conn = _seat(app, cid, "a", ConnectionState.IN_ROOM)
    app.conn_to_room[cid] = (room.id, slot)
    conn.last_heartbeat_at = t0 - (HEARTBEAT_GRACE_S + 1)
    run_sweep_once(app, now=t0)  # mark soft
    assert app.get_connection(cid) is not None
    run_sweep_once(app, now=t0 + IN_ROOM_EVICT_S + 1)  # evict
    assert app.get_connection(cid) is None
    assert cid not in app.conn_to_room


def test_in_game_soft_notice_logs_then_hard_forfeit() -> None:
    app = App()
    now = 1_000_000.0
    host = PlayerMetadata(display_name="alice", kind="ai")
    room, slot_a = app.rooms.create(
        config=RoomConfig(scenario="01_tiny_skirmish"), host=host
    )
    room2 = app.rooms.join(room.id, PlayerMetadata(display_name="bob", kind="ai"))
    assert room2 is not None

    # Spin up a session + slot→team mapping mimicking start_game_for_room.
    state = load_scenario("01_tiny_skirmish")
    session = new_session(state, scenario="01_tiny_skirmish")
    app.sessions[room.id] = session
    app.slot_to_team[room.id] = {Slot.A: Team.BLUE, Slot.B: Team.RED}

    # Seat two connections.
    blue_conn = _seat(app, "blue", "a", ConnectionState.IN_GAME)
    app.conn_to_room["blue"] = (room.id, Slot.A)
    red_conn = _seat(app, "red", "b", ConnectionState.IN_GAME)
    app.conn_to_room["red"] = (room.id, Slot.B)

    # T0: red goes silent just past grace. Sweep marks soft_disconnected_at.
    t0 = 1_000_000.0
    red_conn.last_heartbeat_at = t0 - (HEARTBEAT_GRACE_S + 1)
    blue_conn.last_heartbeat_at = t0
    run_sweep_once(app, now=t0)
    assert app.get_connection("red") is not None

    # T0 + soft-notice: sweep emits notice but does not yet forfeit.
    t1 = t0 + IN_GAME_SOFT_NOTICE_S + 1
    blue_conn.last_heartbeat_at = t1
    run_sweep_once(app, now=t1)
    assert session.state.status != GameStatus.GAME_OVER
    assert app.get_connection("red") is not None

    # T0 + hard-concede: sweep forfeits.
    t2 = t0 + IN_GAME_HARD_CONCEDE_S + 1
    blue_conn.last_heartbeat_at = t2
    run_sweep_once(app, now=t2)
    assert session.state.status == GameStatus.GAME_OVER
    assert session.state.winner == Team.BLUE
    assert app.get_connection("red") is None


def test_sweeper_idempotent_for_live_connection() -> None:
    app = App()
    now = 1_000_000.0
    conn = _seat(app, "c1", "a", ConnectionState.IN_LOBBY)
    conn.last_heartbeat_at = now
    run_sweep_once(app, now=now)
    run_sweep_once(app, now=now + 0.5)
    assert app.get_connection("c1") is not None
