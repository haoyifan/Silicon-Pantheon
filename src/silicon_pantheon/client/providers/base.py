"""Common interface every provider adapter implements.

The rest of the client talks to `ProviderAdapter` only — the code
that drives matches, handles reasoning callbacks, runs the post-
match summary, and manages session lifecycle doesn't know or care
which SDK lives behind the interface.

Lifecycle
---------
  provider = build_provider(...)           # factory chooses concrete class
  provider.system_prompt = "..."           # set once
  for turn in range(max_turns):
      await provider.play_turn(
          user_prompt="...",
          tools=[...],
          tool_dispatcher=dispatch_fn,
          on_thought=on_thought,
      )
  lesson = await provider.summarize_match(...)
  await provider.close()

All methods are async. Persistent-session providers (Anthropic,
OpenAI) open their SDK client lazily on first `play_turn` and
reuse it across turns. `close()` tears everything down.

Tool passing
------------
Tools are described in a provider-agnostic `ToolSpec` (name,
description, JSON-schema input). Each adapter converts these to
its provider's native tool/function format. Tool calls are
executed by the host-provided `tool_dispatcher`, which is a thin
wrapper around the remote MCP game tools.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Protocol

from silicon_pantheon.lessons import Lesson
from silicon_pantheon.server.engine.state import Team


@dataclass(frozen=True)
class ToolSpec:
    """One tool the agent may call during its turn.

    input_schema is JSON Schema (object type with properties).
    Adapters must pass both through to their provider unchanged
    in semantics — any schema normalization (Gemini's stricter
    validation, OpenAI's `strict` mode quirks) happens inside the
    adapter.

    `mutates` flags a tool as state-mutating (move / attack / heal /
    wait / end_turn). Adapters enforce at most one mutating call per
    assistant message while allowing parallel read-only calls in the
    same response — the human-like "observe all the reads, then take
    one action, then observe" pattern. Defaults to False so new read
    tools are safe to add without thinking about batching rules.
    """

    name: str
    description: str
    input_schema: dict[str, Any]
    mutates: bool = False


ToolDispatcher = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]
"""(tool_name, args) -> tool_result. Agent-side hook into the game."""

ThoughtCallback = Callable[[str], Awaitable[None]]
"""Called once per AssistantMessage text block while the agent acts.

The TUI uses this to stream reasoning live into the reasoning panel.
Implementations should be fast and non-blocking; any heavy work
(persisting to disk, re-rendering) should be deferred onto its own
task."""


class ProviderAdapter(Protocol):
    """Protocol every concrete provider adapter implements.

    Implementations keep their SDK-specific state (persistent session,
    tool normalization cache, rate-limit back-off tracker) private.
    """

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
        """Run one turn's reasoning + tool calls.

        Returns when the agent has yielded (typically by calling the
        end_turn tool) OR the time_budget elapses OR the provider SDK
        signals end-of-response. Errors propagate as
        `ProviderError` (defined separately once we add the classifier).
        """
        ...

    async def summarize_match(
        self,
        *,
        viewer: Team,
        scenario: str,
        final_state: dict[str, Any],
        action_history: list[dict[str, Any]],
    ) -> Lesson | None:
        """Post-match reflection. One-shot call, no tools.

        Returns a Lesson (persisted by the caller via LessonStore) or
        None if the provider declined / the response didn't parse.
        """
        ...

    async def close(self) -> None:
        """Tear down persistent session if any. Safe to call twice."""
        ...
