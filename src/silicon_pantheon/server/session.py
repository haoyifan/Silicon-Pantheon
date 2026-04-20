"""A Session bundles the authoritative GameState, coach message queues, and
the replay writer for one match. Tools operate on a Session.
"""

from __future__ import annotations

import threading
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

    Thread-safe: `write()` and `close()` are serialised via an internal
    lock. Leaf in the server's lock hierarchy — see docs/THREADING.md.
    """

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh: TextIO = open(self.path, "a", encoding="utf-8", buffering=1)
        self._lock = threading.Lock()

    def write(self, thought: "AgentThought") -> None:
        collapsed = " ".join(thought.text.split())
        line = f"[T{thought.turn} {thought.team.value}] {collapsed}\n"
        with self._lock:
            self._fh.write(line)
            self._fh.flush()

    def close(self) -> None:
        with self._lock:
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
    # Per-team telemetry for post-game stats.
    tool_calls_by_team: dict[Team, int] = field(
        default_factory=lambda: {Team.BLUE: 0, Team.RED: 0}
    )
    tool_errors_by_team: dict[Team, int] = field(
        default_factory=lambda: {Team.BLUE: 0, Team.RED: 0}
    )
    turn_start_time: float = 0.0  # monotonic timestamp of current turn start
    turn_times_by_team: dict[Team, list[float]] = field(
        default_factory=lambda: {Team.BLUE: [], Team.RED: []}
    )
    tokens_by_team: dict[Team, int] = field(
        default_factory=lambda: {Team.BLUE: 0, Team.RED: 0}
    )
    damage_dealt_by_team: dict[Team, int] = field(
        default_factory=lambda: {Team.BLUE: 0, Team.RED: 0}
    )
    damage_taken_by_team: dict[Team, int] = field(
        default_factory=lambda: {Team.BLUE: 0, Team.RED: 0}
    )
    kills_by_team: dict[Team, int] = field(
        default_factory=lambda: {Team.BLUE: 0, Team.RED: 0}
    )
    # Wall-clock timestamp when the match actually started (set by
    # log_match_players). Used for match_duration_s in leaderboard.
    match_start_time: float = 0.0
    # Serialises concurrent tool dispatches for the same game session.
    lock: threading.Lock = field(default_factory=threading.Lock)
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

    def log_match_players(self, players: dict[str, dict]) -> None:
        """Write a match_players event with per-team player info.

        Called by start_game_for_room once the slot→team mapping is
        known. ``players`` maps team name → {display_name, kind,
        provider, model}.
        """
        import time as _time
        self.match_start_time = _time.time()
        self.log("match_players", {"players": players})

    def log_match_end(self) -> None:
        """Write a match_end event with outcome stats."""
        import time as _time
        state = self.state
        payload: dict = {
            "winner": state.winner.value if state.winner else None,
            "turns_played": state.turn,
            "max_turns": state.max_turns,
            "ended_at": _time.time(),
        }
        # Include last_action for reason
        if state.last_action and isinstance(state.last_action, dict):
            payload["reason"] = state.last_action.get("reason")
        self.log("match_end", payload)

    def notify_action(self, result: dict) -> None:
        for hook in self.action_hooks:
            try:
                hook(self, result)
            except Exception:
                # Never let a hook break the game loop.
                pass

    def add_thought(self, team: Team, text: str, *, turn: int | None = None) -> None:
        """Append an agent-thought event to the session log.

        ── Locking ──
        Acquires ``self.lock`` for the full body because this touches
        ``self.state.turn``, ``self.thoughts``, ``self.replay`` (via
        ``self.log``), ``self.thoughts_log``, and fires hooks via
        ``notify_action``. All server callers that previously drove
        add_thought without holding session.lock — the networked
        record_thought MCP tool is the only one — are tested to
        *not* be holding it (Session.lock is non-reentrant).

        In-process harness callers (e.g. anthropic.py) also don't
        hold session.lock when calling this.
        """
        text = text.strip()
        if not text:
            return
        with self.lock:
            # Allow the caller to pin the turn number. The Claude SDK
            # sometimes streams trailing AssistantMessage text AFTER
            # the agent's end_turn tool call has flipped
            # state.active_player and bumped state.turn, which would
            # otherwise cause that trailing reasoning to be tagged
            # with the next turn number.
            effective_turn = turn if turn is not None else self.state.turn
            thought = AgentThought(turn=effective_turn, team=team, text=text)
            self.thoughts.append(thought)
            self.log(
                "agent_thought",
                {"team": team.value, "text": text, "turn": effective_turn},
            )
            if self.thoughts_log is not None:
                try:
                    self.thoughts_log.write(thought)
                except Exception:
                    # Never let a log failure break the match loop.
                    pass
            # Fire action hooks so the TUI refreshes as reasoning
            # arrives, not only on tool calls. Per THREADING.md, hooks
            # fire under session.lock — consistent with _record_action
            # + _force_end_turn.
            self.notify_action(
                {"kind": "agent_thought", "team": team.value, "text": text}
            )


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
        import time as _time
        payload: dict = {
            "scenario": scenario,
            "max_turns": state.max_turns,
            "first_player": state.first_player.value,
            "started_at": _time.time(),
        }
        # Player info is injected by start_game_for_room after
        # new_session returns (it has access to the room's seats).
        # The extra fields are written via session.log_match_players().
        session.log("match_start", payload)
    return session
