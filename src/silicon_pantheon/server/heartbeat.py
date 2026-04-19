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
    """Single sweep pass. Called once per second by the loop."""
    now = now if now is not None else time.time()
    conn_ids = list(app._connections.keys())  # noqa: SLF001
    for cid in conn_ids:
        conn = app.get_connection(cid)
        if conn is None:
            continue
        idle = _since_heartbeat(conn, now)

        # ---- Rule 1: no heartbeat = dead ----
        if idle >= HEARTBEAT_DEAD_S:
            log.info(
                "heartbeat_dead: cid=%s state=%s idle=%.1fs — evicting",
                cid, conn.state.value, idle,
            )
            if conn.state == ConnectionState.IN_GAME:
                _auto_concede(app, cid)
            elif conn.state == ConnectionState.IN_ROOM:
                _vacate_room(app, cid)
                app.drop_connection(cid)
            else:
                app.drop_connection(cid)
            app.heartbeat_state.pop(cid, None)
            continue

        # ---- Rule 2: unready timeout ----
        if conn.state == ConnectionState.IN_ROOM:
            info = app.conn_to_room.get(cid)
            if info is not None:
                room_id, slot = info
                room = app.rooms.get(room_id)
                if room is not None:
                    seat = room.seats.get(slot)
                    if seat is not None and not seat.ready:
                        hb = app.heartbeat_state.get(cid)
                        if hb and hb.joined_room_at > 0:
                            waited = now - hb.joined_room_at
                            if waited >= UNREADY_TIMEOUT_S:
                                log.info(
                                    "unready_timeout: cid=%s room=%s "
                                    "waited=%.0fs — evicting",
                                    cid, room_id, waited,
                                )
                                _vacate_room(app, cid)
                                # Don't drop connection — send them
                                # back to lobby state.
                                conn.state = ConnectionState.IN_LOBBY
                                app.heartbeat_state.pop(cid, None)

    # ---- Rule 3: per-turn time limit ----
    # Iterate sessions (one per in-game room) rather than connections.
    # Each active turn that's been running past its limit gets a
    # server-driven end_turn. This is the authoritative timeout that
    # catches: hung upstream APIs, infinite-reasoning loops, and
    # half-dead clients whose heartbeat still flows but whose agent
    # has stopped. Client-side timeouts in the bot worker are
    # belt-and-suspenders; this is the contract.
    from silicon_pantheon.server.engine.state import GameStatus  # local: avoid cycles
    mono_now = time.monotonic()
    for room_id, session in list(app.sessions.items()):
        if session.state.status != GameStatus.IN_PROGRESS:
            continue
        if session.turn_start_time <= 0:
            continue  # turn hasn't started yet (just promoted to IN_GAME)
        room = app.rooms.get(room_id)
        if room is None:
            continue
        limit = float(room.config.turn_time_limit_s or 1800)
        elapsed = mono_now - session.turn_start_time
        if elapsed > limit:
            log.info(
                "turn_timeout: room=%s team=%s elapsed=%.0fs limit=%.0fs — "
                "forcing end_turn",
                room_id, session.state.active_player.value,
                elapsed, limit,
            )
            # Pass limit so _force_end_turn can re-check inside the
            # session lock — a concurrent client end_turn may have
            # already advanced the turn between the outer check and
            # lock acquisition.
            _force_end_turn(
                app, room_id, session,
                reason="turn_time_limit_exceeded",
                limit_s=limit,
            )


def _vacate_room(app: App, cid: str) -> None:
    """Remove a connection from its room seat."""
    info = app.conn_to_room.pop(cid, None)
    if info is None:
        return
    room_id, slot = info
    from silicon_pantheon.server.lobby_tools import _cancel_countdown
    _cancel_countdown(app, room_id)
    app.rooms.leave(room_id, slot)


def _auto_concede(app: App, cid: str) -> None:
    """Concede the game for a dead connection, free its seat, drop it.

    Order matters: first flip the game to GAME_OVER and run the
    post-game-over hook (which among other things marks the room
    FINISHED), THEN vacate the seat. The vacate is necessary so the
    room can actually GC when the opponent eventually leaves —
    otherwise a crashed client leaves its seat occupied forever and
    the room lingers in the lobby list even though nobody's playing.
    """
    info = app.conn_to_room.get(cid)
    if info is None:
        app.drop_connection(cid)
        return
    room_id, slot = info
    session = app.sessions.get(room_id)
    if session is not None:
        team_map = app.slot_to_team.get(room_id, {})
        my_team = team_map.get(slot)
        opponent = my_team.other() if my_team else None
        from silicon_pantheon.server.engine.state import GameStatus

        if session.state.status != GameStatus.GAME_OVER:
            session.state.status = GameStatus.GAME_OVER
            session.state.winner = opponent
            session.log(
                "disconnect_forfeit",
                {"by": my_team.value if my_team else None,
                 "winner": opponent.value if opponent else None},
            )
            from silicon_pantheon.server.game_tools import _note_game_over_if_needed
            _note_game_over_if_needed(app, room_id)
    # Vacate the seat unconditionally. If the game was already over
    # when we got here (re-entry via a stale sweep), we still want
    # the crashed client's seat freed so the room can GC.
    _vacate_room(app, cid)
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
    Tool handlers (game_tools._dispatch at line 284) serialize on
    `session.lock` — a threading.Lock, because FastMCP runs sync
    tool handlers in a threadpool. The sweep runs on the asyncio
    event loop thread, distinct from the threadpool workers that
    execute tools. Without taking the lock, a concurrent client
    `end_turn` and this force path can both call `apply(EndTurnAction)`
    back-to-back, double-flipping `active_player` and corrupting
    the turn counter. We use non-blocking acquire: if the lock is
    held by a tool handler, we skip and retry on the next sweep
    tick (~1s later). This avoids blocking the event loop on a
    slow tool handler, and the timeout has 1800s of margin by
    default — missing one tick is harmless.
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
