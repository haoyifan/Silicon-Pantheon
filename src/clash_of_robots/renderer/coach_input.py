"""File-based coach input.

The user writes advice to a text file during the match; the orchestrator polls
the file between turns and pushes any new content into the target team's coach
message queue.

Usage:
    coach = CoachFileWatcher(path="coach_blue.txt", team=Team.BLUE)
    # Call coach.poll(session) between turns
"""

from __future__ import annotations

from pathlib import Path

from clash_of_robots.server.engine.state import Team
from clash_of_robots.server.session import CoachMessage, Session


class CoachFileWatcher:
    def __init__(self, path: str | Path, team: Team):
        self.path = Path(path)
        self.team = team
        self._last_size = 0
        # If the file doesn't exist, create it so the user has somewhere to write.
        if not self.path.exists():
            self.path.write_text("", encoding="utf-8")
        self._last_size = self.path.stat().st_size

    def poll(self, session: Session) -> list[str]:
        """Read any new content since last poll, push as coach messages, return them."""
        if not self.path.exists():
            return []
        size = self.path.stat().st_size
        if size <= self._last_size:
            return []
        with self.path.open("r", encoding="utf-8") as f:
            f.seek(self._last_size)
            new_text = f.read()
        self._last_size = size
        new_msgs = [line.strip() for line in new_text.splitlines() if line.strip()]
        for line in new_msgs:
            session.coach_queues[self.team].append(CoachMessage(turn=session.state.turn, text=line))
            session.log(
                "coach_message",
                {"to": self.team.value, "text": line, "turn": session.state.turn},
            )
        return new_msgs
