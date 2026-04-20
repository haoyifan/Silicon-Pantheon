"""SQLite-backed leaderboard for per-model win/loss/draw tracking.

Writes one row per team per completed match. Reads aggregate stats
grouped by model for the lobby leaderboard panel.

The database lives at ~/.silicon-pantheon/leaderboard.db — survives
server restarts. WAL mode for safe concurrent reads during writes.
"""

from __future__ import annotations

import logging
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

log = logging.getLogger("silicon.leaderboard")

DB_PATH = Path.home() / ".silicon-pantheon" / "leaderboard.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS match_results (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id         TEXT,
    model            TEXT NOT NULL,
    provider         TEXT NOT NULL,
    scenario         TEXT NOT NULL,
    team             TEXT NOT NULL,
    outcome          TEXT NOT NULL,
    turns_played     INTEGER NOT NULL,
    avg_think_time_s REAL NOT NULL,
    max_think_time_s REAL,
    total_tokens     INTEGER NOT NULL,
    tool_calls       INTEGER NOT NULL,
    errors           INTEGER NOT NULL,
    units_killed     INTEGER,
    units_lost       INTEGER,
    damage_dealt     INTEGER,
    damage_taken     INTEGER,
    thoughts_count   INTEGER,
    match_duration_s REAL,
    timestamp        REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_model ON match_results(model);
"""

# Columns added after the initial schema. Each is applied via
# ALTER TABLE ADD COLUMN on existing DBs if missing.
_MIGRATIONS = [
    ("match_id", "TEXT"),
    ("max_think_time_s", "REAL"),
    ("units_killed", "INTEGER"),
    ("units_lost", "INTEGER"),
    ("damage_dealt", "INTEGER"),
    ("damage_taken", "INTEGER"),
    ("thoughts_count", "INTEGER"),
    ("match_duration_s", "REAL"),
]


def _get_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_SCHEMA)
    existing = {r[1] for r in conn.execute("PRAGMA table_info(match_results)").fetchall()}
    for col, sql_type in _MIGRATIONS:
        if col not in existing:
            conn.execute(f"ALTER TABLE match_results ADD COLUMN {col} {sql_type}")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_match_id ON match_results(match_id)")
    conn.commit()
    return conn


def record_match(
    session: Any,
    room: Any,
    slot_to_team: dict,
) -> None:
    """Write 2 rows (one per team) into match_results, linked by match_id.

    Called from _note_game_over_if_needed after the game ends.
    Non-fatal: exceptions are caught by the caller.

    ── Locking ──
    Acquires ``session.lock`` briefly to snapshot every field we'll
    write, then releases before SQLite I/O. Rationale: even after
    GAME_OVER, a handful of paths keep mutating session state —
    specifically ``report_tokens`` (increments tokens_by_team under
    session.lock), ``call_tool`` (increments tool_calls_by_team),
    and ``add_thought`` (appends to thoughts deque under session.lock
    as of this commit). Iterating those collections unlocked can
    raise ``RuntimeError`` on concurrent modification or return
    inconsistent counts. Snapshotting avoids both.
    """
    from silicon_pantheon.server.rooms import Slot  # noqa: F401
    from silicon_pantheon.server.engine.state import Team

    # ── Snapshot under session.lock ──
    with session.lock:
        match_start_time = session.match_start_time
        scenario = session.scenario
        winner = session.state.winner
        turn = session.state.turn
        fallen = dict(getattr(session.state, "fallen_units", {}) or {})
        # Copy units dict (shallow: Unit objects are mutated under
        # session.lock only, and once GAME_OVER is set apply()
        # rejects further mutations, so shallow is safe).
        units = list(session.state.units.values())
        turn_times = {
            t: list(session.turn_times_by_team.get(t, []))
            for t in (Team.BLUE, Team.RED)
        }
        tokens_by_team = dict(session.tokens_by_team)
        tool_calls_by_team = dict(session.tool_calls_by_team)
        tool_errors_by_team = dict(session.tool_errors_by_team)
        kills_by_team = dict(session.kills_by_team)
        damage_dealt_by_team = dict(session.damage_dealt_by_team)
        damage_taken_by_team = dict(session.damage_taken_by_team)
        thoughts_by_team: dict = {Team.BLUE: 0, Team.RED: 0}
        for th in session.thoughts:
            thoughts_by_team[th.team] = thoughts_by_team.get(th.team, 0) + 1

    # ── SQLite I/O outside session.lock ──
    db = _get_db()
    try:
        match_id = uuid.uuid4().hex
        now = time.time()
        duration = now - match_start_time if match_start_time > 0 else 0.0

        start_counts: dict = {}
        end_alive: dict = {}
        for team in (Team.BLUE, Team.RED):
            alive = sum(1 for u in units if u.owner == team and u.alive)
            dead = sum(1 for u in fallen.values() if u.owner == team)
            start_counts[team] = alive + dead
            end_alive[team] = alive

        for slot, seat in room.seats.items():
            if seat.player is None:
                continue
            team = slot_to_team.get(slot)
            if team is None:
                continue

            model = seat.player.model or "unknown"
            provider = seat.player.provider or "unknown"

            if winner is None:
                outcome = "draw"
            elif winner == team:
                outcome = "win"
            else:
                outcome = "loss"

            times = turn_times.get(team, [])
            avg_think = sum(times) / len(times) if times else 0.0
            max_think = max(times) if times else 0.0

            units_lost = start_counts.get(team, 0) - end_alive.get(team, 0)

            db.execute(
                """INSERT INTO match_results
                   (match_id, model, provider, scenario, team, outcome,
                    turns_played, avg_think_time_s, max_think_time_s,
                    total_tokens, tool_calls, errors,
                    units_killed, units_lost,
                    damage_dealt, damage_taken,
                    thoughts_count, match_duration_s, timestamp)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    match_id,
                    model,
                    provider,
                    scenario or "?",
                    team.value,
                    outcome,
                    turn,
                    round(avg_think, 2),
                    round(max_think, 2),
                    tokens_by_team.get(team, 0),
                    tool_calls_by_team.get(team, 0),
                    tool_errors_by_team.get(team, 0),
                    kills_by_team.get(team, 0),
                    units_lost,
                    damage_dealt_by_team.get(team, 0),
                    damage_taken_by_team.get(team, 0),
                    thoughts_by_team.get(team, 0),
                    round(duration, 2),
                    now,
                ),
            )
        db.commit()
        log.info(
            "leaderboard: recorded match scenario=%s winner=%s match_id=%s",
            scenario,
            winner.value if winner else "draw",
            match_id,
        )
    finally:
        db.close()


