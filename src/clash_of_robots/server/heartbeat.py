"""Server-side heartbeat sweeper + disconnect state machine.

Clients are expected to call the `heartbeat` tool every ~10s. This
module runs a single asyncio task per App that checks every
connection's last_heartbeat_at once a second and applies the
disconnect rules from the Phase 1 design doc:

  - 30s silent: connection → soft_disconnect
  - in_room 30s soft: seat vacated; room reverts to waiting
  - in_game 60s soft: opponent notified (log entry; no tool event yet)
  - in_game 120s soft: disconnected player auto-concedes

Design priorities:

  - The sweeper is the *only* place these timers fire, so the logic
    is auditable in one spot.
  - State transitions are idempotent; running the sweeper twice in a
    row on the same connection is a no-op.
  - Cancelling the sweeper cleanly stops the task.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from clash_of_robots.server.app import App
from clash_of_robots.shared.protocol import ConnectionState

log = logging.getLogger(__name__)

# Timer thresholds (seconds). Kept at module scope so tests can
# monkeypatch smaller values.
HEARTBEAT_GRACE_S = 30.0
IN_ROOM_EVICT_S = 30.0
IN_GAME_SOFT_NOTICE_S = 60.0
IN_GAME_HARD_CONCEDE_S = 120.0

SWEEP_INTERVAL_S = 1.0


@dataclass
class HeartbeatState:
    """Per-connection bookkeeping for the sweeper."""

    soft_disconnected_at: float | None = None
    notified_opponent: bool = False


def _since_heartbeat(conn, now: float) -> float:  # noqa: ANN001
    return now - conn.last_heartbeat_at


def run_sweep_once(app: App, now: float | None = None) -> None:
    """Single sweep pass — public entry point so tests can run it
    deterministically without waiting on the asyncio loop."""
    now = now if now is not None else time.time()
    # Collect in a local list so we don't mutate connections while
    # holding the app's connection lock.
    conn_ids = list(app._connections.keys())  # noqa: SLF001
    for cid in conn_ids:
        conn = app.get_connection(cid)
        if conn is None:
            continue
        idle = _since_heartbeat(conn, now)
        hb = app.heartbeat_state.setdefault(cid, HeartbeatState())

        # Still alive: reset soft-disconnect bookkeeping.
        if idle < HEARTBEAT_GRACE_S:
            if hb.soft_disconnected_at is not None:
                hb.soft_disconnected_at = None
                hb.notified_opponent = False
            continue

        # Entered soft-disconnect.
        if hb.soft_disconnected_at is None:
            hb.soft_disconnected_at = now
            log.info("soft_disconnect: cid=%s state=%s", cid, conn.state.value)

        soft_age = now - hb.soft_disconnected_at

        if conn.state == ConnectionState.IN_LOBBY:
            if soft_age >= HEARTBEAT_GRACE_S:
                log.info("evicting anonymous/lobby conn cid=%s", cid)
                app.drop_connection(cid)
                app.heartbeat_state.pop(cid, None)

        elif conn.state == ConnectionState.IN_ROOM:
            if soft_age >= IN_ROOM_EVICT_S:
                log.info("evicting in_room conn cid=%s", cid)
                info = app.conn_to_room.pop(cid, None)
                if info is not None:
                    room_id, slot = info
                    # Break any pending countdown, vacate the seat, keep
                    # the room alive if the other seat is still occupied.
                    from clash_of_robots.server.lobby_tools import _cancel_countdown

                    _cancel_countdown(app, room_id)
                    app.rooms.leave(room_id, slot)
                app.drop_connection(cid)
                app.heartbeat_state.pop(cid, None)

        elif conn.state == ConnectionState.IN_GAME:
            if soft_age >= IN_GAME_HARD_CONCEDE_S:
                log.info("hard_disconnect: auto-concede cid=%s", cid)
                _auto_concede(app, cid)
                app.heartbeat_state.pop(cid, None)
            elif soft_age >= IN_GAME_SOFT_NOTICE_S and not hb.notified_opponent:
                hb.notified_opponent = True
                _notify_opponent_of_disconnect(app, cid)


def _notify_opponent_of_disconnect(app: App, cid: str) -> None:
    """Log an opponent-disconnect event in the session's replay."""
    info = app.conn_to_room.get(cid)
    if info is None:
        return
    room_id, _slot = info
    session = app.sessions.get(room_id)
    if session is None:
        return
    session.log("disconnect_notice", {"connection_id": cid})


def _auto_concede(app: App, cid: str) -> None:
    """Mark the disconnecting player as losing the match."""
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
        from clash_of_robots.server.engine.state import GameStatus

        session.state.status = GameStatus.GAME_OVER
        session.state.winner = opponent
        session.log(
            "disconnect_forfeit",
            {"by": my_team.value if my_team else None,
             "winner": opponent.value if opponent else None},
        )
    app.drop_connection(cid)


async def run_sweep_loop(app: App) -> None:
    """Long-lived asyncio task — sweep once per SWEEP_INTERVAL_S."""
    try:
        while True:
            run_sweep_once(app)
            await asyncio.sleep(SWEEP_INTERVAL_S)
    except asyncio.CancelledError:
        return
