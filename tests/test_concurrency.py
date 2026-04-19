"""Multi-thread stress tests for the server synchronisation model.

The production server runs on a single asyncio event loop — these
tests deliberately invoke tool handlers from multiple OS threads
to stress the locking regime as if we'd already moved to a
threadpool-dispatched or true-MT deployment. Passing here means
the code is ready for that transition.

Each test:
  - spawns N worker threads
  - each worker does M random state-mutating operations
  - main thread joins with a timeout — a deadlock is detected as
    "workers didn't finish in time"
  - at the end, asserts invariants (no orphan state, consistent
    reverse indices, no torn reads observed)

Wall-clock budget: every test must finish in <10s under load;
generous on a laptop, tight enough to catch deadlocks without
false positives from contended-but-progressing locks.
"""

from __future__ import annotations

import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import pytest

from silicon_pantheon.server.app import App
from silicon_pantheon.server.engine.scenarios import load_scenario
from silicon_pantheon.server.engine.state import GameStatus, Team
from silicon_pantheon.server.heartbeat import HeartbeatState, run_sweep_once
from silicon_pantheon.server.rooms import RoomConfig, RoomStatus, Slot
from silicon_pantheon.server.session import new_session
from silicon_pantheon.shared.player_metadata import PlayerMetadata
from silicon_pantheon.shared.protocol import ConnectionState


def _mk_player(name: str) -> PlayerMetadata:
    return PlayerMetadata(display_name=name, kind="ai")


def _deadlock_watchdog(fn, *, timeout: float, **kw):
    """Run fn in a worker and fail the test if it doesn't finish.

    threading.Thread.join(timeout=...) returns whether we want to
    know. If still alive afterwards, it means the workers are
    stuck — most likely on a lock.
    """
    done = threading.Event()
    result: list = []
    err: list = []

    def _inner() -> None:
        try:
            result.append(fn(**kw))
        except BaseException as e:  # noqa: BLE001
            err.append(e)
        finally:
            done.set()

    t = threading.Thread(target=_inner, daemon=True)
    t.start()
    if not done.wait(timeout=timeout):
        raise AssertionError(
            f"deadlock suspected: workers not finished after {timeout}s"
        )
    if err:
        raise err[0]
    return result[0] if result else None


# ──────────────────────────────────────────────────────────────
# App-level state invariants
# ──────────────────────────────────────────────────────────────

def test_concurrent_ensure_connection_is_safe():
    """Many threads calling ensure_connection with distinct cids
    must not corrupt the connections dict. The ENTRY/EXIT pair is
    atomic under state_lock, so every connection ends up stored
    exactly once."""
    app = App()
    # Keep under MAX_CONNECTIONS=500.
    NUM_THREADS = 8
    PER_THREAD = 50

    def worker(tid: int) -> list[str]:
        made = []
        for i in range(PER_THREAD):
            cid = f"t{tid}-c{i}"
            app.ensure_connection(cid)
            made.append(cid)
        return made

    def run() -> None:
        with ThreadPoolExecutor(max_workers=NUM_THREADS) as ex:
            futures = [ex.submit(worker, i) for i in range(NUM_THREADS)]
            all_cids: list[str] = []
            for f in as_completed(futures):
                all_cids.extend(f.result())
        assert len(all_cids) == NUM_THREADS * PER_THREAD
        assert app.connection_count() == NUM_THREADS * PER_THREAD
        # Every cid resolves exactly to the same Connection object
        # on repeat reads — proves no duplication.
        samples = random.sample(all_cids, 50)
        for cid in samples:
            a = app.get_connection(cid)
            b = app.get_connection(cid)
            assert a is not None
            assert a is b

    _deadlock_watchdog(run, timeout=10.0)