def query_leaderboard() -> list[dict]:
    """Aggregate per-model stats across all matches."""
    try:
        db = _get_db()
    except Exception:
        log.debug("leaderboard: DB not available", exc_info=True)
        return []
    try:
        rows = db.execute(
            """
            SELECT
                model,
                provider,
                COUNT(*)                                         AS games,
                SUM(CASE WHEN outcome='win'  THEN 1 ELSE 0 END) AS wins,
                SUM(CASE WHEN outcome='loss' THEN 1 ELSE 0 END) AS losses,
                SUM(CASE WHEN outcome='draw' THEN 1 ELSE 0 END) AS draws,
                AVG(avg_think_time_s)                            AS avg_think
            FROM match_results
            GROUP BY model, provider
            ORDER BY
                CAST(SUM(CASE WHEN outcome='win' THEN 1 ELSE 0 END) AS REAL)
                    / MAX(COUNT(*), 1) DESC,
                COUNT(*) DESC
            LIMIT 100
            """
        ).fetchall()
        return [
            {
                "model": r[0],
                "provider": r[1],
                "games": r[2],
                "wins": r[3],
                "losses": r[4],
                "draws": r[5],
                "avg_think_time_s": round(r[6], 1) if r[6] else 0.0,
            }
            for r in rows
        ]
    finally:
        db.close()


def query_head_to_head(model: str, provider: str) -> list[dict]:
    """Per-opponent stats for a single model. Self-join on match_id.

    Requires both rows to have a non-NULL match_id (historical rows
    written before the migration are skipped).
    """
    try:
        db = _get_db()
    except Exception:
        return []
    try:
        rows = db.execute(
            """
            SELECT
                opp.model                                              AS opponent,
                opp.provider                                           AS opp_provider,
                COUNT(*)                                               AS games,
                SUM(CASE WHEN me.outcome='win'  THEN 1 ELSE 0 END)    AS wins,
                SUM(CASE WHEN me.outcome='loss' THEN 1 ELSE 0 END)    AS losses,
                SUM(CASE WHEN me.outcome='draw' THEN 1 ELSE 0 END)    AS draws,
                AVG(me.turns_played)                                  AS avg_turns
            FROM match_results me
            JOIN match_results opp
              ON me.match_id = opp.match_id
             AND me.team    != opp.team
            WHERE me.model = ? AND me.provider = ?
              AND me.match_id IS NOT NULL
            GROUP BY opp.model, opp.provider
            ORDER BY games DESC,
                     CAST(SUM(CASE WHEN me.outcome='win' THEN 1 ELSE 0 END) AS REAL)
                         / MAX(COUNT(*), 1) DESC
            """,
            (model, provider),
        ).fetchall()
        return [
            {
                "opponent": r[0],
                "provider": r[1],
                "games": r[2],
                "wins": r[3],
                "losses": r[4],
                "draws": r[5],
                "avg_turns": round(r[6], 1) if r[6] else 0.0,
            }
            for r in rows
        ]
    finally:
        db.close()


