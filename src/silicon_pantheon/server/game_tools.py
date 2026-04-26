"""MCP-facing wrappers around the 13 in-process game tools.

Each MCP tool derives the player's viewer (Team.BLUE/RED) from the
connection's slot in its room, looks up the room's authoritative
Session, dispatches to the existing in-process tool layer, and
returns a structured result.

Phase 1a: only one hardcoded "dev room" exists, created by the
`create_dev_game` tool. Phase 1b replaces that with proper lobby /
create_room / join_room flow.

The heavy lifting stays in `server/tools/__init__.py` — these are
thin dispatch wrappers so fog-of-war filtering (1c) can slot into a
single transform that every game tool output passes through.
"""

from __future__ import annotations

import logging
import random
from typing import Any

from mcp.server.fastmcp import FastMCP

log = logging.getLogger("silicon.game")

from silicon_pantheon.server.app import App, Connection, _error, _ok
from silicon_pantheon.shared.sanitize import sanitize_freetext
from silicon_pantheon.server.engine.scenarios import load_scenario
from silicon_pantheon.server.engine.state import Team
from silicon_pantheon.server.rooms import RoomConfig, RoomStatus, Slot
from silicon_pantheon.server.session import Session, new_session
from silicon_pantheon.server.tools import ToolError, call_tool
from silicon_pantheon.shared.protocol import ConnectionState, ErrorCode
from silicon_pantheon.shared.viewer_filter import (
    ViewerContext,
    filter_history,
    filter_legal_actions,
    filter_state,
    filter_threat_map,
    filter_unit,
    update_ever_seen,
)


# Tools whose dict result is the full state snapshot or a per-unit view;
# these must be passed through the viewer filter before returning.
_FILTERED_STATE_TOOLS = frozenset({"get_state"})
_FILTERED_UNIT_TOOLS = frozenset({"get_unit"})
_FILTERED_THREAT_TOOLS = frozenset({"get_threat_map"})
_FILTERED_HISTORY_TOOLS = frozenset({"get_history"})
_FILTERED_LEGAL_ACTIONS_TOOLS = frozenset({"get_legal_actions"})


# ── Stuck-dispatch auto-dump rate limiter ──
# When the 10s watchdog fires for one stuck dispatch, we dump all
# thread stack traces via faulthandler. If N dispatches are stuck
# simultaneously (which is the common case — state-lock starvation,
# deadlock, etc., blocks many calls at once), we'd otherwise dump
# N times. Rate-limit to once per 30s so the log gets ONE complete
# stack snapshot per hang event, not a flood.
import threading as _threading_for_rl  # noqa: E402
_last_stack_dump_at: float = 0.0
_stack_dump_lock = _threading_for_rl.Lock()
_STACK_DUMP_COOLDOWN_S = 30.0


def _append_agent_report_jsonl(event: dict) -> None:
    """Append an ``agent_report`` event to today's jsonl file.

    The file lives at ``~/.silicon-pantheon/debug-reports/YYYYMMDD.jsonl``.
    One jsonl entry per call — easy to grep/cat/jq after a session.
    Creates the directory lazily on first write. All exceptions bubble
    out; the tool caller decides whether to swallow (production) or
    re-raise (debug) via ``reraise_in_debug``.
    """
    import datetime as _dt
    import json as _json
    from pathlib import Path as _Path

    day = _dt.datetime.fromtimestamp(event["timestamp"]).strftime("%Y%m%d")
    out_dir = _Path.home() / ".silicon-pantheon" / "debug-reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{day}.jsonl"
    with out_path.open("a", encoding="utf-8") as f:
        f.write(_json.dumps(event, ensure_ascii=False) + "\n")


def _viewer_context(session: Session, viewer: Team) -> ViewerContext:
    return ViewerContext(
        team=viewer,
        fog_mode=session.fog_of_war,  # type: ignore[arg-type]
        ever_seen=session.ever_seen.get(viewer, frozenset()),
    )


def _apply_filter(
    tool_name: str, result: dict, session: Session, viewer: Team
) -> dict:
    """Pass state-revealing tool results through the viewer filter.

    Only tools that return state / unit / threat-map info need filtering;
    action results (move/attack/heal/wait/end_turn) are always safe to
    echo back because they describe the caller's own action.
    """
    if session.fog_of_war == "none":
        return result
    from silicon_pantheon.server.engine.state import GameStatus
    if session.state.status == GameStatus.GAME_OVER:
        return result
    ctx = _viewer_context(session, viewer)
    if tool_name in _FILTERED_STATE_TOOLS:
        return filter_state(session.state, ctx)
    if tool_name in _FILTERED_UNIT_TOOLS:
        filtered = filter_unit(result.get("id", ""), result, session.state, ctx)
        return filtered if filtered is not None else {"error": "unit not found (dead, nonexistent, or hidden by fog)"}
    if tool_name in _FILTERED_THREAT_TOOLS:
        return filter_threat_map(result, session.state, ctx)
    if tool_name in _FILTERED_HISTORY_TOOLS:
        return filter_history(result, session.state, ctx)
    if tool_name in _FILTERED_LEGAL_ACTIONS_TOOLS:
        return filter_legal_actions(result, session.state, ctx)
    return result


def _maybe_update_ever_seen(session: Session, result: dict, viewer: Team) -> None:
    """After a half-turn ends, grow this team's ever_seen for classic mode."""
    if session.fog_of_war != "classic":
        return
    if not isinstance(result, dict):
        return
    if result.get("type") == "end_turn":
        session.ever_seen[viewer] = update_ever_seen(
            session.state, viewer, session.ever_seen[viewer]
        )


