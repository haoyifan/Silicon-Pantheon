"""A Session bundles the authoritative GameState, coach message queues, and
the replay writer for one match. Tools operate on a Session.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .engine.replay import ReplayWriter
from .engine.state import GameState, Team

ActionHook = Callable[["Session", dict], None]


@dataclass
class CoachMessage:
    turn: int
    text: str


@dataclass
class Session:
    state: GameState
    replay: ReplayWriter | None = None
    # coach message queues per team (messages waiting to be read by that team's agent)
    coach_queues: dict[Team, list[CoachMessage]] = field(
        default_factory=lambda: {Team.BLUE: [], Team.RED: []}
    )
    # Hooks called after each action mutates state. Used by the renderer to
    # refresh the UI in real time as the agent calls tools.
    action_hooks: list[ActionHook] = field(default_factory=list)

    def log(self, kind: str, payload: dict) -> None:
        if self.replay is not None:
            self.replay.write({"kind": kind, "payload": payload, "turn": self.state.turn})

    def notify_action(self, result: dict) -> None:
        for hook in self.action_hooks:
            try:
                hook(self, result)
            except Exception:
                # Never let a hook break the game loop.
                pass


def new_session(state: GameState, replay_path: str | Path | None = None) -> Session:
    writer = ReplayWriter(replay_path) if replay_path else None
    return Session(state=state, replay=writer)
