"""Coach channel tests."""

from __future__ import annotations

from pathlib import Path

from clash_of_robots.renderer.coach_input import CoachFileWatcher
from clash_of_robots.server.engine.scenarios import load_scenario
from clash_of_robots.server.engine.state import Team
from clash_of_robots.server.session import new_session
from clash_of_robots.server.tools import call_tool


def test_file_watcher_pushes_new_lines(tmp_path: Path):
    session = new_session(load_scenario("01_tiny_skirmish"))
    f = tmp_path / "coach.txt"
    f.write_text("")
    watcher = CoachFileWatcher(f, Team.BLUE)

    # No new content -> no messages
    assert watcher.poll(session) == []

    # Append one line
    with f.open("a") as fh:
        fh.write("push the knight forward\n")
    msgs = watcher.poll(session)
    assert msgs == ["push the knight forward"]

    # Blue agent retrieves via tool
    out = call_tool(session, Team.BLUE, "get_coach_messages", {})
    assert [m["text"] for m in out["messages"]] == ["push the knight forward"]


def test_file_watcher_handles_multiple_appends(tmp_path: Path):
    session = new_session(load_scenario("01_tiny_skirmish"))
    f = tmp_path / "coach.txt"
    watcher = CoachFileWatcher(f, Team.RED)
    with f.open("a") as fh:
        fh.write("hold the fort\n")
    watcher.poll(session)
    with f.open("a") as fh:
        fh.write("send mage to heal\n")
        fh.write("pull cavalry back\n")
    msgs = watcher.poll(session)
    assert msgs == ["send mage to heal", "pull cavalry back"]
