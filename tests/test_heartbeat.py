"""Tests for the heartbeat sweeper and per-turn time limit.

Four rules:
  1. No heartbeat for HEARTBEAT_DEAD_S → evict (vacate room, concede).
  2. In room but not ready for UNREADY_TIMEOUT_S → evict to lobby.
  3. Per-turn timeout: force end_turn if active player has been
     sitting on their turn past room.config.turn_time_limit_s.
  4. auto_concede vacates the crashed player's seat so the room
     can GC when the opponent eventually leaves.
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


# ---- Rule 3: per-turn time limit ----

def _setup_in_game(app: App, *, turn_limit_s: int = 60) -> tuple[str, object]:
    """Create an in-game room + session with both seats taken."""
    host = PlayerMetadata(display_name="blue-host", kind="ai")
    room, _slot_a = app.rooms.create(
        config=RoomConfig(
            scenario="01_tiny_skirmish", turn_time_limit_s=turn_limit_s,
        ),
        host=host,
    )
    app.rooms.join(room.id, PlayerMetadata(display_name="red-joiner", kind="ai"))
    state = load_scenario("01_tiny_skirmish")
    session = new_session(state, scenario="01_tiny_skirmish")
    app.sessions[room.id] = session
    app.slot_to_team[room.id] = {Slot.A: Team.BLUE, Slot.B: Team.RED}

    blue = _seat(app, "blue", ConnectionState.IN_GAME)
    app.conn_to_room["blue"] = (room.id, Slot.A)
    red = _seat(app, "red", ConnectionState.IN_GAME)
    app.conn_to_room["red"] = (room.id, Slot.B)
    # Keep heartbeats fresh — this test exercises the turn-timeout
    # path, NOT the heartbeat-dead path.
    now = time.time()
    blue.last_heartbeat_at = now
    red.last_heartbeat_at = now
    return room.id, session


def test_turn_timeout_forces_end_turn():
    """An active player whose turn has exceeded turn_time_limit_s gets
    their turn force-ended by the sweeper; control passes to opponent."""
    app = App()
    room_id, session = _setup_in_game(app, turn_limit_s=60)
    active_before = session.state.active_player
    # Backdate the turn start so we're past the limit.
    session.turn_start_time = time.monotonic() - 120  # 2 min past start, limit 60
    # Pre-state: still IN_PROGRESS
    assert session.state.status == GameStatus.IN_PROGRESS

    run_sweep_once(app, now=time.time())

    # Turn flipped.
    assert session.state.active_player != active_before
    # Game NOT over — turn timeout only ends the turn, doesn't concede.
    assert session.state.status == GameStatus.IN_PROGRESS
    # Replay log contains the forfeit event.
    # (session.log writes via ReplayWriter if present, which the test
    # session doesn't have; but we can assert it didn't raise.)


def test_turn_timeout_skips_when_game_already_over():
    """If the game ended via some other path (concede, heartbeat_dead)
    before the sweeper reaches the turn-timeout rule, the turn-timeout
    rule must no-op rather than trying to end a turn on a dead game."""
    app = App()
    room_id, session = _setup_in_game(app, turn_limit_s=60)
    session.turn_start_time = time.monotonic() - 120
    session.state.status = GameStatus.GAME_OVER  # simulate prior concede

    run_sweep_once(app, now=time.time())

    # Still GAME_OVER — the turn-timeout rule didn't re-trigger end_turn.
    assert session.state.status == GameStatus.GAME_OVER


def test_turn_timeout_skips_when_turn_start_time_is_zero():
    """If a session's turn_start_time is 0 (e.g. it was just promoted
    to IN_PROGRESS but the first turn hasn't started ticking yet), the
    sweeper must not misread elapsed as 'huge' and force-end."""
    app = App()
    room_id, session = _setup_in_game(app, turn_limit_s=60)
    session.turn_start_time = 0.0

    run_sweep_once(app, now=time.time())

    # No force-end happened; turn_start_time stays at 0.
    assert session.turn_start_time == 0.0
    assert session.state.status == GameStatus.IN_PROGRESS


def test_turn_timeout_resets_turn_start_for_new_active_player():
    """After a force-ended turn, the new active player's turn_start_time
    is reset so THEIR clock starts fresh."""
    app = App()
    room_id, session = _setup_in_game(app, turn_limit_s=60)
    session.turn_start_time = time.monotonic() - 120

    before = time.monotonic()
    run_sweep_once(app, now=time.time())
    after = time.monotonic()

    # New turn's start time is recent (within the sweep window).
    assert before - 0.1 <= session.turn_start_time <= after + 0.1


def test_turn_timeout_bypasses_pending_units_guard():
    """Even if the active player has units in MOVED status (moved but
    hadn't finalized with attack/heal/wait), force_end_turn must still
    succeed — normal end_turn would reject with 'N units still pending'."""
    from silicon_pantheon.server.engine.state import UnitStatus
    app = App()
    room_id, session = _setup_in_game(app, turn_limit_s=60)
    session.turn_start_time = time.monotonic() - 120

    # Put one of the active player's units in MOVED so normal
    # end_turn would reject.
    active = session.state.active_player
    moved = None
    for u in session.state.units_of(active):
        u.status = UnitStatus.MOVED
        moved = u
        break
    assert moved is not None

    run_sweep_once(app, now=time.time())

    # The forced end_turn went through — turn flipped to opponent.
    assert session.state.active_player != active


# ---- Rule 4: auto_concede also vacates the crashed seat ----

def test_auto_concede_vacates_crashed_seat_for_room_gc():
    """When a connection crashes mid-game and heartbeat_dead fires,
    auto_concede now vacates that seat so the room can GC when the
    opponent eventually leaves. Previously the seat stayed occupied
    forever and the room lingered in the lobby list."""
    app = App()
    room_id, session = _setup_in_game(app, turn_limit_s=60)

    # Red crashes (heartbeat goes stale), blue stays alive.
    t0 = time.time()
    app.get_connection("red").last_heartbeat_at = t0 - HEARTBEAT_DEAD_S - 1
    app.get_connection("blue").last_heartbeat_at = t0

    run_sweep_once(app, now=t0)

    # Red conn gone, red seat vacated.
    assert app.get_connection("red") is None
    assert "red" not in app.conn_to_room
    room = app.rooms.get(room_id)
    assert room is not None
    assert room.seats[Slot.B].player is None, (
        "red's seat should be vacated after auto_concede so the room "
        "can GC once blue leaves"
    )
    # Blue's seat still occupied — blue is still a live player.
    assert room.seats[Slot.A].player is not None


def test_auto_concede_then_opponent_leaves_cleans_up_room():
    """Full end-to-end: red crashes mid-game, blue eventually leaves
    (via regular leave_room flow), room is deleted."""
    from silicon_pantheon.server.rooms import Slot as _Slot
    app = App()
    room_id, session = _setup_in_game(app, turn_limit_s=60)

    # Red dies.
    t0 = time.time()
    app.get_connection("red").last_heartbeat_at = t0 - HEARTBEAT_DEAD_S - 1
    app.get_connection("blue").last_heartbeat_at = t0
    run_sweep_once(app, now=t0)

    # Simulate blue doing the normal leave_room flow.
    app.conn_to_room.pop("blue", None)
    app.rooms.leave(room_id, _Slot.A)

    # Room is gone.
    assert app.rooms.get(room_id) is None, (
        "room should be deleted after both seats free and status FINISHED"
    )