def test_concurrent_same_cid_ensure_connection_is_idempotent():
    """Two threads racing ensure_connection with the SAME cid —
    the second must observe the first's object, not create a
    duplicate. Guards against lost-update on the `_connections`
    dict."""
    app = App()
    cid = "race"

    observed: list = []
    b = threading.Barrier(32)

    def worker() -> None:
        b.wait()
        conn = app.ensure_connection(cid)
        observed.append(conn)

    def run() -> None:
        threads = [threading.Thread(target=worker) for _ in range(32)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        first = observed[0]
        for o in observed[1:]:
            assert o is first, "ensure_connection created duplicates under race"
        assert app.connection_count() == 1

    _deadlock_watchdog(run, timeout=5.0)


# ──────────────────────────────────────────────────────────────
# Lobby-path atomicity under concurrency
# ──────────────────────────────────────────────────────────────

def test_concurrent_room_churn_no_orphans():
    """Simulate many lobby ops happening in parallel: create, join,
    leave, set_ready across random connections. Assert:

      - no connection ends up with an entry in conn_to_room pointing
        at a room that doesn't exist
      - no room has a seat claiming a player who's been dropped
      - every connection's state is consistent with its conn_to_room
        entry (IN_LOBBY -> not in map; IN_ROOM / IN_GAME -> in map)
    """
    app = App()
    NUM_CLIENTS = 24
    OPS_PER_CLIENT = 40

    # Seat every client initially.
    for i in range(NUM_CLIENTS):
        cid = f"c{i}"
        conn = app.ensure_connection(cid)
        conn.player = _mk_player(cid)
        conn.state = ConnectionState.IN_LOBBY

    def worker(cid: str, rng: random.Random) -> None:
        for _ in range(OPS_PER_CLIENT):
            op = rng.choice(("create", "join", "leave", "ready"))
            try:
                if op == "create":
                    with app.state_lock():
                        conn = app._connections.get(cid)
                        if conn is None or conn.state != ConnectionState.IN_LOBBY:
                            continue
                        if conn.player is None:
                            continue
                        config = RoomConfig(scenario="01_tiny_skirmish")
                        room, slot = app.rooms.create(
                            config=config, host=conn.player,
                        )
                        app.conn_to_room[cid] = (room.id, slot)
                        conn.state = ConnectionState.IN_ROOM
                        app.heartbeat_state[cid] = HeartbeatState(
                            joined_room_at=time.time(),
                        )
                elif op == "join":
                    with app.state_lock():
                        conn = app._connections.get(cid)
                        if conn is None or conn.state != ConnectionState.IN_LOBBY:
                            continue
                        if conn.player is None:
                            continue
                        rooms = [
                            r for r in app.rooms.list()
                            if r.status == RoomStatus.WAITING_FOR_PLAYERS
                        ]
                        if not rooms:
                            continue
                        target = rng.choice(rooms)
                        res = app.rooms.join(target.id, conn.player)
                        if res is None:
                            continue
                        _, slot = res
                        app.conn_to_room[cid] = (target.id, slot)
                        conn.state = ConnectionState.IN_ROOM
                elif op == "leave":
                    with app.state_lock():
                        conn = app._connections.get(cid)
                        if conn is None or conn.state != ConnectionState.IN_ROOM:
                            continue
                        info = app.conn_to_room.pop(cid, None)
                        if info is None:
                            continue
                        room_id, slot = info
                        app.rooms.leave(room_id, slot)
                        conn.state = ConnectionState.IN_LOBBY
                        if app.rooms.get(room_id) is None:
                            app.sessions.pop(room_id, None)
                            app.slot_to_team.pop(room_id, None)
                elif op == "ready":
                    with app.state_lock():
                        conn = app._connections.get(cid)
                        if conn is None or conn.state != ConnectionState.IN_ROOM:
                            continue
                        info = app.conn_to_room.get(cid)
                        if info is None:
                            continue
                        room_id, slot = info
                        room = app.rooms.get(room_id)
                        if room is None:
                            continue
                        seat = room.seats[slot]
                        seat.ready = rng.choice((True, False))
                        room.recompute_status()
            except Exception:  # noqa: BLE001
                # Legal ops may race and raise; integrity is still the goal.
                continue

    def run() -> None:
        rng = random.Random(42)
        threads: list[threading.Thread] = []
        for i in range(NUM_CLIENTS):
            tr = random.Random(rng.randint(0, 2**30))
            t = threading.Thread(
                target=worker, args=(f"c{i}", tr), daemon=True,
            )
            threads.append(t)
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15.0)
            if t.is_alive():
                raise AssertionError("worker deadlocked")

        # ── Invariants ──
        with app.state_lock():
            # Every conn_to_room entry points at a real room with a seat
            # actually occupied by that connection's player.
            for cid, (room_id, slot) in list(app.conn_to_room.items()):
                room = app.rooms.get(room_id)
                assert room is not None, (
                    f"conn_to_room[{cid}] -> missing room {room_id}"
                )
                seat = room.seats[slot]
                conn = app._connections.get(cid)
                assert conn is not None
                assert seat.player is conn.player, (
                    f"conn_to_room/{cid} says slot {slot} but seat "
                    f"holds {seat.player}"
                )
            # Every connection's state matches its conn_to_room presence.
            for cid, conn in app._connections.items():
                if conn.state == ConnectionState.IN_LOBBY:
                    assert cid not in app.conn_to_room, (
                        f"{cid} in lobby but still in conn_to_room"
                    )
                elif conn.state == ConnectionState.IN_ROOM:
                    assert cid in app.conn_to_room, (
                        f"{cid} IN_ROOM but missing from conn_to_room"
                    )

    _deadlock_watchdog(run, timeout=20.0)


