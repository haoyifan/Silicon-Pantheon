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
from pathlib import Path
from typing import Any

log = logging.getLogger("silicon.leaderboard")

DB_PATH = Path.home() / ".silicon-pantheon" / "leaderboard.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS match_results (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    model            TEXT NOT NULL,
    provider         TEXT NOT NULL,
    scenario         TEXT NOT NULL,
    team             TEXT NOT NULL,
    outcome          TEXT NOT NULL,
    turns_played     INTEGER NOT NULL,
    avg_think_time_s REAL NOT NULL,
    total_tokens     INTEGER NOT NULL,
    tool_calls       INTEGER NOT NULL,
    errors           INTEGER NOT NULL,
    timestamp        REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_model ON match_results(model);
"""


def _get_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_SCHEMA)
    return conn


def record_match(
    session: Any,
    room: Any,
    slot_to_team: dict,
) -> None:
    """Write 2 rows (one per team) into match_results.

    Called from _note_game_over_if_needed after the game ends.
    Non-fatal: exceptions are caught by the caller.
    """
    from silicon_pantheon.server.engine.state import Team
    from silicon_pantheon.server.rooms import Slot

    db = _get_db()
    try:
        for slot, seat in room.seats.items():
            if seat.player is None:
                continue
            team = slot_to_team.get(slot)
            if team is None:
                continue

            model = seat.player.model or "unknown"
            provider = seat.player.provider or "unknown"

            if session.state.winner is None:
                outcome = "draw"
            elif session.state.winner == team:
                outcome = "win"
            else:
                outcome = "loss"

            times = session.turn_times_by_team.get(team, [])
            avg_think = sum(times) / len(times) if times else 0.0

            db.execute(
                """INSERT INTO match_results
                   (model, provider, scenario, team, outcome, turns_played,
                    avg_think_time_s, total_tokens, tool_calls, errors, timestamp)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    model,
                    provider,
                    session.scenario or "?",
                    team.value,
                    outcome,
                    session.state.turn,
                    round(avg_think, 2),
                    session.tokens_by_team.get(team, 0),
                    session.tool_calls_by_team.get(team, 0),
                    session.tool_errors_by_team.get(team, 0),
                    time.time(),
                ),
            )
        db.commit()
        log.info(
            "leaderboard: recorded match scenario=%s winner=%s",
            session.scenario,
            session.state.winner.value if session.state.winner else "draw",
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
