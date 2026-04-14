"""NetworkedAgent — provider-agnostic orchestrator for one player.

Composition: one `ProviderAdapter` (picked from the user's
credentials, defaulting to Anthropic) + the remote `ServerClient` +
the local scenario + strategy + lesson context. Every
per-turn interaction — building the turn prompt, dispatching tool
calls back through the server, surfacing reasoning — routes through
this class; the concrete LLM never leaks into the TUI.

Persistent-session default: the adapter opens its session on the
first `play_turn` and reuses it until `close()`, so the agent keeps
its chain-of-thought across turns within a single match.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from clash_of_odin.client.providers import (
    ProviderAdapter,
    ThoughtCallback,
    ToolSpec,
)
from clash_of_odin.client.transport import ServerClient
from clash_of_odin.harness.prompts import (
    build_system_prompt,
    build_turn_prompt_from_state_dict,
)
from clash_of_odin.lessons import Lesson, LessonStore
from clash_of_odin.server.engine.state import Team

# Canonical list of game-side tools exposed to the agent. Each entry:
# (name, description, JSON-schema input). connection_id is injected by
# ServerClient.call so it's not part of the schema the agent sees.
GAME_TOOLS: list[ToolSpec] = [
    ToolSpec(
        "get_state",
        "Get the current game state visible to you (fog-of-war filtered).",
        {"type": "object", "properties": {}, "required": []},
    ),
    ToolSpec(
        "get_unit",
        "Get a single unit's details by id.",
        {
            "type": "object",
            "properties": {"unit_id": {"type": "string"}},
            "required": ["unit_id"],
        },
    ),
    ToolSpec(
        "get_legal_actions",
        "Get the legal moves/attacks/heals/wait for one of your units.",
        {
            "type": "object",
            "properties": {"unit_id": {"type": "string"}},
            "required": ["unit_id"],
        },
    ),
    ToolSpec(
        "simulate_attack",
        "Predict attack outcome without mutating state.",
        {
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
    ),
    ToolSpec(
        "get_threat_map",
        "For each tile, which visible enemy units can attack you there.",
        {"type": "object", "properties": {}, "required": []},
    ),
    ToolSpec(
        "get_history",
        "Recent action history.",
        {
            "type": "object",
            "properties": {"last_n": {"type": "integer", "default": 10}},
            "required": [],
        },
    ),
    ToolSpec(
        "get_coach_messages",
        "Drain unread coach messages for your team.",
        {
            "type": "object",
            "properties": {"since_turn": {"type": "integer", "default": 0}},
            "required": [],
        },
    ),
    ToolSpec(
        "move",
        "Move one of your ready units to a destination tile.",
        {
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
    ),
    ToolSpec(
        "attack",
        "Attack an enemy unit from your current position.",
        {
            "type": "object",
            "properties": {
                "unit_id": {"type": "string"},
                "target_id": {"type": "string"},
            },
            "required": ["unit_id", "target_id"],
        },
    ),
    ToolSpec(
        "heal",
        "Heal an adjacent ally (Mage only).",
        {
            "type": "object",
            "properties": {
                "healer_id": {"type": "string"},
                "target_id": {"type": "string"},
            },
            "required": ["healer_id", "target_id"],
        },
    ),
    ToolSpec(
        "wait",
        "End this unit's turn without attacking or healing.",
        {
            "type": "object",
            "properties": {"unit_id": {"type": "string"}},
            "required": ["unit_id"],
        },
    ),
    ToolSpec(
        "end_turn",
        "Pass control to the opponent. Must be called to end your turn.",
        {"type": "object", "properties": {}, "required": []},
    ),
]


def _build_default_adapter(model: str) -> ProviderAdapter:
    """Factory for the default (Anthropic) adapter.

    Other providers slot in as alternatives; selection happens in
    NetworkedAgent based on the model id prefix / the user's
    credentials. A proper catalog + registry comes in a later commit.
    """
    from clash_of_odin.client.providers.anthropic import AnthropicAdapter

    return AnthropicAdapter(model=model)


class NetworkedAgent:
    """Drives one player's turns against a remote clash-serve."""

    def __init__(
        self,
        client: ServerClient,
        *,
        model: str,
        scenario: str,
        strategy: str | None = None,
        lessons_dir: Path | None = Path("lessons"),
        thoughts_callback: ThoughtCallback | None = None,
        time_budget_s: float = 90.0,
        adapter: ProviderAdapter | None = None,
    ):
        self.client = client
        self.model = model
        self.scenario = scenario
        self.strategy = strategy
        self.lessons_dir = lessons_dir
        self.thoughts_callback = thoughts_callback
        self.time_budget_s = time_budget_s
        self.adapter: ProviderAdapter = adapter or _build_default_adapter(model)

    async def close(self) -> None:
        try:
            await self.adapter.close()
        except Exception:
            pass

    # ---- tool dispatch ----

    async def _dispatch_tool(self, name: str, args: dict) -> dict:
        """Forward a tool call to the remote server.

        Unwraps the server's {ok, result | error} envelope so the agent
        sees raw game-tool payloads; {ok:false} surfaces as an error dict
        the SDK tool wrapper marks as isError.
        """
        result = await self.client.call(name, **args)
        if result.get("ok"):
            return result.get("result", result)
        return {"error": result.get("error", {})}

    # ---- state helpers ----

    async def _fetch_state(self) -> dict:
        r = await self.client.call("get_state")
        if not r.get("ok"):
            return {}
        return r.get("result", {})

    def _load_lessons(self) -> list:
        if self.lessons_dir is None:
            return []
        try:
            return LessonStore(self.lessons_dir).list_for_scenario(
                self.scenario, limit=5
            )
        except Exception:
            return []

    # ---- turn orchestration ----

    async def play_turn(self, viewer: Team, *, max_turns: int) -> dict:
        """Drive one half-turn: fetch state, build prompt, let the
        adapter run until the turn ends."""
        state = await self._fetch_state()
        base_prompt = build_turn_prompt_from_state_dict(state, viewer)
        # First turn vs subsequent: subsequent turns frame the state
        # as an update so the persistent transcript reads coherently.
        user_prompt = base_prompt
        system_prompt = build_system_prompt(
            team=viewer,
            max_turns=max_turns,
            strategy=self.strategy,
            lessons=self._load_lessons(),
        )

        await self.adapter.play_turn(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            tools=GAME_TOOLS,
            tool_dispatcher=self._dispatch_tool,
            on_thought=self.thoughts_callback,
            time_budget_s=self.time_budget_s,
        )

        return await self._fetch_state()

    async def summarize_match(self, viewer: Team) -> Lesson | None:
        """Post-match lesson writer. Saves to LessonStore if configured."""
        final_state = await self._fetch_state()
        history_r = await self.client.call("get_history", last_n=60)
        history = (history_r.get("result") or {}).get("history", [])

        lesson = await self.adapter.summarize_match(
            viewer=viewer,
            scenario=self.scenario,
            final_state=final_state,
            action_history=history,
        )
        if lesson is None:
            return None
        if self.lessons_dir is not None:
            try:
                LessonStore(self.lessons_dir).save(lesson)
            except Exception:
                pass
        return lesson
