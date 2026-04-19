"""Server-side heartbeat sweeper.

Simple liveness model:

  1. **Heartbeat = alive.** As long as the client sends heartbeats
     (every ~10s), the server treats it as alive regardless of state.
     A human on PostMatchScreen for an hour? Fine. AFK during their
     turn? The turn timer handles that separately; the connection
     stays.

  2. **No heartbeat = dead.** If heartbeats stop for HEARTBEAT_DEAD_S
     (45s = ~4 missed beats), the client is presumed crashed / network
     down. The server evicts: vacates room seat, concedes game.

  3. **Unready timeout.** If a player sits in a room without readying
     for UNREADY_TIMEOUT_S (600s = 10 min), they're evicted back to
     the lobby. Prevents a stale joiner from blocking the host.

  4. **Per-turn time limit.** If the active player hasn't called
     end_turn within `room.config.turn_time_limit_s` of their turn
     start, the server force-ends their turn. The turn passes to
     the opponent; any partial moves already made stick; pending
     units are marked DONE and skipped. Game does NOT concede —
     just the turn forfeits. Handles: hung models, upstream API
     stalls, infinite reasoning loops, disconnected-but-still-
     heartbeating clients. See `_force_end_turn` for the
     bypass-pending-actions path.

No soft-disconnect tiers, no game-activity tracking, no multi-stage
state machine. Three timers, four rules.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from silicon_pantheon.server.app import App
from silicon_pantheon.shared.protocol import ConnectionState

log = logging.getLogger("silicon.heartbeat")

# A client that misses ~4 heartbeats (10s interval) is dead.
HEARTBEAT_DEAD_S = 45.0
# A player in a room who hasn't readied up in 10 minutes gets evicted.
UNREADY_TIMEOUT_S = 600.0

SWEEP_INTERVAL_S = 1.0


@dataclass
class HeartbeatState:
    """Per-connection bookkeeping."""
    joined_room_at: float = 0.0


def _since_heartbeat(conn, now: float) -> float:  # noqa: ANN001
    return now - conn.last_heartbeat_at


def run_sweep_once(app: App, now: float | None = None) -> None:
    """Single sweep pass. Called once per second by the loop.

    ── Locking strategy ──
    "Snapshot then process": grab lightweight snapshots of
    connections + sessions under state_lock, release the lock,
    then handle each flagged case with its own scoped acquisition.

    Why: holding state_lock for the full sweep body would block
    every tool handler for the duration. The sweep iterates O(N
    connections + M rooms) and does I/O (logging, possibly
    leaderboard writes via _auto_concede). Instead we do the
    iteration under a short lock, then process each victim with
    the shortest possible lock scope.

    ``last_heartbeat_at`` is read without the lock (single-float
    store, GIL-atomic, deliberately unlocked for heartbeat tool
    contention). All other reads (``conn.state``, ``conn_to_room``,
    ``sessions``) happen under state_lock.
    """
    now = now if now is not None else time.time()

    # ── Phase 1: snapshot ──
    # Cheap to hold state_lock briefly: we copy scalars only.
    # Per-field scalar reads are safe under the lock even though
    # last_heartbeat_at is documented as lock-free — reading it
    # here is fine whether locked or not.
    conn_snaps: list[tuple[str, ConnectionState, float]] = []
    session_snaps: list[tuple[str, object]] = []
    with app.state_lock():
        for cid, conn in app._connections.items():  # noqa: SLF001
            conn_snaps.append((cid, conn.state, conn.last_heartbeat_at))
        for room_id, session in app.sessions.items():
            session_snaps.append((room_id, session))

    # ── Phase 2: per-connection liveness (Rule 1 + Rule 2) ──
    for cid, state, last_hb in conn_snaps:
        idle = now - last_hb

        # ---- Rule 1: no heartbeat = dead ----
        if idle >= HEARTBEAT_DEAD_S:
            # Re-read the connection under state_lock to confirm
            # it still exists in the same state — a concurrent
            # leave_room / drop_connection may have raced ahead.
            with app.state_lock():
                conn = app._connections.get(cid)  # noqa: SLF001
                if conn is None:
                    continue
                current_state = conn.state
            log.info(
                "heartbeat_dead: cid=%s state=%s idle=%.1fs — evicting",
                cid, current_state.value, idle,
            )
            if current_state == ConnectionState.IN_GAME:
                _auto_concede(app, cid)
            elif current_state == ConnectionState.IN_ROOM:
                _vacate_room(app, cid)
                app.drop_connection(cid)
            else:
                app.drop_connection(cid)
            app.pop_heartbeat_state(cid)
            continue

        # ---- Rule 2: unready timeout ----
        if state == ConnectionState.IN_ROOM:
            # Re-check everything under state_lock atomically. The
            # ready-state / joined_at / room existence can all race
            # with set_ready / leave_room / kick_player.
            did_evict = False
            with app.state_lock():
                conn = app._connections.get(cid)  # noqa: SLF001
                if conn is None or conn.state != ConnectionState.IN_ROOM:
                    continue
                info = app.conn_to_room.get(cid)
                if info is None:
                    continue
                room_id, slot = info
                room = app.rooms.get(room_id)
                if room is None:
                    continue
                seat = room.seats.get(slot)
                if seat is None or seat.ready:
                    continue
                hb = app.heartbeat_state.get(cid)
                if not hb or hb.joined_room_at <= 0:
                    continue
                waited = now - hb.joined_room_at
                if waited < UNREADY_TIMEOUT_S:
                    continue
                log.info(
                    "unready_timeout: cid=%s room=%s waited=%.0fs — evicting",
                    cid, room_id, waited,
                )
                # Inline the vacate under our existing lock (RLock
                # lets _vacate_room re-enter if it needed to). But
                # we've already got everything — just do the work.
                did_evict = True
                # Inline countdown cancel.
                task = app.autostart_tasks.pop(room_id, None)
                app.autostart_deadlines.pop(room_id, None)
                app.conn_to_room.pop(cid, None)
                app.rooms.leave(room_id, slot)
                # Don't drop connection — send them back to lobby state.
                conn.state = ConnectionState.IN_LOBBY
                app.heartbeat_state.pop(cid, None)
            if did_evict:
                # Cancel task.cancel() outside the lock — clean but
                # atomically safe either way since .cancel() just
                # flips a flag on the Task.
                if task is not None and not task.done():
                    task.cancel()

    # ── Phase 3: per-turn time limit (Rule 3) ──
    # Iterate the snapshot we took in Phase 1. For each candidate
    # we re-check the preconditions under state_lock + session.lock
    # (via _force_end_turn) so a concurrent end_turn doesn't slip
    # past us.
    from silicon_pantheon.server.engine.state import GameStatus  # local: avoid cycles
    mono_now = time.monotonic()
    for room_id, session in session_snaps:
        # Cheap pre-filters without locks (scalar reads, GIL-atomic,
        # possibly stale — _force_end_turn re-checks under lock).
        if session.state.status != GameStatus.IN_PROGRESS:  # type: ignore[attr-defined]
            continue
        if session.turn_start_time <= 0:  # type: ignore[attr-defined]
            continue  # turn hasn't started yet (just promoted to IN_GAME)
        # Grab limit under state_lock (room.config read) so a
        # concurrent update_room_config can't race us.
        with app.state_lock():
            room = app.rooms.get(room_id)
            if room is None:
                continue
            limit = float(room.config.turn_time_limit_s or 1800)
        elapsed = mono_now - session.turn_start_time  # type: ignore[attr-defined]
        if elapsed > limit:
            log.info(
                "turn_timeout: room=%s team=%s elapsed=%.0fs limit=%.0fs — "
                "forcing end_turn",
                room_id, session.state.active_player.value,  # type: ignore[attr-defined]
                elapsed, limit,
            )
            _force_end_turn(
                app, room_id, session,
                reason="turn_time_limit_exceeded",
                limit_s=limit,
            )


def _vacate_room(app: App, cid: str) -> None:
    """Remove a connection from its room seat.

    Atomic under ``app.state_lock()``. Cancels the countdown, pops
    the conn→room mapping, removes the seat, and (via
    ``rooms.leave``) deletes the room if it's empty + pre-game /
    FINISHED. ``task.cancel()`` is deferred outside the lock.
    """
    cancelled_task = None
    with app.state_lock():
        info = app.conn_to_room.pop(cid, None)
        if info is None:
            return
        room_id, slot = info
        # Inline countdown cancellation (no nested _cancel_countdown
        # call so we control the lock scope precisely).
        cancelled_task = app.autostart_tasks.pop(room_id, None)
        app.autostart_deadlines.pop(room_id, None)
        app.rooms.leave(room_id, slot)
    if cancelled_task is not None and not cancelled_task.done():
        cancelled_task.cancel()


def _auto_concede(app: App, cid: str) -> None:
    """Concede the game for a dead connection, free its seat, drop it.

    ── Locking ──
    Four phases:

    1. Read conn→room + session + team mapping under state_lock;
       release.
    2. Flip game status under session.lock (snapshot wining team
       under state_lock first so we don't need state_lock while
       holding session.lock — honouring state_lock > session.lock
       order).
    3. _note_game_over_if_needed (its own 3-phase protocol) to
       flip the room FINISHED + write leaderboard.
    4. _vacate_room (takes state_lock) to free the seat so the
       room can GC when the opponent leaves.
    5. drop_connection (takes state_lock).

    The earlier bug (crashed player's seat remaining occupied
    forever) is fixed by step 4 — the vacate is unconditional,
    even if the game was already GAME_OVER on re-entry.
    """
    from silicon_pantheon.server.engine.state import GameStatus

    # Phase 1: resolve team mapping under state_lock.
    with app.state_lock():
        info = app.conn_to_room.get(cid)
        if info is None:
            # No room to concede from; just drop the conn.
            app._connections.pop(cid, None)  # noqa: SLF001
            return
        room_id, slot = info
        session = app.sessions.get(room_id)
        team_map = dict(app.slot_to_team.get(room_id, {}))

    my_team = team_map.get(slot)
    opponent = my_team.other() if my_team else None

    # Phase 2: flip game status under session.lock (release it
    # before we re-enter state_lock for the vacate).
    if session is not None:
        with session.lock:
            if session.state.status != GameStatus.GAME_OVER:
                session.state.status = GameStatus.GAME_OVER
                session.state.winner = opponent
                session.log(
                    "disconnect_forfeit",
                    {
                        "by": my_team.value if my_team else None,
                        "winner": opponent.value if opponent else None,
                    },
                )
        # Phase 3: _note_game_over_if_needed has its own protocol.
        from silicon_pantheon.server.game_tools import _note_game_over_if_needed
        _note_game_over_if_needed(app, room_id)

    # Phase 4: vacate seat unconditionally (even if game was
    # already GAME_OVER from a prior sweep re-entry) so the room
    # can GC.
    _vacate_room(app, cid)
    # Phase 5: drop the connection.
    app.drop_connection(cid)


def _force_end_turn(
    app: App, room_id: str, session, reason: str = "turn_timeout",
    limit_s: float | None = None,
) -> None:
    """Force the active player's turn to end, bypassing the usual
    'pending unit actions' guard.

    Used by the per-turn-limit sweep rule. The engine's normal
    end_turn handler (in tools/mutations.py) rejects if any of the
    active player's units are in status MOVED (moved but hasn't
    finalized the turn's attack/heal/wait). That's the right guard
    for a cooperating client, but the point of this path is the
    client HAS stopped cooperating — we need to finalize the turn
    regardless. Pending MOVED units are force-marked DONE so the
    engine's end-of-turn hooks can run cleanly.

    Partial progress sticks — any moves/attacks already recorded
    stay. Only the PENDING-but-not-yet-resolved actions are dropped.

    Game-over semantics: this does NOT concede the game. It only
    ends the turn. If the turn-end itself triggers a win condition
    (e.g. max_turns_draw, reach_tile from the other side on their
    previous turn), that's picked up by the engine's normal check
    in apply(EndTurnAction) and _note_game_over_if_needed fires.

    ── Concurrency ──
    Acquires ``session.lock`` to serialise with cooperative tool
    handlers in ``game_tools._dispatch``. Concurrent client
    ``end_turn`` + force-end would otherwise both call
    ``apply(EndTurnAction)``, double-flipping ``active_player`` and
    corrupting the turn counter.

    We use **non-blocking acquire** so the sweep can't freeze if a
    tool handler is holding the session lock (in the current single-
    threaded asyncio model that can't happen anyway; in a future
    threadpool / true-MT world it would be worth avoiding anyway).
    If the lock is busy, we skip and retry on the next sweep tick
    (~1s later). The default turn limit is 1800s — missing 1800
    sweep ticks would require a session lock held solid for 30
    minutes, which is a bug in its own right.

    ``_note_game_over_if_needed`` is called AFTER ``session.lock``
    is released — it has its own 3-phase locking protocol and
    would violate our order if invoked inside session.lock
    (state_lock > session.lock, never reversed).
    """
    import time as _time
    from silicon_pantheon.server.engine.state import (
        GameStatus,
        UnitStatus,
    )
    from silicon_pantheon.server.engine.rules import EndTurnAction, apply

    # Non-blocking lock acquire — see "Concurrency" note above.
    if not session.lock.acquire(blocking=False):
        log.debug(
            "force_end_turn: lock busy for room=%s, retry next sweep",
            room_id,
        )
        return
    try:
        if session.state.status == GameStatus.GAME_OVER:
            return  # already over — nothing to force

        # Re-check elapsed inside the lock. A concurrent client
        # `end_turn` may have landed between the outer sweep's
        # `elapsed > limit` check and our lock acquisition; in that
        # case session.turn_start_time was just reset and we must
        # NOT force-end a turn the client already ended cleanly.
        if limit_s is not None and session.turn_start_time > 0:
            elapsed = _time.monotonic() - session.turn_start_time
            if elapsed <= limit_s:
                log.info(
                    "force_end_turn: race resolved — client ended turn "
                    "between sweep check and lock acquire (room=%s elapsed=%.1fs)",
                    room_id, elapsed,
                )
                return

        active = session.state.active_player

        # Step 1: force-complete any MOVED (pending) units so apply()
        # doesn't reject the EndTurnAction. Completed and READY units
        # stay as they are — the engine's end-of-turn hook resets the
        # incoming player's units to READY anyway.
        for u in session.state.units_of(active):
            if u.status is UnitStatus.MOVED:
                u.status = UnitStatus.DONE

        # Step 2: record the truncated turn duration for telemetry so
        # /leaderboard stats show this turn's actual elapsed time, not
        # zero.
        if session.turn_start_time > 0:
            dt = _time.monotonic() - session.turn_start_time
            session.turn_times_by_team.setdefault(active, []).append(dt)

        # Step 3: apply the EndTurnAction. The engine runs end-of-turn
        # effects (terrain heal/damage, win conditions, turn counter
        # advance, active_player flip).
        try:
            result = apply(session.state, EndTurnAction())
        except Exception:
            log.exception(
                "force_end_turn: apply() raised for room=%s team=%s",
                room_id, active.value,
            )
            return

        # Step 4: replicate the bookkeeping that mutations._record_action
        # does on a cooperative end_turn — history append, narrative
        # drain, coach queue clear, action hooks fired, new turn timer
        # reset. We do NOT call _record_action directly because
        # tools/mutations.py imports us transitively (circular).
        session.state.last_action = result
        session.state.history.append(result)
        # Drain narrative events emitted by apply() (terrain deaths,
        # on_turn_start hooks) so they land in the replay instead of
        # accumulating silently on the state until the next cooperative
        # action eventually drains them.
        nlog = getattr(session.state, "_narrative_log", None)
        if nlog:
            for entry in nlog:
                session.log("narrative_event", entry)
            nlog.clear()
        session.coach_queues[active] = []
        session.turn_start_time = _time.monotonic()
        session.log("turn_timeout_forfeit", {"team": active.value, "reason": reason})
        try:
            session.notify_action(result)
        except Exception:
            log.exception("force_end_turn: notify_action failed")

        # Step 5: if the turn-end triggered a win condition (max_turns_draw,
        # a reach_tile that was satisfied the previous turn, etc), the
        # engine will have set session.state.status = GAME_OVER. Wire
        # that through to the post-game-over hook so leaderboard /
        # replay / room cleanup runs.
        game_over = session.state.status == GameStatus.GAME_OVER
    finally:
        session.lock.release()

    # _note_game_over_if_needed touches rooms + leaderboard which
    # have their own locking — call it outside the session lock to
    # avoid any chance of lock-order inversion.
    if game_over:
        from silicon_pantheon.server.game_tools import _note_game_over_if_needed
        _note_game_over_if_needed(app, room_id)


async def run_sweep_loop(app: App) -> None:
    """Long-lived asyncio task — sweep once per SWEEP_INTERVAL_S."""
    try:
        while True:
            run_sweep_once(app)
            await asyncio.sleep(SWEEP_INTERVAL_S)
    except asyncio.CancelledError:
        return
