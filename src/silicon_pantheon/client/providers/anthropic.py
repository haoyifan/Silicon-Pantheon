"""Anthropic (Claude Agent SDK) adapter.

Wraps `ClaudeSDKClient` + `create_sdk_mcp_server` + the SDK's `tool`
decorator behind the provider-agnostic `ProviderAdapter` Protocol so
the rest of the client never imports `claude_agent_sdk` directly.

Persistent session: opened lazily on first `play_turn`, reused across
every subsequent turn. System prompt is baked in at session open and
stays for the match's lifetime. Each `play_turn` sends one user
message onto the existing transcript so the agent retains its
chain-of-thought.
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    TextBlock,
    create_sdk_mcp_server,
    query,
    tool,
)

from silicon_pantheon.client.providers.base import (
    ThoughtCallback,
    ToolDispatcher,
    ToolSpec,
)
from silicon_pantheon.client.providers.errors import classify
from silicon_pantheon.lessons import Lesson, slugify
from silicon_pantheon.server.engine.state import Team

_MCP_SERVER_NAME = "silicon"
log = logging.getLogger("silicon.provider.anthropic")


def _parse_lesson_json(text: str) -> dict | None:
    """Tolerant JSON extractor for the summarizer's response.

    Strips common code-fence wrappers and locates the outermost JSON
    object. Returns None if nothing parseable was found.
    """
    if not text:
        return None
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[1] if "\n" in stripped else ""
        if stripped.endswith("```"):
            stripped = stripped[: -len("```")]
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        data = json.loads(stripped[start : end + 1])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


class AnthropicAdapter:
    """ProviderAdapter for Claude via claude-agent-sdk."""

    def __init__(
        self,
        model: str,
        *,
        max_iterations_per_turn: int = 40,
    ):
        self.model = model
        self.max_iterations = max_iterations_per_turn
        self._sdk_client: ClaudeSDKClient | None = None
        self._system_prompt: str | None = None
        self._turn_count = 0
        # Telemetry for post-game stats.
        self.total_tokens: int = 0
        self.total_tool_calls: int = 0
        self.total_errors: int = 0

    async def _ensure_session(
        self,
        *,
        system_prompt: str,
        tools: list[ToolSpec],
        tool_dispatcher: ToolDispatcher,
    ) -> ClaudeSDKClient:
        if self._sdk_client is not None:
            return self._sdk_client

        sdk_tools = [
            self._wrap_tool(spec, tool_dispatcher) for spec in tools
        ]
        mcp_server = create_sdk_mcp_server(
            name=_MCP_SERVER_NAME, version="1.0", tools=sdk_tools
        )
        allowed = [f"mcp__{_MCP_SERVER_NAME}__{s.name}" for s in tools]
        opts = ClaudeAgentOptions(
            model=self.model,
            system_prompt=system_prompt,
            mcp_servers={_MCP_SERVER_NAME: mcp_server},
            allowed_tools=allowed,
            permission_mode="bypassPermissions",
            max_turns=self.max_iterations,
        )
        client = ClaudeSDKClient(options=opts)
        await client.__aenter__()
        self._sdk_client = client
        self._system_prompt = system_prompt
        return client

    def _wrap_tool(self, spec: ToolSpec, dispatcher: ToolDispatcher):
        """Build one Claude-SDK-decorated tool that calls our dispatcher."""

        adapter = self

        @tool(spec.name, spec.description, spec.input_schema)
        async def _handler(args: dict) -> dict:
            adapter.total_tool_calls += 1
            try:
                result = await dispatcher(spec.name, args or {})
            except Exception as e:
                adapter.total_errors += 1
                return {
                    "content": [
                        {"type": "text", "text": json.dumps({"error": str(e)})}
                    ],
                    "isError": True,
                }
            return {
                "content": [
                    {"type": "text", "text": json.dumps(result, default=str)}
                ]
            }

        return _handler

    async def play_turn(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        tools: list[ToolSpec],
        tool_dispatcher: ToolDispatcher,
        on_thought: ThoughtCallback | None = None,
        time_budget_s: float = 1800.0,
    ) -> None:
        client = await self._ensure_session(
            system_prompt=system_prompt,
            tools=tools,
            tool_dispatcher=tool_dispatcher,
        )
        self._turn_count += 1

        start = time.time()
        try:
            await client.query(user_prompt)
            async for msg in client.receive_response():
                if time.time() - start > time_budget_s:
                    break
                if isinstance(msg, AssistantMessage) and on_thought is not None:
                    for block in msg.content:
                        if isinstance(block, TextBlock) and block.text.strip():
                            try:
                                await on_thought(block.text)
                            except Exception:
                                pass
                # Try to extract token usage from the message.
                usage = getattr(msg, "usage", None)
                if usage is not None:
                    self.total_tokens += getattr(usage, "total_tokens", 0) or (
                        getattr(usage, "input_tokens", 0) + getattr(usage, "output_tokens", 0)
                    )
                if isinstance(msg, ResultMessage):
                    break
        except Exception as e:
            log.exception("Anthropic play_turn raised: %s", e)
            raise classify(e) from e

    async def summarize_match(
        self,
        *,
        viewer: Team,
        scenario: str,
        final_state: dict[str, Any],
        action_history: list[dict[str, Any]],
    ) -> Lesson | None:
        """One-shot tool-less query for a post-match reflection."""
        winner = final_state.get("winner")
        outcome = "draw" if winner is None else ("win" if winner == viewer.value else "loss")
        last = final_state.get("last_action") or {}
        reason = str(last.get("reason", "")) if isinstance(last, dict) else ""

        # Strip class-invariant + display-only fields from the final
        # units list before stuffing it into the summary prompt. Saves
        # several KB per scenario; nothing in the reflection needs
        # art_frames / display_name / descriptions / v2 fields.
        from silicon_pantheon.harness.prompts import _slim_unit

        context = {
            "scenario": scenario,
            "you": viewer.value,
            "outcome": outcome,
            "reason": reason,
            "turns_played": final_state.get("turn"),
            "max_turns": final_state.get("max_turns"),
            "action_history": action_history[-60:],
            "final_units": [
                _slim_unit(u) for u in final_state.get("units", [])
            ],
        }
        prompt = (
            f"You just finished a SiliconPantheon match as {viewer.value} on scenario "
            f"'{scenario}'. Outcome: {outcome}"
            + (f" by {reason}" if reason else "")
            + ".\n\nReflect on ONE key decision or pattern that drove the outcome. "
            "Focus on generalizable tactical principle, not play-by-play.\n\n"
            "Respond with ONLY a JSON object (no prose, no code fences) with fields:\n"
            '  "title": short human title (<=80 chars)\n'
            '  "slug":  kebab-case phrase (<=60 chars)\n'
            '  "body":  markdown, <=400 words, with Situation and Lesson sections\n\n'
            f"Match context (JSON):\n```json\n{json.dumps(context, indent=2, default=str)}\n```\n"
        )
        opts = ClaudeAgentOptions(
            model=self.model,
            system_prompt="You are a tactical post-mortem writer. Return JSON only.",
            max_turns=1,
        )
        text = ""
        try:
            async for msg in query(prompt=prompt, options=opts):
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            text += block.text
                if isinstance(msg, ResultMessage):
                    break
        except Exception:
            return None

        parsed = _parse_lesson_json(text)
        if parsed is None:
            return None
        title = parsed.get("title", "Untitled").strip() or "Untitled lesson"
        slug = slugify(parsed.get("slug", "").strip() or title)
        body = parsed.get("body", "").strip()
        if not body:
            return None
        return Lesson(
            slug=slug,
            title=title,
            scenario=scenario,
            team=viewer.value,
            model=self.model,
            outcome=outcome,
            reason=reason,
            created_at=Lesson.now_iso(),
            body=body,
        )

    async def close(self) -> None:
        if self._sdk_client is not None:
            try:
                await self._sdk_client.__aexit__(None, None, None)
            except Exception:
                pass
            self._sdk_client = None
            self._system_prompt = None
            self._turn_count = 0