def start_game_for_room(app: App, room_id: str) -> None:
    """Promote a room from COUNTING_DOWN to IN_GAME.

    Builds the engine Session from the room's scenario, pins the
    slot->team mapping (deterministic for fixed-assignment rooms,
    coin-flipped for random), and flips every connection seated in
    the room into state IN_GAME. Idempotent if the room has already
    started.

    ── Locking ──
    The whole promotion is done under ``app.state_lock()``. Scenario
    load + run_dir creation are inside the lock too — they're fast
    (sub-10ms typically) and keeping them inline preserves the
    atomicity of the whole transition. A per-room promotion is a
    one-shot operation that happens at most once per match lifetime;
    holding state_lock for ~10ms during that window is cheap.

    ``session.log_match_players`` is called **outside** state_lock —
    it writes to the replay file (which has its own lock) and only
    reads the newly-created Session, which is already fully
    initialised.
    """
    log.info("start_game_for_room: room=%s", room_id)
    from datetime import datetime
    from pathlib import Path as _Path
    import time as _time

    players: dict[str, dict] = {}
    session: Session | None = None
    with app.state_lock():
        room = app.rooms.get(room_id)
        if room is None:
            return
        # Idempotency re-check under the lock — two concurrent
        # countdown tasks or a countdown + dev-game shortcut can
        # both call us; only the first one wins.
        if room.status == RoomStatus.IN_GAME:
            return
        if not room.all_ready():
            return

        state = load_scenario(room.config.scenario)
        state.max_turns = room.config.max_turns
        runs_dir = _Path("runs-server")
        ts = datetime.now().strftime("%Y%m%dT%H%M%S")
        run_dir = runs_dir / f"{ts}_{room.config.scenario}_{room_id}"
        run_dir.mkdir(parents=True, exist_ok=True)
        replay_path = run_dir / "replay.jsonl"
        log.info("start_game_for_room: run_dir=%s", run_dir)
        session = new_session(
            state,
            replay_path=replay_path,
            scenario=room.config.scenario,
            fog_of_war=room.config.fog_of_war,
        )
        session.turn_start_time = _time.monotonic()
        app.sessions[room_id] = session
        if room.config.team_assignment == "fixed":
            host_team = Team.BLUE if room.config.host_team == "blue" else Team.RED
            other = Team.RED if host_team is Team.BLUE else Team.BLUE
            app.slot_to_team[room_id] = {Slot.A: host_team, Slot.B: other}
        else:  # "random"
            coin = random.random() < 0.5
            app.slot_to_team[room_id] = (
                {Slot.A: Team.BLUE, Slot.B: Team.RED}
                if coin
                else {Slot.A: Team.RED, Slot.B: Team.BLUE}
            )
        room.status = RoomStatus.IN_GAME
        promoted = []
        for cid, (rid, _slot) in app.conn_to_room.items():
            if rid == room_id:
                c = app._connections.get(cid)  # noqa: SLF001
                if c is not None:
                    c.state = ConnectionState.IN_GAME
                    promoted.append(cid[:8])
        log.info(
            "start_game_for_room: room=%s promoted connections=%s "
            "slot_to_team=%s",
            room_id,
            promoted,
            {s.value: t.value for s, t in app.slot_to_team[room_id].items()},
        )
        # Build the players payload under the lock (seats + slot_team
        # are state_lock-guarded). The actual replay write happens
        # OUTSIDE the lock below.
        slot_team = app.slot_to_team[room_id]
        for slot, seat in room.seats.items():
            team = slot_team.get(slot)
            if team is not None and seat.player is not None:
                players[team.value] = {
                    "display_name": seat.player.display_name,
                    "kind": seat.player.kind,
                    "provider": seat.player.provider,
                    "model": seat.player.model,
                }

    # Replay I/O outside state_lock. ReplayWriter has its own lock.
    # Safe: session is fully initialised and no other thread has yet
    # seen it mutate (we released state_lock after the writes above).
    if session is not None:
        session.log_match_players(players)


def _note_game_over_if_needed(app: App, room_id: str) -> None:
    """If the engine has flipped to GAME_OVER, mark the room FINISHED.

    Called after every game-tool dispatch and any other code path that
    might cause termination (concede, auto-concede). Idempotent.

    ── Locking ──
    Three phases:

    1. Read ``session.state.status`` under ``session.lock``. (The flip
       to GAME_OVER is always done under session.lock by whichever
       mutation caused it.)
    2. If game is over, flip ``room.status = FINISHED`` and grab
       snapshots of ``room`` + ``slot_to_team`` under ``state_lock``.
       Use a did-we-win-the-race flag to make the leaderboard write
       idempotent — only the thread that actually performs the
       FINISHED transition does the I/O.
    3. OUTSIDE all locks, do the slow I/O (log_match_end, record_match).
       log_match_end reads session.state (immutable after GAME_OVER is
       set) and writes the replay (which has its own lock); safe
       without re-acquiring session.lock.

    Strict acquisition order is honoured: session.lock is released
    before state_lock is taken (never reversed). The two critical
    sections don't nest.
    """
    from silicon_pantheon.server.engine.state import GameStatus

    session = app.get_session(room_id)
    if session is None:
        from silicon_pantheon.shared.debug import invariant
        # _note_game_over_if_needed runs after a tool call that
        # dispatched against a live session; it vanishing here means
        # either a race with room deletion or a cache-eviction bug
        # during a live match. In production we tolerate it (the
        # game will be cleaned up eventually); in debug we want to
        # see the stack at the moment of the race.
        invariant(
            session is not None,
            f"session vanished before game_over check for room={room_id}",
            logger=log,
        )
        return

    # Phase 1: read session.state.status under session.lock.
    with session.lock:
        if session.state.status != GameStatus.GAME_OVER:
            return

    # Phase 2: transition room + snapshot under state_lock.
    won_race = False
    room_snap = None
    slot_to_team_snap: dict = {}
    with app.state_lock():
        room = app.rooms.get(room_id)
        if room is None:
            return
        if room.status != RoomStatus.FINISHED:
            log.info(
                "room %s transitioning IN_GAME -> FINISHED (game_over)",
                room_id,
            )
            room.status = RoomStatus.FINISHED
            won_race = True
            room_snap = room
            slot_to_team_snap = dict(app.slot_to_team.get(room_id, {}))

    # Phase 3: slow I/O outside all app locks. Only the winner of the
    # FINISHED-transition race performs the writes.
    if won_race:
        from silicon_pantheon.shared.debug import reraise_in_debug
        try:
            session.log_match_end()
        except Exception:
            reraise_in_debug(log, f"log_match_end failed for room {room_id}")
            log.exception("log_match_end failed for room %s", room_id)
        try:
            from silicon_pantheon.server.leaderboard import record_match
            record_match(session, room_snap, slot_to_team_snap)
        except Exception:
            reraise_in_debug(
                log, f"leaderboard record_match failed for room {room_id}"
            )
            log.exception("leaderboard record_match failed for room %s", room_id)


