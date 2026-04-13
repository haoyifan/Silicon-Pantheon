"""Structured schema for replay.jsonl events.

The engine writes events as free-form JSON dicts (via Session.log). This
module defines the typed shapes for each `kind` and a parser that turns a
raw dict into a typed ReplayEvent, plus `action_from_payload` which
reconstructs an engine Action object so consumers (the interactive
replayer) can re-apply it to a fresh GameState.

Event kinds currently written by the engine/harness:

- `match_start`        {scenario, max_turns, first_player}
- `action`             result dict of apply() (type=move|attack|heal|wait|end_turn)
- `agent_thought`      {team, text, turn}
- `coach_message`      {to, text, turn}
- `forced_end_turn`    {team}
- `agent_error`        {team, error}
- `summarize_error`    {team, error}
- `lessons_load_error` {error}

Any unrecognized kind parses into a ReplayEvent with `kind='<unknown>'`
so consumers can still show or skip it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from clash_of_robots.server.engine.rules import (
    Action,
    AttackAction,
    EndTurnAction,
    HealAction,
    MoveAction,
    WaitAction,
)
from clash_of_robots.server.engine.state import Pos


# ---- typed payloads ----


@dataclass(frozen=True)
class MatchStart:
    scenario: str | None
    max_turns: int
    first_player: str  # "blue" | "red"


@dataclass(frozen=True)
class AgentThought:
    team: str  # "blue" | "red"
    text: str


@dataclass(frozen=True)
class CoachMessage:
    to: str  # "blue" | "red"
    text: str


@dataclass(frozen=True)
class ForcedEndTurn:
    team: str


@dataclass(frozen=True)
class ErrorPayload:
    # Shared shape for agent_error / summarize_error / lessons_load_error.
    team: str | None
    error: str


# ---- envelope ----


@dataclass(frozen=True)
class ReplayEvent:
    kind: str
    turn: int
    # Structured payload for kinds we recognize; raw dict otherwise so
    # unknown/future kinds still round-trip through the parser.
    data: MatchStart | AgentThought | CoachMessage | ForcedEndTurn | ErrorPayload | dict[str, Any]


# ---- parsing ----


def parse_event(raw: dict[str, Any]) -> ReplayEvent:
    """Turn one replay JSON line (already decoded) into a ReplayEvent."""
    kind = str(raw.get("kind", "unknown"))
    turn = int(raw.get("turn", 0) or 0)
    payload = raw.get("payload") or {}
    if not isinstance(payload, dict):
        payload = {"value": payload}

    data: Any
    if kind == "match_start":
        data = MatchStart(
            scenario=payload.get("scenario"),
            max_turns=int(payload.get("max_turns", 0) or 0),
            first_player=str(payload.get("first_player", "blue")),
        )
    elif kind == "agent_thought":
        data = AgentThought(
            team=str(payload.get("team", "")),
            text=str(payload.get("text", "")),
        )
    elif kind == "coach_message":
        data = CoachMessage(
            to=str(payload.get("to", "")),
            text=str(payload.get("text", "")),
        )
    elif kind == "forced_end_turn":
        data = ForcedEndTurn(team=str(payload.get("team", "")))
    elif kind in {"agent_error", "summarize_error", "lessons_load_error"}:
        data = ErrorPayload(
            team=(str(payload["team"]) if "team" in payload else None),
            error=str(payload.get("error", "")),
        )
    elif kind == "action":
        # The action payload is the engine's result dict. We keep it as
        # a dict so consumers can both display it AND reconstruct an
        # engine Action via `action_from_payload`.
        data = payload
    else:
        data = payload

    return ReplayEvent(kind=kind, turn=turn, data=data)


# ---- action reconstruction ----


class UnreconstructibleAction(ValueError):
    """Raised when an action result dict cannot be turned back into an Action."""


def action_from_payload(payload: dict[str, Any]) -> Action:
    """Reconstruct an engine Action from a logged `action` event payload.

    The payload is the result dict that the engine wrote when the action
    was originally applied; it carries enough identity (unit ids, targets,
    destination tile) to re-apply via engine.apply() on a fresh state.
    """
    t = str(payload.get("type", ""))
    if t == "move":
        dest = payload.get("dest") or payload.get("to") or {}
        if "x" not in dest or "y" not in dest:
            raise UnreconstructibleAction(f"move missing dest: {payload!r}")
        return MoveAction(
            unit_id=str(payload["unit_id"]),
            dest=Pos(int(dest["x"]), int(dest["y"])),
        )
    if t == "attack":
        return AttackAction(
            unit_id=str(payload["unit_id"]),
            target_id=str(payload["target_id"]),
        )
    if t == "heal":
        return HealAction(
            healer_id=str(payload["healer_id"]),
            target_id=str(payload["target_id"]),
        )
    if t == "wait":
        return WaitAction(unit_id=str(payload["unit_id"]))
    if t == "end_turn":
        return EndTurnAction()
    raise UnreconstructibleAction(f"unknown action type: {t!r}")