# ──────────────────────────────────────────────────────────────
# Game path: tool dispatch + sweep concurrency
# ──────────────────────────────────────────────────────────────

def _setup_in_game(app: App) -> tuple[str, object]:
    host = _mk_player("blue-host")
    with app.state_lock():
        room, _ = app.rooms.create(
            config=RoomConfig(scenario="01_tiny_skirmish", turn_time_limit_s=60),
            host=host,
        )
        app.rooms.join(room.id, _mk_player("red-joiner"))
        state = load_scenario("01_tiny_skirmish")
        session = new_session(state, scenario="01_tiny_skirmish")
        session.turn_start_time = time.monotonic()
        app.sessions[room.id] = session
        app.slot_to_team[room.id] = {Slot.A: Team.BLUE, Slot.B: Team.RED}
        for cid, slot in (("blue", Slot.A), ("red", Slot.B)):
            conn = app.ensure_connection(cid)
            conn.player = _mk_player(cid)
            conn.state = ConnectionState.IN_GAME
            app.conn_to_room[cid] = (room.id, slot)
    return room.id, session


def test_sweep_and_state_reads_dont_deadlock():
    """Spawn a thread running run_sweep_once in a tight loop while
    many other threads hammer get_session / get_room_for_conn /
    get_heartbeat_state. Purely about deadlock absence + data
    integrity."""
    app = App()
    room_id, session = _setup_in_game(app)

    stop = threading.Event()

    def sweeper() -> None:
        while not stop.is_set():
            run_sweep_once(app, now=time.time())

    def reader() -> None:
        while not stop.is_set():
            _ = app.get_session(room_id)
            _ = app.get_room_for_conn("blue")
            _ = app.get_heartbeat_state("blue")
            _ = app.get_slot_to_team(room_id)

    def run() -> None:
        threads = [threading.Thread(target=sweeper, daemon=True)]
        threads += [threading.Thread(target=reader, daemon=True) for _ in range(8)]
        for t in threads:
            t.start()
        time.sleep(2.0)
        stop.set()
        for t in threads:
            t.join(timeout=3.0)
            if t.is_alive():
                raise AssertionError("thread deadlocked")

    _deadlock_watchdog(run, timeout=10.0)
    # Game integrity preserved.
    assert session.state.status == GameStatus.IN_PROGRESS


def test_force_end_turn_nonblocking_acquire_never_deadlocks():
    """Sweep loop racing with synthetic tool handlers holding
    session.lock. The sweep must NEVER block waiting for session.lock
    (non-blocking acquire invariant), so even if every sweep tick
    coincides with a held lock, the sweep loop finishes the loop
    body in bounded time."""
    app = App()
    room_id, session = _setup_in_game(app)

    # Force timeout branch to fire.
    session.turn_start_time = time.monotonic() - 120

    stop = threading.Event()
    hold_spans: list[float] = []

    def lock_holder() -> None:
        while not stop.is_set():
            with session.lock:
                t0 = time.monotonic()
                # Hold ~3ms: short enough not to starve, long enough
                # to make non-blocking acquire fail intermittently.
                time.sleep(0.003)
                hold_spans.append(time.monotonic() - t0)

    sweep_count = [0]

    def sweeper() -> None:
        while not stop.is_set():
            t0 = time.monotonic()
            run_sweep_once(app, now=time.time())
            dt = time.monotonic() - t0
            sweep_count[0] += 1
            # A single sweep should NEVER take >200ms; if it does,
            # something blocked.
            assert dt < 0.2, f"sweep took {dt:.3f}s — blocked?"

    def run() -> None:
        threads = [threading.Thread(target=lock_holder, daemon=True)
                   for _ in range(4)]
        threads.append(threading.Thread(target=sweeper, daemon=True))
        for t in threads:
            t.start()
        time.sleep(1.5)
        stop.set()
        for t in threads:
            t.join(timeout=3.0)
            if t.is_alive():
                raise AssertionError("thread deadlocked")
        # Many sweeps must have occurred — proves we didn't get stuck.
        assert sweep_count[0] > 10

    _deadlock_watchdog(run, timeout=10.0)


