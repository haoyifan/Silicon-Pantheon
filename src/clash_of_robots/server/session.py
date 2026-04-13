"""A Session bundles the authoritative GameState, coach message queues, and
the replay writer for one match. Tools operate on a Session.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO

from .engine.replay import ReplayWriter
from .engine.state import GameState, Pos, Team

ActionHook = Callable[["Session", dict], None]

THOUGHT_BUFFER_SIZE = 100


class ThoughtsLogWriter:
    """Append-only, line-oriented plain-text log of agent reasoning.

    Meant to be tailed live with `less +F <path>` during a match. One
    thought per line, whitespace collapsed, tagged with turn + team so
    each line is self-describing. `flush()` after every write so the
    pager sees updates with no buffering lag.
    """

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh: TextIO = open(self.path, "a", encoding="utf-8", buffering=1)

    def write(self, thought: "AgentThought") -> None:
        collapsed = " ".join(thought.text.split())
        self._fh.write(f"[T{thought.turn} {thought.team.value}] {collapsed}\n")
        self._fh.flush()

    def close(self) -> None:
        try:
            self._fh.close()
        except Exception:
            pass


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
    # Name of the scenario being played (matches the games/ folder name).
    # Used by lesson-aware providers to scope which prior lessons to inject.
    scenario: str | None = None
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
    # Optional live plain-text log of thoughts (tailable with `less +F`).
    thoughts_log: ThoughtsLogWriter | None = None
    # Fog-of-war mode for this session. "none" = no filtering.
    fog_of_war: str = "none"
    # Per-team memory of tiles ever seen (classic mode). Kept frozen
    # so consumers can share references without fear of mutation.
    ever_seen: dict[Team, frozenset[Pos]] = field(
        default_factory=lambda: {Team.BLUE: frozenset(), Team.RED: frozenset()}
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

    def add_thought(self, team: Team, text: str, *, turn: int | None = None) -> None:
        text = text.strip()
        if not text:
            return
        # Allow the caller to pin the turn number. The Claude SDK sometimes
        # streams trailing AssistantMessage text AFTER the agent's end_turn
        # tool call has flipped state.active_player and bumped state.turn,
        # which would otherwise cause that trailing reasoning to be tagged
        # with the next turn number.
        effective_turn = turn if turn is not None else self.state.turn
        thought = AgentThought(turn=effective_turn, team=team, text=text)
        self.thoughts.append(thought)
        self.log("agent_thought", {"team": team.value, "text": text, "turn": effective_turn})
        if self.thoughts_log is not None:
            try:
                self.thoughts_log.write(thought)
            except Exception:
                # Never let a log failure break the match loop.
                pass
        # Fire action hooks so the TUI refreshes as reasoning arrives,
        # not only on tool calls. rich.Live's auto-refresh thread only
        # repaints whatever renderable was last passed to Live.update();
        # without this, thoughts arriving between tool calls would
        # only appear in the panel after the NEXT tool call. Flicker
        # is not a concern here because the TUI uses screen=True
        # (alternate screen buffer) and rich.Live throttles repaints
        # to its configured frame rate regardless of update() frequency.
        self.notify_action({"kind": "agent_thought", "team": team.value, "text": text})


def new_session(
    state: GameState,
    replay_path: str | Path | None = None,
    *,
    scenario: str | None = None,
    thoughts_log_path: str | Path | None = None,
    fog_of_war: str = "none",
) -> Session:
    writer = ReplayWriter(replay_path) if replay_path else None
    thoughts_log = ThoughtsLogWriter(thoughts_log_path) if thoughts_log_path else None
    session = Session(
        state=state,
        replay=writer,
        scenario=scenario,
        thoughts_log=thoughts_log,
        fog_of_war=fog_of_war,
    )
    # Write a single metadata line at the top of the replay so downstream
    # tools (interactive replayer, analytics) can reconstruct the match
    # without any out-of-band knowledge (folder name, CLI flags, etc.).
    if writer is not None:
        session.log(
            "match_start",
            {
                "scenario": scenario,
                "max_turns": state.max_turns,
                "first_player": state.first_player.value,
            },
        )
    return session
