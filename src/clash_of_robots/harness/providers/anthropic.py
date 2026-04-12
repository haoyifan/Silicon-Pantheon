"""Claude Agent SDK-backed provider.

Wraps the in-process tool layer as SDK MCP tools and drives a one-shot `query`
per turn. Uses the user's existing Claude subscription auth via the Claude CLI.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from claude_agent_sdk import (
    ClaudeAgentOptions,
    ResultMessage,
    create_sdk_mcp_server,
    query,
    tool,
)

from clash_of_robots.harness.prompts import build_system_prompt, build_turn_prompt, load_strategy
from clash_of_robots.harness.providers.base import Provider
from clash_of_robots.server.engine.state import Team
from clash_of_robots.server.session import Session
from clash_of_robots.server.tools import TOOL_REGISTRY, ToolError, call_tool

MCP_SERVER_NAME = "clash"


def _sdk_tools_for(session: Session, viewer: Team):
    """Wrap each TOOL_REGISTRY entry as an SDK MCP tool bound to this session/viewer."""
    sdk_tools = []
    for name, spec in TOOL_REGISTRY.items():
        sdk_tools.append(_make_one(name, spec, session, viewer))
    return sdk_tools


def _make_one(name: str, spec: dict, session: Session, viewer: Team):
    description = spec["description"]
    schema = spec["input_schema"]

    @tool(name, description, schema)
    async def _handler(args):
        try:
            result = call_tool(session, viewer, name, args or {})
            return {"content": [{"type": "text", "text": json.dumps(result)}]}
        except ToolError as e:
            return {
                "content": [{"type": "text", "text": json.dumps({"error": str(e)})}],
                "isError": True,
            }

    return _handler


class AnthropicProvider(Provider):
    name = "anthropic"

    def __init__(
        self,
        model: str,
        strategy_path: str | Path | None = None,
        time_budget_s: float = 90.0,
        max_agent_iterations: int = 40,
    ):
        self.model = model
        self.strategy = load_strategy(strategy_path)
        self.time_budget_s = time_budget_s
        self.max_agent_iterations = max_agent_iterations

    def decide_turn(self, session: Session, viewer: Team) -> None:
        asyncio.run(self._async_turn(session, viewer))

    async def _async_turn(self, session: Session, viewer: Team) -> None:
        start = time.time()
        turn_at_start = session.state.turn
        sdk_tools = _sdk_tools_for(session, viewer)
        mcp_server = create_sdk_mcp_server(name=MCP_SERVER_NAME, version="0.1.0", tools=sdk_tools)

        system_prompt = build_system_prompt(
            team=viewer, max_turns=session.state.max_turns, strategy=self.strategy
        )
        turn_prompt = build_turn_prompt(session, viewer)

        allowed = [f"mcp__{MCP_SERVER_NAME}__{n}" for n in TOOL_REGISTRY]
        opts = ClaudeAgentOptions(
            model=self.model,
            system_prompt=system_prompt,
            mcp_servers={MCP_SERVER_NAME: mcp_server},
            allowed_tools=allowed,
            permission_mode="bypassPermissions",
            max_turns=self.max_agent_iterations,
        )

        try:
            async for msg in query(prompt=turn_prompt, options=opts):
                if isinstance(msg, ResultMessage):
                    break
                # If the agent already called end_turn, state has flipped.
                if session.state.active_player is not viewer:
                    break
                # Time budget
                if time.time() - start > self.time_budget_s:
                    break
        except Exception as e:
            session.log("agent_error", {"team": viewer.value, "error": str(e)})

        # If the agent didn't end its turn, force it.
        if session.state.active_player is viewer and session.state.turn == turn_at_start:
            self._force_end_turn(session, viewer)

    def _force_end_turn(self, session: Session, viewer: Team) -> None:
        # Wait any mid-action units, then end turn.
        for u in list(session.state.units_of(viewer)):
            if u.status.value == "moved":
                try:
                    call_tool(session, viewer, "wait", {"unit_id": u.id})
                except ToolError:
                    pass
        try:
            call_tool(session, viewer, "end_turn", {})
        except ToolError:
            pass
        session.log("forced_end_turn", {"team": viewer.value})