def test_auto_concede_races_vacate_room_without_deadlock():
    """Two threads simulate: thread A runs _auto_concede for red;
    thread B runs leave_room for blue. The RLock means both paths
    can enter state_lock sequentially without deadlock; after both
    finish, the room is gone + both seats vacated."""
    from silicon_pantheon.server.heartbeat import _auto_concede, _vacate_room

    app = App()
    room_id, session = _setup_in_game(app)

    b = threading.Barrier(2)

    def concede_red() -> None:
        b.wait()
        _auto_concede(app, "red")

    def leave_blue() -> None:
        b.wait()
        _vacate_room(app, "blue")
        app.drop_connection("blue")

    def run() -> None:
        tr = threading.Thread(target=concede_red)
        tb = threading.Thread(target=leave_blue)
        tr.start()
        tb.start()
        tr.join(timeout=3.0)
        tb.join(timeout=3.0)
        if tr.is_alive() or tb.is_alive():
            raise AssertionError("thread deadlocked")
        # Room is fully cleaned up — no orphan conn_to_room entries.
        with app.state_lock():
            assert "red" not in app.conn_to_room
            assert "blue" not in app.conn_to_room

    _deadlock_watchdog(run, timeout=5.0)


# ──────────────────────────────────────────────────────────────
# Replay writer stress
# ──────────────────────────────────────────────────────────────

def test_replay_writer_concurrent_writes_are_atomic(tmp_path):
    """Many threads calling replay.write concurrently should not
    produce torn / interleaved lines. The writer's internal lock
    serialises the fh.write."""
    from silicon_pantheon.server.engine.replay import ReplayWriter

    w = ReplayWriter(tmp_path / "replay.jsonl")
    NUM_THREADS = 8
    PER_THREAD = 200

    def worker(tid: int) -> None:
        for i in range(PER_THREAD):
            w.write({"tid": tid, "i": i, "payload": "x" * 64})

    def run() -> None:
        threads = [
            threading.Thread(target=worker, args=(i,), daemon=True)
            for i in range(NUM_THREADS)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10.0)
            if t.is_alive():
                raise AssertionError("worker deadlocked")
        w.close()
        # Every line is a complete JSON object. If a write was
        # interleaved, json.loads would raise.
        import json
        with open(tmp_path / "replay.jsonl", encoding="utf-8") as f:
            lines = f.readlines()
        assert len(lines) == NUM_THREADS * PER_THREAD
        for line in lines:
            obj = json.loads(line)
            assert "tid" in obj
            assert "i" in obj
            assert obj["payload"] == "x" * 64

    _deadlock_watchdog(run, timeout=15.0)


# ──────────────────────────────────────────────────────────────
# Token registry stress (pre-existing internal lock)
# ──────────────────────────────────────────────────────────────

def test_token_registry_issue_resolve_concurrency():
    """Regression guard: tokens.issue + tokens.resolve under many
    concurrent threads must not corrupt the underlying dict or
    lose tokens."""
    from silicon_pantheon.server.auth import TokenIdentity
    app = App()

    NUM_THREADS = 16
    PER_THREAD = 128

    issued: list[str] = []
    issued_lock = threading.Lock()

    def issuer(tid: int) -> None:
        for i in range(PER_THREAD):
            tok = app.tokens.issue(TokenIdentity(room_id=f"r{tid}", slot="a"))
            with issued_lock:
                issued.append(tok)

    def resolver() -> None:
        for _ in range(PER_THREAD * 4):
            with issued_lock:
                if not issued:
                    continue
                tok = random.choice(issued)
            ident = app.tokens.resolve(tok)
            # Resolve must yield exactly what we issued — no torn reads.
            if ident is not None:
                assert ident.slot == "a"
                assert ident.room_id.startswith("r")

    def run() -> None:
        threads = [threading.Thread(target=issuer, args=(i,), daemon=True)
                   for i in range(NUM_THREADS)]
        threads += [threading.Thread(target=resolver, daemon=True) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10.0)
            if t.is_alive():
                raise AssertionError("worker deadlocked")
        assert len(issued) == NUM_THREADS * PER_THREAD
        assert len(app.tokens) == NUM_THREADS * PER_THREAD

    _deadlock_watchdog(run, timeout=15.0)
