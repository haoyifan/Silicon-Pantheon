"""A Session bundles the authoritative GameState, coach message queues, and
the replay writer for one match. Tools operate on a Session.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .engine.replay import ReplayWriter
from .engine.state import GameState, Team

ActionHook = Callable[["Session", dict], None]

THOUGHT_BUFFER_SIZE = 100


@dataclass
class CoachMessage:
    turn: int
    text: str


@dataclass
class AgentThought:
    turn: int
    team: Team
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
    # Rolling buffer of agent reasoning text emitted between tool calls.
    thoughts: deque[AgentThought] = field(
        default_factory=lambda: deque(maxlen=THOUGHT_BUFFER_SIZE)
    )

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

    def add_thought(self, team: Team, text: str) -> None:
        text = text.strip()
        if not text:
            return
        self.thoughts.append(AgentThought(turn=self.state.turn, team=team, text=text))
        self.log("agent_thought", {"team": team.value, "text": text})
        # Reuse action hooks so the TUI refreshes in real time.
        self.notify_action({"kind": "agent_thought", "team": team.value, "text": text})


def new_session(state: GameState, replay_path: str | Path | None = None) -> Session:
    writer = ReplayWriter(replay_path) if replay_path else None
    return Session(state=state, replay=writer)
