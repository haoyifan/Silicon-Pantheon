"""Tests for leaderboard head-to-head + per-scenario + model-details
queries. Uses a temporary DB path via monkeypatch so the real
~/.silicon-pantheon/leaderboard.db is untouched."""

from __future__ import annotations

import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from silicon_pantheon.server import leaderboard
from silicon_pantheon.server.engine.state import Team


class _FakeSeat:
    def __init__(
        self,
        slot: str,
        model: str,
        provider: str,
        display_name: str | None = None,
    ):
        self.slot = slot
        self.player = SimpleNamespace(
            model=model, provider=provider, display_name=display_name,
        )


class _FakeRoom:
    def __init__(self, seats: dict):
        self.seats = seats


def _fresh_db(tmp_path: Path, monkeypatch) -> Path:
    db_path = tmp_path / "leaderboard.db"
    monkeypatch.setattr(leaderboard, "DB_PATH", db_path)
    return db_path


class _FakeState:
    def __init__(self, *, winner, turn, units, fallen):
        self.winner = winner
        self.turn = turn
        self.units = units
        self.fallen_units = fallen


class _FakeUnit:
    def __init__(self, owner, alive=True):
        self.owner = owner
        self.alive = alive


def _fake_session(*, winner, scenario="03_thermopylae"):
    # Blue starting roster: 3; 1 killed so 2 alive at end.
    units = {
        "u_b_1": _FakeUnit(Team.BLUE, alive=True),
        "u_b_2": _FakeUnit(Team.BLUE, alive=True),
        "u_r_1": _FakeUnit(Team.RED, alive=True),
    }
    fallen = {
        "u_b_3": _FakeUnit(Team.BLUE, alive=False),
        "u_r_2": _FakeUnit(Team.RED, alive=False),
        "u_r_3": _FakeUnit(Team.RED, alive=False),
    }
    state = _FakeState(winner=winner, turn=12, units=units, fallen=fallen)
    import threading
    return SimpleNamespace(
        state=state,
        scenario=scenario,
        match_start_time=time.time() - 30.0,
        tokens_by_team={Team.BLUE: 5000, Team.RED: 4000},
        tool_calls_by_team={Team.BLUE: 40, Team.RED: 35},
        tool_errors_by_team={Team.BLUE: 2, Team.RED: 1},
        damage_dealt_by_team={Team.BLUE: 80, Team.RED: 45},
        damage_taken_by_team={Team.BLUE: 45, Team.RED: 80},
        kills_by_team={Team.BLUE: 2, Team.RED: 1},
        turn_times_by_team={Team.BLUE: [3.0, 5.0, 7.0], Team.RED: [2.0, 4.0, 6.0]},
        thoughts=[],
        # Match the real Session.lock so record_match can snapshot
        # fields atomically. See server/session.py.
        lock=threading.Lock(),
    )


