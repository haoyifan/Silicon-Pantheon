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

from ..engine.state import Team
from ..session import Session

# Re-export shared helpers so external code can keep importing from here.
from ._common import ToolError  # noqa: F401

# Sub-module implementations.
from .read_only import (
    get_state,
    get_unit,
    get_unit_range,
    get_legal_actions,
    simulate_attack,
    get_threat_map,
    get_tactical_summary,
    get_history,
)
from .mutations import (
    move,
    attack,
    heal,
    wait_unit,
    end_turn,
)
from .coach import get_match_telemetry, report_tokens, send_to_agent

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
    "get_unit_range": {
        "fn": get_unit_range,
        "description": (
            "Full threat zone for a unit: tiles it can move to (BFS "
            "reachable) + tiles it can attack from any reachable "
            "position (the outer threat ring). Works for any alive "
            "unit, own or enemy. Units with status=done return empty."
        ),
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
    "get_tactical_summary": {
        "fn": get_tactical_summary,
        "description": (
            "Precomputed 'what's worth doing this turn' digest: attack "
            "opportunities your units can execute from current positions "
            "(with predicted damage / counter / kill outcomes), threats "
            "against your units from currently-visible enemies, and the "
            "list of your units still in MOVED status pending action. "
            "Call once per turn-start instead of many simulate_attack / "
            "get_threat_map calls."
        ),
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
    "report_tokens": {
        "fn": report_tokens,
        "description": "Report token usage to the server for post-game telemetry.",
        "input_schema": {
            "type": "object",
            "properties": {
                "tokens": {"type": "integer"},
            },
            "required": ["tokens"],
        },
    },
    "get_match_telemetry": {
        "fn": get_match_telemetry,
        "description": "Get server-tracked telemetry for both teams (turn times, tool calls, tokens).",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
}


def call_tool(session: Session, viewer: Team, name: str, args: dict) -> dict:
    """Dispatch a tool call by name. Raises ToolError on unknown tool / bad args."""
    spec = TOOL_REGISTRY.get(name)
    if spec is None:
        raise ToolError(f"unknown tool: {name}")
    session.tool_calls_by_team[viewer] += 1
    fn: Tool = spec["fn"]
    try:
        return fn(session, viewer, **args)
    except TypeError as e:
        session.tool_errors_by_team[viewer] += 1
        raise ToolError(f"bad arguments for {name}: {e}") from e
    except ToolError:
        session.tool_errors_by_team[viewer] += 1
        raise
