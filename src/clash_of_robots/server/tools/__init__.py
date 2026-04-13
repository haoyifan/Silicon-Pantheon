"""In-process tool implementations. Each tool operates on a Session.

The MCP server (`server/main.py`) wraps these for remote use; harnesses call
them directly for in-process play.

Each tool:
- takes `(session, viewer: Team, **args)` and returns a JSON-serializable dict
- raises `ToolError` on rule violations (maps to an MCP error or an error dict)
- is registered in TOOL_REGISTRY with its JSON schema
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..engine.board import in_attack_range, tiles_in_attack_range
from ..engine.combat import predict_attack
from ..engine.rules import (
    AttackAction,
    EndTurnAction,
    HealAction,
    IllegalAction,
    MoveAction,
    WaitAction,
    apply,
    legal_actions_for_unit,
)
from ..engine.serialize import state_to_dict
from ..engine.state import GameState, Pos, Team, UnitStatus
from ..session import CoachMessage, Session


class ToolError(Exception):
    """Raised when a tool call cannot be fulfilled. The error message is
    returned to the agent so it can self-correct.
    """


# ---- helpers ----


def _require_active(session: Session, viewer: Team) -> None:
    if session.state.active_player is not viewer:
        raise ToolError(
            f"not your turn (active: {session.state.active_player.value}, you: {viewer.value})"
        )


def _require_own_unit(state: GameState, unit_id: str, viewer: Team) -> None:
    u = state.units.get(unit_id)
    if u is None or not u.alive:
        raise ToolError(f"unit {unit_id} does not exist or is dead")
    if u.owner is not viewer:
        raise ToolError(f"unit {unit_id} is not yours (owner={u.owner.value})")


# ---- read-only tools ----


def get_state(session: Session, viewer: Team) -> dict:
    return state_to_dict(session.state, viewer=viewer)


def get_unit(session: Session, viewer: Team, unit_id: str) -> dict:
    u = session.state.units.get(unit_id)
    if u is None or not u.alive:
        raise ToolError(f"unit {unit_id} does not exist or is dead")
    return {
        "id": u.id,
        "owner": u.owner.value,
        "class": u.class_.value,
        "pos": u.pos.to_dict(),
        "hp": u.hp,
        "hp_max": u.stats.hp_max,
        "atk": u.stats.atk,
        "def": u.stats.defense,
        "res": u.stats.res,
        "spd": u.stats.spd,
        "rng": [u.stats.rng_min, u.stats.rng_max],
        "move": u.stats.move,
        "is_magic": u.stats.is_magic,
        "can_heal": u.stats.can_heal,
        "status": u.status.value,
    }


def get_legal_actions(session: Session, viewer: Team, unit_id: str) -> dict:
    _require_active(session, viewer)
    _require_own_unit(session.state, unit_id, viewer)
    try:
        return legal_actions_for_unit(session.state, unit_id)
    except IllegalAction as e:
        raise ToolError(str(e)) from e


def simulate_attack(
    session: Session,
    viewer: Team,
    attacker_id: str,
    target_id: str,
    from_tile: dict | None = None,
) -> dict:
    state = session.state
    attacker = state.units.get(attacker_id)
    target = state.units.get(target_id)
    if attacker is None or not attacker.alive:
        raise ToolError(f"attacker {attacker_id} does not exist or is dead")
    if target is None or not target.alive:
        raise ToolError(f"target {target_id} does not exist or is dead")
    if attacker.owner is target.owner:
        raise ToolError("attacker and target are on the same team")

    origin = Pos.from_dict(from_tile) if from_tile else attacker.pos
    if not in_attack_range(origin, target.pos, attacker.stats):
        raise ToolError(f"target is not in attack range from {origin.to_dict()}")

    pred = predict_attack(
        attacker,
        target,
        attacker_tile=state.board.tile(origin),
        defender_tile=state.board.tile(target.pos),
        attacker_pos=origin,
    )
    return {
        "attacker_id": attacker_id,
        "target_id": target_id,
        "from": origin.to_dict(),
        "damage_per_hit": pred.damage_per_hit,
        "attacker_hits": pred.attacker_hits,
        "total_damage_to_defender": pred.total_damage_to_defender,
        "defender_dies": pred.defender_dies,
        "will_counter": pred.will_counter,
        "counter_damage_per_hit": pred.counter_damage_per_hit,
        "counter_hits": pred.counter_hits,
        "total_counter_damage": pred.total_counter_damage,
        "attacker_dies": pred.attacker_dies,
    }


def get_threat_map(session: Session, viewer: Team) -> dict:
    """For each tile, which enemy units could attack a unit standing there."""
    state = session.state
    enemy = viewer.other()
    threats: dict[str, list[str]] = {}
    for eu in state.units_of(enemy):
        for p in tiles_in_attack_range(eu.pos, eu.stats, state.board):
            key = f"{p.x},{p.y}"
            threats.setdefault(key, []).append(eu.id)
    return {"threats": threats}


def get_history(session: Session, viewer: Team, last_n: int = 10) -> dict:
    hist = session.state.history[-last_n:] if last_n > 0 else []
    return {
        "history": hist,
        "last_action": session.state.last_action,
        "turn": session.state.turn,
        "active_player": session.state.active_player.value,
    }


def get_coach_messages(session: Session, viewer: Team, since_turn: int = 0) -> dict:
    queue = session.coach_queues[viewer]
    msgs = [{"turn": m.turn, "text": m.text} for m in queue if m.turn >= since_turn]
    # Clear once read.
    session.coach_queues[viewer] = []
    return {"messages": msgs}


# ---- write tools ----


def _record_action(session: Session, result: dict) -> None:
    session.state.last_action = result
    session.state.history.append(result)
    session.log("action", result)
    session.notify_action(result)


def move(session: Session, viewer: Team, unit_id: str, dest: dict) -> dict:
    _require_active(session, viewer)
    _require_own_unit(session.state, unit_id, viewer)
    try:
        result = apply(session.state, MoveAction(unit_id=unit_id, dest=Pos.from_dict(dest)))
    except IllegalAction as e:
        raise ToolError(str(e)) from e
    _record_action(session, result)
    return result


def attack(session: Session, viewer: Team, unit_id: str, target_id: str) -> dict:
    _require_active(session, viewer)
    _require_own_unit(session.state, unit_id, viewer)
    try:
        result = apply(session.state, AttackAction(unit_id=unit_id, target_id=target_id))
    except IllegalAction as e:
        raise ToolError(str(e)) from e
    _record_action(session, result)
    return result


def heal(session: Session, viewer: Team, healer_id: str, target_id: str) -> dict:
    _require_active(session, viewer)
    _require_own_unit(session.state, healer_id, viewer)
    try:
        result = apply(session.state, HealAction(healer_id=healer_id, target_id=target_id))
    except IllegalAction as e:
        raise ToolError(str(e)) from e
    _record_action(session, result)
    return result


def wait_unit(session: Session, viewer: Team, unit_id: str) -> dict:
    _require_active(session, viewer)
    _require_own_unit(session.state, unit_id, viewer)
    try:
        result = apply(session.state, WaitAction(unit_id=unit_id))
    except IllegalAction as e:
        raise ToolError(str(e)) from e
    _record_action(session, result)
    return result


def end_turn(session: Session, viewer: Team) -> dict:
    _require_active(session, viewer)
    for u in session.state.units_of(viewer):
        if u.status is UnitStatus.MOVED:
            raise ToolError(
                f"unit {u.id} moved but has not acted; call attack/heal/wait before end_turn"
            )
    try:
        result = apply(session.state, EndTurnAction())
    except IllegalAction as e:
        raise ToolError(str(e)) from e
    _record_action(session, result)
    return result


# ---- coach channel ----


def send_to_agent(session: Session, viewer: Team, team: str, text: str) -> dict:
    target = Team(team)
    session.coach_queues[target].append(CoachMessage(turn=session.state.turn, text=text))
    session.log("coach_message", {"to": target.value, "text": text, "turn": session.state.turn})
    return {"ok": True, "queued_for": target.value, "turn": session.state.turn}


# ---- registry ----

Tool = Callable[..., dict]

TOOL_REGISTRY: dict[str, dict[str, Any]] = {
    "get_state": {
        "fn": get_state,
        "description": "Get the current full game state visible to you.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    "get_unit": {
        "fn": get_unit,
        "description": "Get a single unit's details by id.",
        "input_schema": {
            "type": "object",
            "properties": {"unit_id": {"type": "string"}},
            "required": ["unit_id"],
        },
    },
    "get_legal_actions": {
        "fn": get_legal_actions,
        "description": "Get the legal moves/attacks/heals/wait for one of your units.",
        "input_schema": {
            "type": "object",
            "properties": {"unit_id": {"type": "string"}},
            "required": ["unit_id"],
        },
    },
    "simulate_attack": {
        "fn": simulate_attack,
        "description": "Predict outcome of attacker_id attacking target_id (optionally from a given tile). Does not modify state.",
        "input_schema": {
            "type": "object",
            "properties": {
                "attacker_id": {"type": "string"},
                "target_id": {"type": "string"},
                "from_tile": {
                    "type": "object",
                    "properties": {"x": {"type": "integer"}, "y": {"type": "integer"}},
                    "required": ["x", "y"],
                },
            },
            "required": ["attacker_id", "target_id"],
        },
    },
    "get_threat_map": {
        "fn": get_threat_map,
        "description": "For each tile, which enemy units could attack a unit standing there. Returns {threats: {'x,y': [unit_id,...]}}.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    "get_history": {
        "fn": get_history,
        "description": "Get recent action history.",
        "input_schema": {
            "type": "object",
            "properties": {"last_n": {"type": "integer", "default": 10}},
            "required": [],
        },
    },
    "get_coach_messages": {
        "fn": get_coach_messages,
        "description": "Retrieve unread coach messages for your team. Drains the queue on read.",
        "input_schema": {
            "type": "object",
            "properties": {"since_turn": {"type": "integer", "default": 0}},
            "required": [],
        },
    },
    "move": {
        "fn": move,
        "description": "Move one of your units to a destination tile. Unit must be in 'ready' status.",
        "input_schema": {
            "type": "object",
            "properties": {
                "unit_id": {"type": "string"},
                "dest": {
                    "type": "object",
                    "properties": {"x": {"type": "integer"}, "y": {"type": "integer"}},
                    "required": ["x", "y"],
                },
            },
            "required": ["unit_id", "dest"],
        },
    },
    "attack": {
        "fn": attack,
        "description": "Attack an enemy unit from your current position. Resolves combat immediately including counter-attack. Unit becomes 'done'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "unit_id": {"type": "string"},
                "target_id": {"type": "string"},
            },
            "required": ["unit_id", "target_id"],
        },
    },
    "heal": {
        "fn": heal,
        "description": "Heal an adjacent ally (Mage only). Unit becomes 'done'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "healer_id": {"type": "string"},
                "target_id": {"type": "string"},
            },
            "required": ["healer_id", "target_id"],
        },
    },
    "wait": {
        "fn": wait_unit,
        "description": "End this unit's turn without attacking or healing. Unit becomes 'done'.",
        "input_schema": {
            "type": "object",
            "properties": {"unit_id": {"type": "string"}},
            "required": ["unit_id"],
        },
    },
    "end_turn": {
        "fn": end_turn,
        "description": "Pass control to the opponent. Rejects if any unit is mid-action (moved but not acted).",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    "send_to_agent": {
        "fn": send_to_agent,
        "description": "(Coach tool) send a message to a team's agent, delivered at start of their next turn.",
        "input_schema": {
            "type": "object",
            "properties": {
                "team": {"type": "string", "enum": ["blue", "red"]},
                "text": {"type": "string"},
            },
            "required": ["team", "text"],
        },
    },
}


def call_tool(session: Session, viewer: Team, name: str, args: dict) -> dict:
    """Dispatch a tool call by name. Raises ToolError on unknown tool / bad args."""
    spec = TOOL_REGISTRY.get(name)
    if spec is None:
        raise ToolError(f"unknown tool: {name}")
    fn: Tool = spec["fn"]
    try:
        return fn(session, viewer, **args)
    except TypeError as e:
        raise ToolError(f"bad arguments for {name}: {e}") from e