def test_migration_adds_new_columns(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    db = leaderboard._get_db()
    try:
        cols = {r[1] for r in db.execute("PRAGMA table_info(match_results)").fetchall()}
        for col in (
            "match_id", "max_think_time_s", "units_killed", "units_lost",
            "damage_dealt", "damage_taken", "thoughts_count", "match_duration_s",
            "display_name",
        ):
            assert col in cols, f"missing column {col}"
    finally:
        db.close()


def test_record_match_persists_display_name(tmp_path, monkeypatch):
    """Two bots on the same model/provider must remain distinguishable
    via display_name — this is the whole point of adding the column.
    Regression for pre-fix DBs where Alexander and Napoleon (both
    grok-3-mini/xai) collapsed into one set of rows."""
    _fresh_db(tmp_path, monkeypatch)
    session = _fake_session(winner=Team.BLUE)
    seats = {
        "a": _FakeSeat("a", "grok-3-mini", "xai", display_name="Alexander"),
        "b": _FakeSeat("b", "grok-3-mini", "xai", display_name="Napoleon"),
    }
    slot_to_team = {"a": Team.BLUE, "b": Team.RED}
    leaderboard.record_match(session, _FakeRoom(seats), slot_to_team)

    db = leaderboard._get_db()
    try:
        rows = db.execute(
            "SELECT display_name, team, outcome FROM match_results "
            "ORDER BY team"
        ).fetchall()
        assert rows == [
            ("Alexander", "blue", "win"),
            ("Napoleon", "red", "loss"),
        ]
    finally:
        db.close()


def test_record_match_display_name_optional(tmp_path, monkeypatch):
    """Legacy seats without a display_name (Player has no attribute,
    or None) must still record cleanly — display_name stays NULL."""
    _fresh_db(tmp_path, monkeypatch)
    session = _fake_session(winner=Team.BLUE)
    seats = {
        "a": _FakeSeat("a", "claude-opus-4-7", "anthropic"),  # no display_name
        "b": _FakeSeat("b", "gpt-5", "openai"),
    }
    slot_to_team = {"a": Team.BLUE, "b": Team.RED}
    leaderboard.record_match(session, _FakeRoom(seats), slot_to_team)

    db = leaderboard._get_db()
    try:
        names = [r[0] for r in db.execute(
            "SELECT display_name FROM match_results"
        ).fetchall()]
        assert names == [None, None]
    finally:
        db.close()


def test_migration_idempotent_on_old_schema(tmp_path, monkeypatch):
    """An existing DB with only the original columns should pick up the
    new columns via ALTER TABLE without crashing."""
    db_path = tmp_path / "leaderboard.db"
    monkeypatch.setattr(leaderboard, "DB_PATH", db_path)
    import sqlite3
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE match_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            model TEXT NOT NULL,
            provider TEXT NOT NULL,
            scenario TEXT NOT NULL,
            team TEXT NOT NULL,
            outcome TEXT NOT NULL,
            turns_played INTEGER NOT NULL,
            avg_think_time_s REAL NOT NULL,
            total_tokens INTEGER NOT NULL,
            tool_calls INTEGER NOT NULL,
            errors INTEGER NOT NULL,
            timestamp REAL NOT NULL
        );
        """
    )
    conn.commit()
    conn.close()
    # Now _get_db should upgrade the schema.
    db = leaderboard._get_db()
    try:
        cols = {r[1] for r in db.execute("PRAGMA table_info(match_results)").fetchall()}
        assert "match_id" in cols
        assert "units_killed" in cols
    finally:
        db.close()


def test_record_match_pairs_with_match_id(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    session = _fake_session(winner=Team.BLUE)
    seats = {
        "a": _FakeSeat("a", "claude-opus-4-7", "anthropic"),
        "b": _FakeSeat("b", "gpt-5", "openai"),
    }
    room = _FakeRoom(seats)
    slot_to_team = {"a": Team.BLUE, "b": Team.RED}
    leaderboard.record_match(session, room, slot_to_team)

    db = leaderboard._get_db()
    try:
        rows = db.execute(
            "SELECT match_id, model, outcome, units_killed, units_lost, damage_dealt, damage_taken "
            "FROM match_results ORDER BY id"
        ).fetchall()
        assert len(rows) == 2
        # Both rows share match_id.
        assert rows[0][0] == rows[1][0] and rows[0][0] is not None
        # Outcomes are opposite.
        outcomes = {rows[0][2], rows[1][2]}
        assert outcomes == {"win", "loss"}
        # New fields populated.
        for r in rows:
            assert r[3] is not None  # units_killed
            assert r[4] is not None  # units_lost
            assert r[5] is not None  # damage_dealt
            assert r[6] is not None  # damage_taken
    finally:
        db.close()


def test_query_head_to_head(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    # Record 3 matches: Claude vs GPT (Claude wins 2, loses 1).
    for winner in (Team.BLUE, Team.BLUE, Team.RED):
        session = _fake_session(winner=winner)
        seats = {
            "a": _FakeSeat("a", "claude", "anthropic"),
            "b": _FakeSeat("b", "gpt", "openai"),
        }
        room = _FakeRoom(seats)
        leaderboard.record_match(session, room, {"a": Team.BLUE, "b": Team.RED})

    h2h = leaderboard.query_head_to_head("claude", "anthropic")
    assert len(h2h) == 1
    entry = h2h[0]
    assert entry["opponent"] == "gpt"
    assert entry["games"] == 3
    assert entry["wins"] == 2
    assert entry["losses"] == 1
    assert entry["draws"] == 0


def test_query_per_scenario(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    # Record matches on two scenarios.
    scenarios = ["03_thermopylae", "03_thermopylae", "04_cannae"]
    winners = [Team.BLUE, Team.BLUE, Team.RED]
    for scenario, winner in zip(scenarios, winners):
        session = _fake_session(winner=winner, scenario=scenario)
        seats = {
            "a": _FakeSeat("a", "claude", "anthropic"),
            "b": _FakeSeat("b", "gpt", "openai"),
        }
        leaderboard.record_match(session, _FakeRoom(seats), {"a": Team.BLUE, "b": Team.RED})

    per = leaderboard.query_per_scenario("claude", "anthropic")
    by_name = {e["scenario"]: e for e in per}
    assert by_name["03_thermopylae"]["games"] == 2
    assert by_name["03_thermopylae"]["wins"] == 2
    assert by_name["04_cannae"]["games"] == 1
    assert by_name["04_cannae"]["wins"] == 0
    assert by_name["04_cannae"]["losses"] == 1
    # Best scenario (thermopylae) sorts first.
    assert per[0]["scenario"] == "03_thermopylae"


def test_query_model_details_aggregates(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    session = _fake_session(winner=Team.BLUE)
    seats = {
        "a": _FakeSeat("a", "claude", "anthropic"),
        "b": _FakeSeat("b", "gpt", "openai"),
    }
    leaderboard.record_match(session, _FakeRoom(seats), {"a": Team.BLUE, "b": Team.RED})

    d = leaderboard.query_model_details("claude", "anthropic")
    assert d["games"] == 1
    assert d["wins"] == 1
    assert d["win_pct"] == pytest.approx(100.0)
    # Tokens per win = total_tokens (5000) / wins (1) = 5000
    assert d["tokens_per_win"] == pytest.approx(5000)
    # Error rate = 2 / 40 = 5.0%
    assert d["error_rate_pct"] == pytest.approx(5.0)
    assert d["avg_units_killed"] == pytest.approx(2.0)
    assert d["avg_units_lost"] == pytest.approx(1.0)
    assert d["max_think_time_s"] > 0


def test_query_model_details_empty(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    d = leaderboard.query_model_details("nobody", "nowhere")
    assert d == {}


def test_query_head_to_head_ignores_legacy_rows(tmp_path, monkeypatch):
    """Old rows predating the migration have NULL match_id; h2h must skip them."""
    db_path = _fresh_db(tmp_path, monkeypatch)
    # Force schema creation, then write legacy rows directly.
    db = leaderboard._get_db()
    try:
        db.execute(
            """INSERT INTO match_results
               (match_id, model, provider, scenario, team, outcome,
                turns_played, avg_think_time_s, total_tokens, tool_calls, errors, timestamp)
               VALUES (NULL, 'oldmodel', 'x', 's', 'blue', 'win', 10, 1.0, 100, 5, 0, ?)""",
            (time.time(),),
        )
        db.execute(
            """INSERT INTO match_results
               (match_id, model, provider, scenario, team, outcome,
                turns_played, avg_think_time_s, total_tokens, tool_calls, errors, timestamp)
               VALUES (NULL, 'oldopp', 'y', 's', 'red', 'loss', 10, 1.0, 100, 5, 0, ?)""",
            (time.time(),),
        )
        db.commit()
    finally:
        db.close()
    # h2h should return nothing since match_id is NULL on both rows.
    assert leaderboard.query_head_to_head("oldmodel", "x") == []
