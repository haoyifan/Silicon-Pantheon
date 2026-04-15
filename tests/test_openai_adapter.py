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
async def test_layer2_drops_extra_parallel_tool_calls() -> None:
    """If the provider ignores parallel_tool_calls=False and emits N
    tool_calls in one assistant message, the adapter MUST execute only
    the first and reply to the rest with synthetic dropped_parallel_call
    errors (preserving the OpenAI invariant that every tool_calls[i]
    needs a matching tool message)."""
    tc1 = _FakeToolCall("call_1", "move", {"unit_id": "u1", "dest": {"x": 4, "y": 4}})
    tc2 = _FakeToolCall("call_2", "wait", {"unit_id": "u1"})
    tc3 = _FakeToolCall("call_3", "end_turn", {})
    first = _FakeResp(_FakeMessage(content=None, tool_calls=[tc1, tc2, tc3]))
    second = _FakeResp(_FakeMessage(content="done.", tool_calls=None))
    adapter = _make_adapter_with_mock_client([first, second])

    dispatched: list[tuple[str, dict]] = []

    async def dispatcher(name, args):
        dispatched.append((name, args))
        return {"ok": True}

    await adapter.play_turn(
        system_prompt="sys", user_prompt="user",
        tools=[
            ToolSpec("move", "m", {"type": "object"}),
            ToolSpec("wait", "w", {"type": "object"}),
            ToolSpec("end_turn", "e", {"type": "object"}),
        ],
        tool_dispatcher=dispatcher, on_thought=None,
    )

    # Only the first tool call ran for real.
    assert dispatched == [("move", {"unit_id": "u1", "dest": {"x": 4, "y": 4}})]

    # Transcript must include one assistant message with all 3 tool_calls
    # AND three matching tool messages (1 real + 2 synthetic errors).
    assistant_msgs = [m for m in adapter._messages if m["role"] == "assistant"]
    tool_msgs = [m for m in adapter._messages if m["role"] == "tool"]
    assert len(tool_msgs) == 3
    # tool_call_id pairing is preserved.
    assert {m["tool_call_id"] for m in tool_msgs} == {"call_1", "call_2", "call_3"}
    # call_1 is the real result; call_2 and call_3 carry the synthetic error.
    by_id = {m["tool_call_id"]: json.loads(m["content"]) for m in tool_msgs}
    assert by_id["call_1"] == {"ok": True}
    for cid in ("call_2", "call_3"):
        err = by_id[cid].get("error") or {}
        assert err.get("code") == "dropped_parallel_call"
        assert "DROPPED" in err.get("message", "")


@pytest.mark.asyncio
async def test_request_sends_parallel_tool_calls_false() -> None:
    """Layer 1: every chat.completions.create call must include
    parallel_tool_calls=False so the model is told at decode time not
    to batch. Without this, weak models batch a whole turn into one
    response and lose the act-observe-decide loop."""
    captured: list[dict] = []

    class _CapturingCompletions:
        async def create(self, **kwargs):
            captured.append(kwargs)
            return _FakeResp(_FakeMessage(content="ok", tool_calls=None))

    class _Chat:
        def __init__(self):
            self.completions = _CapturingCompletions()

    class _Cli:
        def __init__(self):
            self.chat = _Chat()

        async def close(self):
            pass

    adapter = OpenAIAdapter(model="gpt-5-mini", api_key="sk-fake")
    adapter._client = _Cli()  # type: ignore[assignment]
    await adapter.play_turn(
        system_prompt="s", user_prompt="u",
        tools=[], tool_dispatcher=None, on_thought=None,
    )
    assert len(captured) == 1
    assert captured[0].get("parallel_tool_calls") is False


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
async def test_transcript_compacts_between_turns() -> None:
    """Regression: at turn 5-6 a Grok match hit
    'maximum prompt length is 131072 but request contains 351186'
    because self._messages grew unboundedly — every get_state tool
    result (10-20KB) + every assistant tool_call stayed forever.

    After each turn the adapter should compact PRIOR turns down to
    their reasoning (system + user + assistant.content) and drop
    tool_calls / tool results that have no cross-turn value."""

    # Build a fake conversation: system + user turn 1 + assistant-with-
    # tool-calls + tool result + assistant text + user turn 2
    # (about-to-start). After compaction the tool messages from
    # turn 1 should be gone.
    assistant_final_turn1 = _FakeResp(
        _FakeMessage(content="Plan turn 1.", tool_calls=None)
    )
    assistant_final_turn2 = _FakeResp(
        _FakeMessage(content="Plan turn 2.", tool_calls=None)
    )
    adapter = _make_adapter_with_mock_client(
        [assistant_final_turn1, assistant_final_turn2]
    )

    # Turn 1: no tool calls, just text → simple transcript after.
    await adapter.play_turn(
        system_prompt="sys",
        user_prompt="turn 1 state",
        tools=[],
        tool_dispatcher=None,
        on_thought=None,
    )
    # Manually inject a fake tool_call pair into turn 1's messages,
    # simulating what happens when the model DOES call tools — we want
    # to verify those evict on compaction.
    adapter._messages.insert(
        2,
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "tc1", "type": "function",
                 "function": {"name": "get_state", "arguments": "{}"}},
            ],
        },
    )
    adapter._messages.insert(
        3,
        {
            "role": "tool",
            "tool_call_id": "tc1",
            # 20KB fake state dump — the exact shape that filled the
            # context window in production.
            "content": "x" * 20000,
        },
    )
    before_tokens = adapter._estimate_tokens(adapter._messages)

    # Turn 2: triggers _compact_prior_turns at entry.
    await adapter.play_turn(
        system_prompt="sys",
        user_prompt="turn 2 state",
        tools=[],
        tool_dispatcher=None,
        on_thought=None,
    )

    after_tokens = adapter._estimate_tokens(adapter._messages)
    roles = [m["role"] for m in adapter._messages]

    # The transcript shrank substantially (the 20KB dump was the
    # majority of tokens). Compaction now stubs out tool result
    # CONTENT but keeps the message structure so xAI / Grok models
    # still see the proper tool-call protocol in their history.
    assert after_tokens < before_tokens // 4, (
        f"compaction didn't shrink meaningfully: {before_tokens}→{after_tokens}"
    )
    # Tool messages survive structurally (with stub content) so the
    # paired assistant.tool_calls isn't orphaned.
    tool_msgs = [m for m in adapter._messages if m.get("role") == "tool"]
    assert tool_msgs, "tool messages must survive structurally"
    for tm in tool_msgs:
        # Either the stub or the legitimate (small) current-turn one.
        # The injected 20KB dump must NOT be intact.
        assert len(tm.get("content", "")) < 200, (
            f"tool result content not stubbed: {tm.get('content', '')[:60]}..."
        )
    # System prompt preserved.
    assert roles[0] == "system"
    # Prior turn's assistant reasoning text survives.
    assert any(
        m.get("role") == "assistant" and "Plan turn 1" in (m.get("content") or "")
        for m in adapter._messages
    )
    # AND the tool_calls metadata on a prior assistant survives, so
    # the model sees the format pattern.
    assert any(
        m.get("role") == "assistant" and m.get("tool_calls")
        for m in adapter._messages
    ), (
        "no assistant.tool_calls survived compaction — xAI / Grok "
        "models lose the format pattern and start hallucinating "
        "<function_call> XML in plain text"
    )


