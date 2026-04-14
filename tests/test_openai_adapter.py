"""Tests for the OpenAI adapter (unit level — mocks the SDK)."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from silicon_pantheon.client.providers.base import ToolSpec
from silicon_pantheon.client.providers.openai import OpenAIAdapter, _as_openai_tool


def test_tool_schema_conversion() -> None:
    spec = ToolSpec(
        name="move",
        description="Move a unit.",
        input_schema={
            "type": "object",
            "properties": {"unit_id": {"type": "string"}},
            "required": ["unit_id"],
        },
    )
    out = _as_openai_tool(spec)
    assert out["type"] == "function"
    assert out["function"]["name"] == "move"
    assert out["function"]["description"] == "Move a unit."
    assert out["function"]["parameters"]["properties"]["unit_id"]["type"] == "string"


class _FakeMessage:
    def __init__(self, content: str | None, tool_calls: list | None = None):
        self.content = content
        self.tool_calls = tool_calls


class _FakeChoice:
    def __init__(self, message: _FakeMessage):
        self.message = message


class _FakeResp:
    def __init__(self, message: _FakeMessage):
        self.choices = [_FakeChoice(message)]


class _FakeToolCall:
    def __init__(self, call_id: str, name: str, args: dict):
        self.id = call_id
        self.function = SimpleNamespace(
            name=name, arguments=json.dumps(args)
        )


def _make_adapter_with_mock_client(responses):
    """Return an OpenAIAdapter whose client returns the given responses
    in order."""
    adapter = OpenAIAdapter(model="gpt-5-mini", api_key="sk-fake")
    it = iter(responses)

    class _FakeCompletions:
        async def create(self, **kwargs):
            return next(it)

    class _FakeChat:
        def __init__(self):
            self.completions = _FakeCompletions()

    class _FakeClient:
        def __init__(self):
            self.chat = _FakeChat()

        async def close(self):
            pass

    adapter._client = _FakeClient()  # type: ignore[assignment]
    return adapter


@pytest.mark.asyncio
async def test_play_turn_dispatches_tool_calls() -> None:
    # First response: one tool_call to get_state.
    # Second response: plain text, no tool calls → loop ends.
    tool_call = _FakeToolCall("tc1", "get_state", {})
    first = _FakeResp(_FakeMessage(content=None, tool_calls=[tool_call]))
    second = _FakeResp(_FakeMessage(content="Plan: move archer.", tool_calls=None))
    adapter = _make_adapter_with_mock_client([first, second])

    dispatched: list[tuple[str, dict]] = []

    async def dispatcher(name, args):
        dispatched.append((name, args))
        return {"turn": 1, "active_player": "blue"}

    thoughts: list[str] = []

    async def on_thought(text):
        thoughts.append(text)

    tools = [
        ToolSpec(
            "get_state",
            "Get state.",
            {"type": "object", "properties": {}, "required": []},
        ),
    ]

    await adapter.play_turn(
        system_prompt="You are blue.",
        user_prompt="Your turn.",
        tools=tools,
        tool_dispatcher=dispatcher,
        on_thought=on_thought,
    )

    assert dispatched == [("get_state", {})]
    assert thoughts == ["Plan: move archer."]
    # Transcript should now have: system, user, assistant (tool call),
    # tool result, assistant (final).
    roles = [m["role"] for m in adapter._messages]
    assert roles == ["system", "user", "assistant", "tool", "assistant"]


@pytest.mark.asyncio
async def test_play_turn_stops_on_empty_tool_calls_first_response() -> None:
    """If the first response has no tool calls, the loop exits cleanly."""
    adapter = _make_adapter_with_mock_client(
        [_FakeResp(_FakeMessage(content="OK", tool_calls=None))]
    )

    async def dispatcher(_n, _a):
        raise AssertionError("should not be called")

    await adapter.play_turn(
        system_prompt="sys",
        user_prompt="user",
        tools=[],
        tool_dispatcher=dispatcher,
        on_thought=None,
    )
    roles = [m["role"] for m in adapter._messages]
    assert roles == ["system", "user", "assistant"]


@pytest.mark.asyncio
async def test_play_turn_surfaces_reasoning_content() -> None:
    """Grok (grok-4 / grok-3-mini) and OpenAI o-series put chain-of-
    thought in `reasoning_content`, leaving `content` empty when the
    turn is pure reasoning + tool calls. The adapter must emit it via
    on_thought so the TUI thoughts panel populates — otherwise the
    user sees a blank panel even though the model is reasoning."""

    class _MsgWithReasoning:
        def __init__(self, reasoning: str, content: str | None, tool_calls=None):
            self.reasoning_content = reasoning
            self.content = content
            self.tool_calls = tool_calls

    # Response has reasoning but no content and no tool calls — loop exits.
    resp = _FakeResp(
        _MsgWithReasoning(
            reasoning="Considering archer vs knight matchup…",
            content=None,
            tool_calls=None,
        )
    )
    adapter = _make_adapter_with_mock_client([resp])

    thoughts: list[str] = []

    async def on_thought(text):
        thoughts.append(text)

    await adapter.play_turn(
        system_prompt="sys",
        user_prompt="user",
        tools=[],
        tool_dispatcher=None,  # no tool calls to dispatch
        on_thought=on_thought,
    )
    assert thoughts == ["Considering archer vs knight matchup…"]


@pytest.mark.asyncio
async def test_close_is_idempotent() -> None:
    adapter = _make_adapter_with_mock_client([])
    await adapter.close()
    await adapter.close()
