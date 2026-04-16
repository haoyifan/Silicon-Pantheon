"""Coach channel tests."""

from __future__ import annotations

from pathlib import Path

from silicon_pantheon.renderer.coach_input import CoachFileWatcher
from silicon_pantheon.server.engine.scenarios import load_scenario
from silicon_pantheon.server.engine.state import Team
from silicon_pantheon.server.session import new_session
from silicon_pantheon.server.tools import call_tool


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

    # Blue agent retrieves via the auto-delivered coach_messages
    # field on get_tactical_summary (the dedicated get_coach_messages
    # tool was removed in favor of proactive delivery).
    out = call_tool(session, Team.BLUE, "get_tactical_summary", {})
    assert [m["text"] for m in out["coach_messages"]] == ["push the knight forward"]


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
