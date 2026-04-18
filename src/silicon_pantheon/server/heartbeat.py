"""Server-side heartbeat sweeper.

Simple liveness model:

  1. **Heartbeat = alive.** As long as the client sends heartbeats
     (every ~10s), the server treats it as alive regardless of state.
     A human on PostMatchScreen for an hour? Fine. AFK during their
     turn? The turn timer handles gameplay; the connection stays.

  2. **No heartbeat = dead.** If heartbeats stop for HEARTBEAT_DEAD_S
     (45s = ~4 missed beats), the client is presumed crashed / network
     down. The server evicts: vacates room seat, concedes game.

  3. **Unready timeout.** If a player sits in a room without readying
     for UNREADY_TIMEOUT_S (600s = 10 min), they're evicted back to
     the lobby. Prevents a stale joiner from blocking the host.

No soft-disconnect tiers, no game-activity tracking, no multi-stage
state machine. One timer, two rules.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from silicon_pantheon.server.app import App
from silicon_pantheon.shared.protocol import ConnectionState

log = logging.getLogger(__name__)

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
    """Concede the game for a dead connection and drop it."""
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
    app.drop_connection(cid)


async def run_sweep_loop(app: App) -> None:
    """Long-lived asyncio task — sweep once per SWEEP_INTERVAL_S."""
    try:
        while True:
            run_sweep_once(app)
            await asyncio.sleep(SWEEP_INTERVAL_S)
    except asyncio.CancelledError:
        return