def _viewer_for(conn: Connection, app: App) -> tuple[Any, Team] | None:
    """Resolve (session, viewer) for a connection currently in a game.

    Returns None if the connection isn't in a game or the room/session
    has gone away.

    Locking: takes ``app.state_lock()`` for the duration of the
    multi-dict read so the resolution is atomic under concurrency.
    """
    with app.state_lock():
        if conn.state != ConnectionState.IN_GAME:
            return None
        info = app.conn_to_room.get(conn.id)
        if info is None:
            return None
        room_id, slot = info
        session = app.sessions.get(room_id)
        if session is None:
            return None
        # Slot → Team mapping is pinned at game-start time on the App.
        mapping = app.slot_to_team.get(room_id)
        if mapping is None:
            return None
        return session, mapping[slot]


def _viewer_for_any_state(app: App, connection_id: str) -> tuple[Any, Team] | None:
    """Like _viewer_for but works even after the game has finished.

    Locking: takes ``app.state_lock()`` for the duration of the
    multi-dict read so the resolution is atomic under concurrency.
    """
    with app.state_lock():
        conn = app._connections.get(connection_id)  # noqa: SLF001
        if conn is None:
            return None
        info = app.conn_to_room.get(connection_id)
        if info is None:
            return None
        room_id, slot = info
        session = app.sessions.get(room_id)
        if session is None:
            return None
        mapping = app.slot_to_team.get(room_id)
        if mapping is None:
            return None
        return session, mapping[slot]


def _dispatch(app: App, connection_id: str, tool_name: str, args: dict) -> dict:
    """Shared body for every game tool wrapper.

    ── Locking ──
    Three-phase execution:

    1. **Resolve phase (state_lock):** atomically look up the
       Connection, validate its state, resolve the viewer session +
       team mapping, and bump ``conn.last_game_activity_at``. This
       phase guarantees a consistent snapshot: if a concurrent
       ``leave_room`` / sweep eviction races with us, either we saw
       the connection in a valid state and proceed, or we return an
       error — no torn reads.
    2. **Execute phase (session.lock):** run the actual tool logic
       against the game state. Hooks (session.action_hooks) fire
       INSIDE this lock to preserve their ordering w.r.t. the
       mutations they observe.
    3. **Post-process phase (no app-level lock held):** check for
       game-over transition via ``_note_game_over_if_needed``, which
       has its own locking protocol.

    Critical: no ``await`` is ever issued while either lock is held.
    The handler is sync ``def``, so it cannot await anyway; this is
    guaranteed by construction.
    """
    import threading as _threading
    import time as _time

    # Stuck-dispatch watchdog: if the body of _dispatch doesn't
    # complete within DISPATCH_STUCK_THRESHOLD_S, log a WARNING from
    # a side thread. Catches the class of bug we saw 2026-04-20
    # 05:15:00 UTC where "Processing request of type CallToolRequest"
    # kept arriving but "tool dispatch:" lines stopped firing — i.e.
    # the state_lock acquire or some later line blocked for 30+ s.
    # threading.Timer is cheap (~microseconds to create) and cancel
    # releases it immediately on normal exit, so zero overhead for
    # fast-path calls.
    DISPATCH_STUCK_THRESHOLD_S = 10.0
    _t0_monotonic = _time.monotonic()

    def _warn_stuck() -> None:
        elapsed = _time.monotonic() - _t0_monotonic
        log.warning(
            "tool handler STUCK: tool=%s cid=%s elapsed=%.1fs — "
            "dispatch has not completed (possible lock contention, "
            "deadlock, or blocked I/O).",
            tool_name, connection_id[:8], elapsed,
        )
        # Auto-dump all Python thread stacks to stderr (→ server log)
        # so the next hang is diagnosable without an operator being
        # at a keyboard in time to run `kill -USR1`. Rate-limited via
        # _stack_dump_lock + cooldown so a burst of concurrent stuck
        # dispatches produces ONE stack dump per hang event, not N.
        global _last_stack_dump_at
        with _stack_dump_lock:
            now = _time.monotonic()
            if now - _last_stack_dump_at < _STACK_DUMP_COOLDOWN_S:
                return
            _last_stack_dump_at = now
        import faulthandler
        import sys as _sys
        log.warning(
            "stuck dispatch — dumping ALL thread stacks to stderr "
            "(→ systemd journal: `journalctl -u silicon-serve -n 500`). "
            "Next dump suppressed for %.0fs.", _STACK_DUMP_COOLDOWN_S,
        )
        try:
            faulthandler.dump_traceback(file=_sys.stderr)
        except Exception:
            log.exception("faulthandler.dump_traceback raised")

    _watchdog = _threading.Timer(DISPATCH_STUCK_THRESHOLD_S, _warn_stuck)
    _watchdog.daemon = True
    _watchdog.start()

    try:
        return _dispatch_inner(app, connection_id, tool_name, args)
    finally:
        _watchdog.cancel()