@pytest.mark.asyncio
async def test_compaction_drops_corrective_system_messages() -> None:
    """Corrective system messages we inject mid-turn ("use proper
    tool_calls") MUST evict at the next compaction. Otherwise every
    stuck turn permanently bloats the transcript with one or two
    extra system messages — over many turns this is what kept the
    context blowing up even after the per-turn compaction fix.

    Only the FIRST system message (the canonical system prompt) is
    kept across compactions."""

    bad_resp = _FakeResp(
        _FakeMessage(
            content="<function_call>get_state()</function_call>",
            tool_calls=None,
        )
    )
    # After the corrective injection the model still emits no tool
    # calls so the loop exits.
    fixed_resp = _FakeResp(_FakeMessage(content="OK done", tool_calls=None))

    adapter = _make_adapter_with_mock_client(
        [bad_resp, fixed_resp,  # turn 1: hallucinate, give up
         _FakeResp(_FakeMessage(content="t2", tool_calls=None))]  # turn 2
    )

    await adapter.play_turn(
        system_prompt="canonical system prompt",
        user_prompt="turn 1 state",
        tools=[],
        tool_dispatcher=None,
        on_thought=None,
    )
    # After turn 1 we expect at least one corrective system message
    # in the transcript.
    sys_msgs_after_t1 = [m for m in adapter._messages if m.get("role") == "system"]
    assert len(sys_msgs_after_t1) >= 2, (
        f"correction wasn't injected: roles={[m.get('role') for m in adapter._messages]}"
    )

    # Turn 2 — compaction runs at entry.
    await adapter.play_turn(
        system_prompt="canonical system prompt",
        user_prompt="turn 2 state",
        tools=[],
        tool_dispatcher=None,
        on_thought=None,
    )

    sys_msgs_after_t2 = [m for m in adapter._messages if m.get("role") == "system"]
    assert len(sys_msgs_after_t2) == 1, (
        f"corrective system messages survived compaction: "
        f"{[m.get('content', '')[:40] for m in sys_msgs_after_t2]}"
    )
    assert sys_msgs_after_t2[0]["content"] == "canonical system prompt"


@pytest.mark.asyncio
async def test_xml_function_call_hallucination_triggers_correction() -> None:
    """Regression: a Grok match looped forever because the model
    emitted "<function_call>get_legal_actions(...)</function_call>"
    as plain content text instead of using the API tool_calls field.
    The loop saw no tool_calls and broke; play_turn returned without
    end_turn; the TUI re-triggered with the same delta prompt; the
    model produced the same hallucination; repeat. Detection +
    corrective system reminder breaks the loop."""

    # First response: hallucinated XML, no real tool_calls.
    bad = _FakeResp(
        _FakeMessage(
            content="I'll check legal actions: <function_call>get_legal_actions(unit_id=\"u_b_x\")</function_call>",
            tool_calls=None,
        )
    )
    # After our corrective system message, the model uses the real
    # tool_calls protocol.
    fixed = _FakeResp(_FakeMessage(content="OK", tool_calls=None))

    adapter = _make_adapter_with_mock_client([bad, fixed])

    await adapter.play_turn(
        system_prompt="sys",
        user_prompt="turn 1",
        tools=[],
        tool_dispatcher=None,
        on_thought=None,
    )

    # The corrective system message must have been injected.
    sys_msgs = [m for m in adapter._messages if m.get("role") == "system"]
    correction = [
        m for m in sys_msgs
        if "<function_call>" in (m.get("content") or "")
        and "inert" in (m.get("content") or "")
    ]
    assert correction, (
        f"no corrective reminder injected; messages: "
        f"{[(m.get('role'), (m.get('content') or '')[:50]) for m in adapter._messages]}"
    )
    # Loop continued past the bad iteration (we got two responses).
    assert adapter._corrections_this_turn == 1


@pytest.mark.asyncio
async def test_close_is_idempotent() -> None:
    adapter = _make_adapter_with_mock_client([])
    await adapter.close()
    await adapter.close()
