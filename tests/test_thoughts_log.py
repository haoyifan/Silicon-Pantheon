"""Tests for the plain-text ThoughtsLogWriter streamed by Session.add_thought."""

from __future__ import annotations

from pathlib import Path

from clash_of_robots.server.engine.scenarios import load_scenario
from clash_of_robots.server.engine.state import Team
from clash_of_robots.server.session import new_session


def test_thoughts_log_is_appended_live(tmp_path: Path) -> None:
    state = load_scenario("01_tiny_skirmish")
    log_path = tmp_path / "thoughts.log"
    session = new_session(state, thoughts_log_path=log_path)
    try:
        session.add_thought(Team.BLUE, "I'll push the archer forward")
        session.add_thought(Team.RED, "Counter with cavalry.\nFast move.")
    finally:
        assert session.thoughts_log is not None
        session.thoughts_log.close()

    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert lines == [
        "[T1 blue] I'll push the archer forward",
        "[T1 red] Counter with cavalry. Fast move.",
    ]


def test_no_path_means_no_file(tmp_path: Path) -> None:
    state = load_scenario("01_tiny_skirmish")
    session = new_session(state)
    session.add_thought(Team.BLUE, "foo")
    # No thoughts_log wired; no file should exist in tmp_path.
    assert list(tmp_path.iterdir()) == []
    assert session.thoughts_log is None


def test_parent_dir_is_created(tmp_path: Path) -> None:
    state = load_scenario("01_tiny_skirmish")
    nested = tmp_path / "a" / "b" / "thoughts.log"
    session = new_session(state, thoughts_log_path=nested)
    try:
        session.add_thought(Team.BLUE, "hi")
    finally:
        assert session.thoughts_log is not None
        session.thoughts_log.close()
    assert nested.exists()
