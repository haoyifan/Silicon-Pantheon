"""Tests for the simplified heartbeat sweeper.

The new model has two rules:
  1. No heartbeat for HEARTBEAT_DEAD_S → evict (vacate room, concede game).
  2. In room but not ready for UNREADY_TIMEOUT_S → evict to lobby.
"""

from __future__ import annotations

import time

from silicon_pantheon.server.app import App, Connection
from silicon_pantheon.server.engine.scenarios import load_scenario
from silicon_pantheon.server.engine.state import GameStatus, Team
from silicon_pantheon.server.heartbeat import (
    HEARTBEAT_DEAD_S,
    UNREADY_TIMEOUT_S,
    HeartbeatState,
    run_sweep_once,
)
from silicon_pantheon.server.rooms import RoomConfig, Slot
from silicon_pantheon.server.session import new_session
from silicon_pantheon.shared.player_metadata import PlayerMetadata
from silicon_pantheon.shared.protocol import ConnectionState


def _seat(app: App, cid: str, state: ConnectionState) -> Connection:
    conn = app.ensure_connection(cid)
    conn.player = PlayerMetadata(display_name=cid, kind="ai")
    conn.state = state
    return conn


def test_fresh_connection_not_evicted():
    app = App()
    conn = _seat(app, "c1", ConnectionState.IN_LOBBY)
    conn.last_heartbeat_at = time.time()
    run_sweep_once(app, now=time.time())
    assert app.get_connection("c1") is not None


def test_dead_heartbeat_lobby_evicted():
    """No heartbeat for HEARTBEAT_DEAD_S → connection dropped."""
    app = App()
    t0 = 1_000_000.0
    conn = _seat(app, "c1", ConnectionState.IN_LOBBY)
    conn.last_heartbeat_at = t0 - HEARTBEAT_DEAD_S - 1
    run_sweep_once(app, now=t0)
    assert app.get_connection("c1") is None


def test_dead_heartbeat_room_vacates_seat():
    app = App()
    t0 = 1_000_000.0
    host = PlayerMetadata(display_name="alice", kind="ai")
    room, slot = app.rooms.create(
        config=RoomConfig(scenario="01_tiny_skirmish"), host=host
    )
    cid = "c1"
    conn = _seat(app, cid, ConnectionState.IN_ROOM)
    app.conn_to_room[cid] = (room.id, slot)
    conn.last_heartbeat_at = t0 - HEARTBEAT_DEAD_S - 1
    run_sweep_once(app, now=t0)
    assert app.get_connection(cid) is None
    assert cid not in app.conn_to_room


def test_dead_heartbeat_game_concedes():
    app = App()
    t0 = 1_000_000.0
    host = PlayerMetadata(display_name="alice", kind="ai")
    room, slot_a = app.rooms.create(
        config=RoomConfig(scenario="01_tiny_skirmish"), host=host
    )
    app.rooms.join(room.id, PlayerMetadata(display_name="bob", kind="ai"))

    state = load_scenario("01_tiny_skirmish")
    session = new_session(state, scenario="01_tiny_skirmish")
    app.sessions[room.id] = session
    app.slot_to_team[room.id] = {Slot.A: Team.BLUE, Slot.B: Team.RED}

    blue_conn = _seat(app, "blue", ConnectionState.IN_GAME)
    app.conn_to_room["blue"] = (room.id, Slot.A)
    red_conn = _seat(app, "red", ConnectionState.IN_GAME)
    app.conn_to_room["red"] = (room.id, Slot.B)

    # Blue is alive, red is dead.
    blue_conn.last_heartbeat_at = t0
    red_conn.last_heartbeat_at = t0 - HEARTBEAT_DEAD_S - 1

    run_sweep_once(app, now=t0)

    assert session.state.status == GameStatus.GAME_OVER
    assert session.state.winner == Team.BLUE
    assert app.get_connection("red") is None
    assert app.get_connection("blue") is not None


def test_alive_heartbeat_in_game_not_evicted():
    """Heartbeat is fresh → never evicted, even if game activity is stale."""
    app = App()
    t0 = 1_000_000.0
    host = PlayerMetadata(display_name="h", kind="human")
    room, _ = app.rooms.create(
        config=RoomConfig(scenario="01_tiny_skirmish"), host=host
    )
    from silicon_pantheon.server.rooms import RoomStatus
    room.status = RoomStatus.IN_GAME
    state = load_scenario("01_tiny_skirmish")
    session = new_session(state)
    app.sessions[room.id] = session
    app.slot_to_team[room.id] = {Slot.A: Team.BLUE, Slot.B: Team.RED}

    blue_conn = _seat(app, "blue", ConnectionState.IN_GAME)
    app.conn_to_room["blue"] = (room.id, Slot.A)

    # Heartbeat keeps coming (client is alive), but no game activity
    # for 5 minutes. The sweeper should NOT evict.
    blue_conn.last_heartbeat_at = t0 + 300
    run_sweep_once(app, now=t0 + 300)
    assert app.get_connection("blue") is not None
    assert session.state.status != GameStatus.GAME_OVER


def test_unready_timeout_evicts_to_lobby():
    """Player in room who doesn't ready up within timeout gets evicted."""
    app = App()
    t0 = 1_000_000.0
    host = PlayerMetadata(display_name="alice", kind="ai")
    room, slot = app.rooms.create(
        config=RoomConfig(scenario="01_tiny_skirmish"), host=host
    )
    joiner = PlayerMetadata(display_name="bob", kind="ai")
    app.rooms.join(room.id, joiner)

    cid = "joiner"
    conn = _seat(app, cid, ConnectionState.IN_ROOM)
    app.conn_to_room[cid] = (room.id, Slot.B)
    conn.last_heartbeat_at = t0
    app.heartbeat_state[cid] = HeartbeatState(joined_room_at=t0)
    room.seats[Slot.B].ready = False

    # Before timeout: heartbeat is fresh, still there.
    t1 = t0 + UNREADY_TIMEOUT_S - 1
    conn.last_heartbeat_at = t1
    run_sweep_once(app, now=t1)
    assert app.get_connection(cid) is not None

    # After timeout: heartbeat fresh but unready too long → evicted to lobby.
    t2 = t0 + UNREADY_TIMEOUT_S + 1
    conn.last_heartbeat_at = t2
    run_sweep_once(app, now=t2)
    assert app.get_connection(cid) is not None  # still alive
    assert conn.state == ConnectionState.IN_LOBBY  # back to lobby
    assert cid not in app.conn_to_room


def test_sweeper_idempotent_for_live_connection():
    app = App()
    now = 1_000_000.0
    conn = _seat(app, "c1", ConnectionState.IN_LOBBY)
    conn.last_heartbeat_at = now
    run_sweep_once(app, now=now)
    run_sweep_once(app, now=now + 0.5)
    assert app.get_connection("c1") is not None