def _dispatch_inner(app: App, connection_id: str, tool_name: str, args: dict) -> dict:
    """The actual dispatch body — wrapped by _dispatch's watchdog."""
    import time as _time

    # ── Phase 1: resolve under state_lock ────────────────────────
    with app.state_lock():
        conn = app._connections.get(connection_id)  # noqa: SLF001
        if conn is None:
            return _error(
                ErrorCode.NOT_REGISTERED, "call set_player_metadata first"
            )
        if conn.state != ConnectionState.IN_GAME:
            return _error(
                ErrorCode.TOOL_NOT_AVAILABLE_IN_STATE,
                f"game tools require state=in_game (current: {conn.state.value})",
            )
        # Track last meaningful tool call so the heartbeat sweeper can
        # detect "transport alive, game loop dead" — the case where a
        # client's heartbeat task keeps pinging but the TUI's tick loop
        # has crashed and the player can no longer act. Written under
        # state_lock for atomicity with the conn.state read above.
        conn.last_game_activity_at = _time.time()
        info = app.conn_to_room.get(connection_id)
        if info is None:
            return _error(
                ErrorCode.GAME_NOT_STARTED,
                "no active game for this connection",
            )
        room_id, slot = info
        session = app.sessions.get(room_id)
        mapping = app.slot_to_team.get(room_id)
        if session is None or mapping is None:
            return _error(
                ErrorCode.GAME_NOT_STARTED,
                "no active game for this connection",
            )
        viewer = mapping[slot]

    # Log every dispatch. Reads on session.state (turn + active_player)
    # here are not strictly locked — they're written under session.lock
    # by the engine, but single-field scalars are GIL-atomic and this
    # line is diagnostic only.
    log.info(
        "tool dispatch: cid=%s tool=%s viewer=%s active=%s "
        "turn=%s args=%s",
        connection_id[:8],
        tool_name,
        viewer.value,
        session.state.active_player.value,
        session.state.turn,
        str(args)[:200] if args else "{}",
    )

    # ── Phase 2: execute under session.lock ──────────────────────
    _t0_dispatch = _time.time()
    with session.lock:
        # Snapshot pre-mutation visibility so the fog audit (post-
        # mutation) can allowlist whatever the agent was legitimately
        # allowed to see when it chose this action. Must happen BEFORE
        # call_tool; e.g. if an attacker dies on counter-attack, LoS
        # to the target shrinks and a naive post-mutation recompute
        # falsely flags the target_id the agent itself passed in.
        from silicon_pantheon.server.tools._common import (
            visible_enemy_ids_snapshot,
        )
        pre_visible_enemy_ids = visible_enemy_ids_snapshot(session, viewer)
        try:
            result = call_tool(session, viewer, tool_name, args)
        except ToolError as e:
            log.info(
                "tool rejected: cid=%s tool=%s viewer=%s err=%s",
                connection_id[:8],
                tool_name,
                viewer.value,
                e,
            )
            return _error(ErrorCode.BAD_INPUT, str(e))
        # Grow ever_seen *before* filtering the response so the viewer sees
        # tiles they just observed at the boundary. Currently only end_turn
        # updates ever_seen; if we later want live memory during a turn we
        # can expand this.
        _maybe_update_ever_seen(session, result, viewer)
        # Log the authoritative unit statuses around state-revealing tools
        # so we can tell if any client is confused about unit readiness.
        if tool_name in _FILTERED_STATE_TOOLS or tool_name == "end_turn":
            log.info(
                "post-%s viewer=%s active=%s turn=%s units=%s",
                tool_name,
                viewer.value,
                session.state.active_player.value,
                session.state.turn,
                ",".join(
                    f"{u.id}={u.status.value}" for u in session.state.units.values()
                ),
            )
        filtered = _apply_filter(tool_name, result, session, viewer)
        # Diagnostic: under fog, scan the FILTERED response for hidden
        # enemy IDs. If any leak through, log WARNING pointing at the
        # exact field path — this is how we chase down "the agent knew
        # an ID it shouldn't have seen" reports. No-op under fog=none.
        from silicon_pantheon.server.tools._common import (
            audit_response_for_fog_leaks,
        )
        audit_response_for_fog_leaks(
            filtered,
            session,
            viewer,
            tool_name,
            pre_visible_enemy_ids=pre_visible_enemy_ids,
        )

    # ── Phase 3: post-process (no app-level lock held) ──────────
    # _note_game_over_if_needed has its own 3-phase locking protocol;
    # we just invoke it with the room_id we captured in phase 1.
    _note_game_over_if_needed(app, room_id)
    _dt_dispatch = _time.time() - _t0_dispatch
    if _dt_dispatch > 1.0:
        log.warning(
            "tool dispatch SLOW: cid=%s tool=%s dt=%.2fs",
            connection_id[:8], tool_name, _dt_dispatch,
        )
    return _ok({"result": filtered})