def query_per_scenario(model: str, provider: str) -> list[dict]:
    """Per-scenario stats for a single model, sorted by win% descending."""
    try:
        db = _get_db()
    except Exception:
        return []
    try:
        rows = db.execute(
            """
            SELECT
                scenario,
                COUNT(*)                                         AS games,
                SUM(CASE WHEN outcome='win'  THEN 1 ELSE 0 END) AS wins,
                SUM(CASE WHEN outcome='loss' THEN 1 ELSE 0 END) AS losses,
                SUM(CASE WHEN outcome='draw' THEN 1 ELSE 0 END) AS draws
            FROM match_results
            WHERE model = ? AND provider = ?
            GROUP BY scenario
            ORDER BY
                CAST(SUM(CASE WHEN outcome='win' THEN 1 ELSE 0 END) AS REAL)
                    / MAX(COUNT(*), 1) DESC,
                COUNT(*) DESC
            """,
            (model, provider),
        ).fetchall()
        return [
            {
                "scenario": r[0],
                "games": r[1],
                "wins": r[2],
                "losses": r[3],
                "draws": r[4],
            }
            for r in rows
        ]
    finally:
        db.close()


def query_model_details(model: str, provider: str) -> dict:
    """Aggregated detail stats for the model-details drill-down.

    Returns totals + computed metrics (tokens-per-win, error rate,
    avg units killed/lost, avg/max think time, avg match duration).
    """
    try:
        db = _get_db()
    except Exception:
        return {}
    try:
        row = db.execute(
            """
            SELECT
                COUNT(*)                                                AS games,
                SUM(CASE WHEN outcome='win'  THEN 1 ELSE 0 END)        AS wins,
                SUM(CASE WHEN outcome='loss' THEN 1 ELSE 0 END)        AS losses,
                SUM(CASE WHEN outcome='draw' THEN 1 ELSE 0 END)        AS draws,
                SUM(total_tokens)                                      AS tokens,
                SUM(tool_calls)                                        AS tool_calls,
                SUM(errors)                                            AS errors,
                AVG(avg_think_time_s)                                  AS avg_think,
                MAX(max_think_time_s)                                  AS max_think,
                AVG(units_killed)                                      AS avg_kills,
                AVG(units_lost)                                        AS avg_lost,
                AVG(match_duration_s)                                  AS avg_duration
            FROM match_results
            WHERE model = ? AND provider = ?
            """,
            (model, provider),
        ).fetchone()
        if row is None or row[0] == 0:
            return {}
        games, wins, losses, draws, tokens, tool_calls, errors = row[:7]
        avg_think, max_think, avg_kills, avg_lost, avg_duration = row[7:]
        tokens = tokens or 0
        tool_calls = tool_calls or 0
        errors = errors or 0
        return {
            "model": model,
            "provider": provider,
            "games": games,
            "wins": wins,
            "losses": losses,
            "draws": draws,
            "win_pct": (wins / games * 100) if games else 0.0,
            "total_tokens": tokens,
            "tokens_per_win": (tokens / wins) if wins else None,
            "total_tool_calls": tool_calls,
            "total_errors": errors,
            "error_rate_pct": (errors / tool_calls * 100) if tool_calls else 0.0,
            "avg_think_time_s": round(avg_think, 1) if avg_think else 0.0,
            "max_think_time_s": round(max_think, 1) if max_think else 0.0,
            "avg_units_killed": round(avg_kills, 1) if avg_kills is not None else None,
            "avg_units_lost": round(avg_lost, 1) if avg_lost is not None else None,
            "avg_match_duration_s": round(avg_duration, 1) if avg_duration else 0.0,
        }
    finally:
        db.close()