def register_game_tools(mcp: FastMCP, app: App) -> None:
    """Attach the 13 game tools + create_dev_game to an MCP server.

    Each tool has an explicit Python signature so FastMCP can generate
    a proper JSON schema for agents. The dispatch body delegates to the
    in-process tool layer via `_dispatch`.
    """

    # ---- dev-only game creation (Phase 1a) ----

    @mcp.tool()
    def create_dev_game(
        connection_id: str,
        scenario: str = "01_tiny_skirmish",
    ) -> dict:
        """Mutating. Development-only: create a dev room and seat yourself as the blue player (slot A). scenario defaults to '01_tiny_skirmish' but can be overridden. A second connection calls join_dev_game to take slot B and start immediately. Requires state=in_lobby (call set_player_metadata first). Only one dev game can exist at a time. In production, use create_room instead."""
        with app.state_lock():
            conn = app._connections.get(connection_id)  # noqa: SLF001
            if conn is None or conn.state != ConnectionState.IN_LOBBY:
                return _error(
                    ErrorCode.TOOL_NOT_AVAILABLE_IN_STATE,
                    "create_dev_game requires state=in_lobby",
                )
            if conn.player is None:
                return _error(ErrorCode.BAD_INPUT, "set_player_metadata first")
            if app.sessions:
                return _error(ErrorCode.ALREADY_IN_ROOM, "a dev game already exists")
            room, slot = app.rooms.create(
                config=RoomConfig(scenario=scenario), host=conn.player
            )
            app.conn_to_room[connection_id] = (room.id, slot)
            conn.state = ConnectionState.IN_ROOM
            room_id = room.id
            slot_value = slot.value
        return _ok({"room_id": room_id, "slot": slot_value})

    @mcp.tool()
    def join_dev_game(connection_id: str) -> dict:
        """Mutating. Development-only shortcut: join the first available room as the red player and start the match immediately, bypassing the normal ready-up flow. Requires state=in_lobby (call set_player_metadata first). Returns the room_id and assigned slot. In production, use join_room + set_ready instead."""
        # Phase 1: validate + claim seat under state_lock.
        with app.state_lock():
            conn = app._connections.get(connection_id)  # noqa: SLF001
            if conn is None or conn.state != ConnectionState.IN_LOBBY:
                return _error(
                    ErrorCode.TOOL_NOT_AVAILABLE_IN_STATE,
                    "join_dev_game requires state=in_lobby",
                )
            if conn.player is None:
                return _error(ErrorCode.BAD_INPUT, "set_player_metadata first")
            rooms = app.rooms.list()
            if not rooms:
                return _error(ErrorCode.ROOM_NOT_FOUND, "no dev game to join")
            room = rooms[0]
            result = app.rooms.join(room.id, conn.player)
            if result is None:
                return _error(ErrorCode.ROOM_FULL, "dev game is full")
            _, slot = result
            app.conn_to_room[connection_id] = (room.id, slot)
            scenario_name = room.config.scenario
            room_id = room.id

        # Phase 2: scenario load outside state_lock (slow YAML I/O).
        state = load_scenario(scenario_name)
        session = new_session(state, scenario=scenario_name)

        # Phase 3: install session + promote both connections under
        # state_lock. Re-check the room still exists — between Phase 1
        # and Phase 3 a concurrent leave_room could have removed it.
        with app.state_lock():
            if app.rooms.get(room_id) is None:
                return _error(
                    ErrorCode.ROOM_NOT_FOUND, "dev game vanished during join"
                )
            app.sessions[room_id] = session
            # Hardcoded mapping for Phase 1a: slot A = blue, slot B = red.
            app.slot_to_team[room_id] = {Slot.A: Team.BLUE, Slot.B: Team.RED}
            for cid, (rid, _slot) in app.conn_to_room.items():
                if rid == room_id:
                    c = app._connections.get(cid)  # noqa: SLF001
                    if c is not None:
                        c.state = ConnectionState.IN_GAME
            slot_value = slot.value
        return _ok({"room_id": room_id, "slot": slot_value})

    # ---- the 13 game tools, each a thin dispatch wrapper ----

    @mcp.tool()
    def get_state(connection_id: str) -> dict:
        """Read-only. Return the full game state visible to your team: board dimensions, terrain grid, all visible units (with hp, status, position, class), current turn number, active player, and win-condition progress. Fog-of-war hides enemy units outside your vision range. Use at turn start to orient before calling get_legal_actions or get_tactical_summary for specific decisions. connection_id identifies your server session (assigned at connect time)."""
        return _dispatch(app, connection_id, "get_state", {})

    @mcp.tool()
    def get_unit(connection_id: str, unit_id: str) -> dict:
        """Read-only. Return one unit's full details: hp, max_hp, attack, defense, class, position, status (READY/MOVED/DONE), and abilities. Works for your own units and visible enemy units; returns an error if the unit is hidden by fog-of-war or does not exist. unit_id is the string identifier shown in get_state output (e.g. 'blue_archer_1'). Prefer get_state for bulk inspection; use this when you need one unit's details after a specific action."""
        return _dispatch(app, connection_id, "get_unit", {"unit_id": unit_id})

    @mcp.tool()
    def get_unit_range(connection_id: str, unit_id: str) -> dict:
        """Read-only. Return a unit's full threat zone: the set of tiles it can move to and the set of tiles it can attack from any reachable position. Works for any alive unit, own or enemy. unit_id is the string identifier from get_state (e.g. 'red_cavalry_2'). Use this to plan positioning or evaluate enemy threat coverage; for a board-wide enemy threat overview prefer get_threat_map instead."""
        return _dispatch(app, connection_id, "get_unit_range", {"unit_id": unit_id})

    @mcp.tool()
    def get_legal_actions(connection_id: str, unit_id: str) -> dict:
        """Read-only. Return all legal actions for one of your units this turn: movable tiles, attackable enemy unit_ids, healable ally unit_ids, and whether wait is available. Only works on your own units in READY or MOVED status; returns an error for enemy units or units that have already acted. unit_id is the string identifier from get_state. Call this before issuing move, attack, heal, or wait to avoid illegal-action errors."""
        return _dispatch(app, connection_id, "get_legal_actions", {"unit_id": unit_id})

    @mcp.tool()
    def simulate_attack(
        connection_id: str,
        attacker_id: str,
        target_id: str,
        from_tile: dict | None = None,
    ) -> dict:
        """Read-only. Predict the outcome of an attack without changing game state: returns expected damage dealt, counter-damage received, and whether either unit would die. attacker_id and target_id are unit string identifiers from get_state. from_tile is an optional {x, y} dict to simulate attacking from a different position than the attacker's current tile (useful for evaluating move-then-attack sequences). Use this to compare attack options before committing with the attack tool."""
        args: dict = {"attacker_id": attacker_id, "target_id": target_id}
        if from_tile is not None:
            args["from_tile"] = from_tile
        return _dispatch(app, connection_id, "simulate_attack", args)

    @mcp.tool()
    def get_threat_map(connection_id: str) -> dict:
        """Read-only. Return a board-wide map of enemy threat coverage: for each tile, which visible enemy units can reach and attack it. Only includes enemies visible through fog-of-war. Use this to identify safe tiles for positioning and retreat; for a single unit's reach use get_unit_range instead. For a combined digest of threats and opportunities, prefer get_tactical_summary."""
        return _dispatch(app, connection_id, "get_threat_map", {})

    @mcp.tool()
    def get_tactical_summary(connection_id: str) -> dict:
        """Read-only. Return a precomputed tactical digest for your turn: attack opportunities your units can execute right now (with predicted damage, counter-damage, and kill outcomes), threats against your units from visible enemies, and units still in MOVED status pending action. Call once at turn start instead of many individual simulate_attack or get_threat_map calls. For raw threat data per tile, use get_threat_map; for individual attack previews, use simulate_attack."""
        return _dispatch(app, connection_id, "get_tactical_summary", {})

    @mcp.tool()
    def get_history(connection_id: str, last_n: int = 10) -> dict:
        """Read-only. Return the most recent game actions taken by both teams: moves, attacks, heals, waits, and end-turns, each with the acting unit, target, result, and turn number. last_n controls how many actions to return (default 10, max 100). Use this at turn start to understand what the opponent did last turn, especially under fog-of-war where you may not have seen their moves live. For aggregate match statistics use get_match_telemetry instead."""
        return _dispatch(app, connection_id, "get_history", {"last_n": last_n})

    @mcp.tool()
    def move(connection_id: str, unit_id: str, dest: dict) -> dict:
        """Mutating. Move one of your units to a destination tile. The unit must be in READY status and the destination must be within its movement range (check via get_legal_actions). unit_id is the unit's string identifier. dest is an {x, y} dict for the target tile. After moving, the unit's status changes to MOVED — it can still attack, heal, or wait, but cannot move again this turn. Returns the updated unit state. Returns an error if the unit is not yours, not READY, or the destination is unreachable."""
        return _dispatch(app, connection_id, "move", {"unit_id": unit_id, "dest": dest})

    @mcp.tool()
    def attack(connection_id: str, unit_id: str, target_id: str) -> dict:
        """Mutating. Attack an enemy unit, resolving combat and counter-attack immediately. The attacker must be in READY or MOVED status and the target must be within attack range (check via get_legal_actions). unit_id is your attacking unit; target_id is the enemy unit. Both units may take damage; either may die. After attacking, the unit's status becomes DONE for this turn. Use simulate_attack first to preview the outcome without committing. Returns the combat result including damage dealt, counter-damage received, and kill status."""
        return _dispatch(
            app, connection_id, "attack", {"unit_id": unit_id, "target_id": target_id}
        )

    @mcp.tool()
    def heal(connection_id: str, healer_id: str, target_id: str) -> dict:
        """Mutating. Heal an adjacent allied unit. Only units with the heal ability (typically Mages) can use this. healer_id is your healing unit (must be READY or MOVED); target_id is an adjacent allied unit that is damaged. Restores HP based on the healer's magic stat. After healing, the healer's status becomes DONE for this turn. Use get_legal_actions on the healer to see which allies are valid heal targets. Returns the amount healed and the target's updated HP."""
        return _dispatch(
            app, connection_id, "heal", {"healer_id": healer_id, "target_id": target_id}
        )

    @mcp.tool()
    def wait(connection_id: str, unit_id: str) -> dict:
        """Mutating. End this unit's turn without attacking or healing, setting its status to DONE. The unit must be in READY or MOVED status. unit_id is the unit's string identifier. Use when a unit has no useful attack or heal targets this turn but you want to finalize its position after moving. Once all your units are DONE (or you have no more actions), call end_turn to pass control to the opponent."""
        return _dispatch(app, connection_id, "wait", {"unit_id": unit_id})

    @mcp.tool()
    def end_turn(connection_id: str) -> dict:
        """Mutating. End your turn and pass control to the opponent. Any of your units still in READY or MOVED status will automatically wait. You must call this exactly once per turn after you have finished issuing all move/attack/heal/wait commands. The opponent's turn begins immediately after. Returns an error if it is not currently your turn."""
        return _dispatch(app, connection_id, "end_turn", {})

    @mcp.tool()
    def send_to_agent(connection_id: str, team: str, text: str) -> dict:
        """Mutating. Coach-only tool: queue a natural-language message that will be delivered to the specified team's AI agent at the start of its next turn. team must be 'blue' or 'red'. text is the coaching instruction (e.g. 'push cavalry on the right flank'). The agent sees the message as context but is free to ignore it. Only human coach connections can use this; AI agent connections receive an error. Messages are not visible to the opposing team."""
        return _dispatch(app, connection_id, "send_to_agent", {"team": team, "text": text})

    @mcp.tool()
    def record_thought(connection_id: str, text: str) -> dict:
        """Mutating. Record an agent's chain-of-thought reasoning into the match replay file so it can be reviewed in post-match replay playback. text is the reasoning text (max 10,000 characters). Called automatically by the client harness after each agent response — not typically called by the agent itself. Requires state=in_game. The thought is attributed to your team based on your slot assignment."""
        with app.state_lock():
            conn = app._connections.get(connection_id)  # noqa: SLF001
            if conn is None:
                return _error(
                    ErrorCode.NOT_REGISTERED, "call set_player_metadata first"
                )
            if conn.state != ConnectionState.IN_GAME:
                return _error(
                    ErrorCode.TOOL_NOT_AVAILABLE_IN_STATE,
                    "record_thought requires state=in_game",
                )
            info = app.conn_to_room.get(connection_id)
            if info is None:
                return _error(
                    ErrorCode.GAME_NOT_STARTED,
                    "no active game for this connection",
                )
            room_id, slot = info
            session = app.sessions.get(room_id)
            mapping = app.slot_to_team.get(room_id)
            if session is None or mapping is None:
                return _error(
                    ErrorCode.GAME_NOT_STARTED,
                    "no active game for this connection",
                )
            viewer = mapping[slot]
            # Don't update last_game_activity_at — the heartbeat sweeper
            # uses that to detect a wedged TUI. A reasoning push proves
            # the agent loop is alive but doesn't prove the player can
            # still ACT, so keep it out of the liveness signal. Bare
            # heartbeat handles transport-level liveness already.

        text = sanitize_freetext(text, max_length=10_000)
        try:
            session.add_thought(viewer, text)
        except Exception as e:  # pragma: no cover - defensive
            log.exception("record_thought add_thought raised")
            return _error(ErrorCode.INTERNAL, str(e))
        return _ok({})

    @mcp.tool()
    def report_issue(
        connection_id: str,
        category: str,
        summary: str,
        details: str | None = None,
    ) -> dict:
        """Mutating. Report a problem or observation encountered during gameplay. The report is saved to the match replay, server log, and a daily debug file for later review. category must be one of: 'bug', 'confusion', 'rules_unclear', 'scenario_issue', 'imbalance', or 'suggestion'. Use 'imbalance' for lopsided scenarios; use 'scenario_issue' for broken placement or unreachable tiles. summary is a short description (max 500 chars, required). details is an optional longer explanation (max 10,000 chars). Requires state=in_game."""
        allowed = (
            "bug", "confusion", "rules_unclear",
            "scenario_issue", "imbalance", "suggestion",
        )
        if category not in allowed:
            return _error(
                ErrorCode.INVALID_ARGUMENT,
                f"category must be one of {allowed}, got {category!r}",
            )
        summary = sanitize_freetext(summary, max_length=500)
        if not summary:
            return _error(ErrorCode.INVALID_ARGUMENT, "summary must be non-empty")
        details_clean = (
            sanitize_freetext(details, max_length=10_000) if details else None
        )

        with app.state_lock():
            conn = app._connections.get(connection_id)  # noqa: SLF001
            if conn is None:
                return _error(
                    ErrorCode.NOT_REGISTERED, "call set_player_metadata first"
                )
            if conn.state != ConnectionState.IN_GAME:
                return _error(
                    ErrorCode.TOOL_NOT_AVAILABLE_IN_STATE,
                    "report_issue requires state=in_game",
                )
            info = app.conn_to_room.get(connection_id)
            if info is None:
                return _error(
                    ErrorCode.GAME_NOT_STARTED,
                    "no active game for this connection",
                )
            room_id, slot = info
            session = app.sessions.get(room_id)
            mapping = app.slot_to_team.get(room_id)
            if session is None or mapping is None:
                return _error(
                    ErrorCode.GAME_NOT_STARTED,
                    "no active game for this connection",
                )
            viewer = mapping[slot]
            player = conn.player
            player_info = {
                "display_name": player.display_name if player else None,
                "provider": getattr(player, "provider", None) if player else None,
                "model": getattr(player, "model", None) if player else None,
            }

        # Build the event once; reused across all three sinks.
        import time as _time
        ts = _time.time()
        event = {
            "timestamp": ts,
            "room_id": room_id,
            "turn": session.state.turn,
            "team": viewer.value,
            "player": player_info,
            "category": category,
            "summary": summary,
            "details": details_clean,
        }

        # Sink 1: match replay.
        session.log("agent_report", {k: v for k, v in event.items() if k != "room_id"})

        # Sink 2: dedicated logger (lands in server log).
        _report_log = logging.getLogger("silicon.agent_report")
        _report_log.info(
            "agent_report room=%s turn=%d team=%s player=%s category=%s "
            "summary=%r details=%r",
            room_id, event["turn"], viewer.value,
            player_info.get("display_name"), category, summary, details_clean,
        )

        # Sink 3: per-day jsonl in ~/.silicon-pantheon/debug-reports/.
        try:
            _append_agent_report_jsonl(event)
        except Exception:
            from silicon_pantheon.shared.debug import reraise_in_debug
            reraise_in_debug(log, "report_issue: jsonl append failed")
            log.exception("report_issue: jsonl append failed (ignored)")

        return _ok({"recorded": True})

    @mcp.tool()
    def report_tokens(connection_id: str, tokens: int) -> dict:
        """Mutating. Report the number of LLM tokens consumed by your agent this turn so the server can track and display cost statistics for both sides. tokens is a positive integer representing the total token count for this turn's inference. Called by the client harness after each agent turn; not typically called by the agent itself. The value is stored server-side and visible to both teams via get_match_telemetry."""
        return _dispatch(app, connection_id, "report_tokens", {"tokens": tokens})

    @mcp.tool()
    def get_match_telemetry(connection_id: str) -> dict:
        """Read-only. Return server-tracked match statistics for both teams: total tokens consumed, per-turn thinking time, number of tool calls, and turn count. Available during and after a match. Use this for post-game analysis or mid-game cost monitoring. For game-state history (what moves were made) use get_history instead."""
        resolved = _viewer_for_any_state(app, connection_id)
        if resolved is None:
            return _error(ErrorCode.GAME_NOT_STARTED, "no game session")
        session, _viewer = resolved
        from silicon_pantheon.server.tools import get_match_telemetry as _get_telemetry
        with session.lock:
            result = _get_telemetry(session, _viewer)
        return _ok({"result": result})

    @mcp.tool()
    def download_replay(connection_id: str) -> dict:
        """Read-only. Download the full match replay as JSONL text. Each line is a JSON event (moves, attacks, thoughts, turn boundaries) that can be fed into the TUI replay viewer. Available during and immediately after a match while the connection is still in_game state. Returns the replay content and the server-side file path."""
        # Phase 1: resolve under state_lock.
        with app.state_lock():
            conn = app._connections.get(connection_id)  # noqa: SLF001
            if conn is None:
                log.warning(
                    "download_replay rejected: unknown cid=%s "
                    "(known_cids=%d, conn_to_room_keys=%s)",
                    connection_id, len(app._connections),  # noqa: SLF001
                    list(app.conn_to_room.keys())[:5],
                )
                return _error(
                    ErrorCode.NOT_REGISTERED, "call set_player_metadata first"
                )
            if conn.state != ConnectionState.IN_GAME:
                # Diagnostic for the "winner pressed d on post-match,
                # got download_replay requires state=in_game" report.
                seated = app.conn_to_room.get(connection_id)
                sess_present = (
                    seated is not None and seated[0] in app.sessions
                )
                log.warning(
                    "download_replay rejected: cid=%s state=%s "
                    "(expected in_game) seated=%s session_present=%s "
                    "player=%s",
                    connection_id, conn.state.value, seated,
                    sess_present,
                    getattr(conn, "player", None),
                )
                return _error(
                    ErrorCode.TOOL_NOT_AVAILABLE_IN_STATE,
                    "download_replay requires state=in_game",
                )
            info = app.conn_to_room.get(connection_id)
            if info is None:
                return _error(ErrorCode.NOT_IN_ROOM, "connection not seated")
            room_id, _slot = info
            session = app.sessions.get(room_id)
            if session is None:
                return _error(
                    ErrorCode.GAME_NOT_STARTED, "no session for this room"
                )
            if session.replay is None:
                return _error(
                    ErrorCode.BAD_INPUT,
                    "this match was not configured with a replay writer",
                )
            replay_path = session.replay.path

        # Phase 2: file read outside state_lock.
        try:
            with open(replay_path, encoding="utf-8") as f:
                body = f.read()
        except OSError as e:
            return _error(ErrorCode.INTERNAL, f"failed to read replay: {e}")
        return _ok({"replay_jsonl": body, "path": str(replay_path)})

    @mcp.tool()
    def concede(connection_id: str) -> dict:
        """Mutating. Resign the match — the opponent wins immediately. The result is recorded in the leaderboard as a concession. Requires state=in_game. After conceding, the match ends and both players can download the replay or leave the room. Use leave_room if you want to both concede and return to the lobby in one step."""
        from silicon_pantheon.server.engine.state import GameStatus

        # Phase 1: resolve under state_lock.
        with app.state_lock():
            conn = app._connections.get(connection_id)  # noqa: SLF001
            if conn is None or conn.state != ConnectionState.IN_GAME:
                return _error(
                    ErrorCode.TOOL_NOT_AVAILABLE_IN_STATE,
                    "concede requires state=in_game",
                )
            info = app.conn_to_room.get(connection_id)
            if info is None:
                return _error(ErrorCode.NOT_IN_ROOM, "connection not seated")
            room_id, slot = info
            session = app.sessions.get(room_id)
            if session is None:
                return _error(ErrorCode.GAME_NOT_STARTED, "no session")
            team_map = app.slot_to_team.get(room_id, {})
            my_team = team_map.get(slot)
            if my_team is None:
                return _error(ErrorCode.INTERNAL, "no team mapping")

        opponent = my_team.other()

        # Phase 2: flip status under session.lock.
        with session.lock:
            if session.state.status != GameStatus.GAME_OVER:
                session.state.status = GameStatus.GAME_OVER
                session.state.winner = opponent
                session.log(
                    "concede",
                    {"by": my_team.value, "winner": opponent.value},
                )

        # Phase 3: _note_game_over_if_needed with its own protocol.
        _note_game_over_if_needed(app, room_id)
        return _ok({"winner": opponent.value})
