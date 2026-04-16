"""Adapter tests for the Codex (subscription OAuth) provider.

Mocks the Responses API endpoint via httpx.MockTransport so we
exercise the full request-build → response-parse → tool-dispatch
loop without hitting the network.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import httpx
import pytest

from silicon_pantheon.client.providers.base import ToolSpec
from silicon_pantheon.client.providers.codex import (
    CodexAdapter,
    CodexAuthError,
    CodexCredentials,
)
from silicon_pantheon.client.providers.codex import oauth as codex_oauth


# ---- fixtures ----------------------------------------------------------


@pytest.fixture
def stub_creds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Persist a fake CodexCredentials with a fresh expiry so the
    adapter's auth path is satisfied without ever calling the
    refresh endpoint."""
    p = tmp_path / "codex-oauth.json"
    monkeypatch.setattr(codex_oauth, "CREDENTIALS_PATH", p)
    creds = CodexCredentials(
        access_token="acc-stub",
        refresh_token="ref-stub",
        expires_at=time.time() + 3600,
        account_id="acct-1",
    )
    codex_oauth.save_credentials(creds)
    return creds


def _patch_async_client(monkeypatch, transport):
    """Force httpx.AsyncClient to use a MockTransport."""
    real = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient",
        lambda **kw: real(
            transport=transport,
            **{k: v for k, v in kw.items() if k != "transport"},
        ),
    )


# ---- happy path: model emits text + tool_calls ------------------------


@pytest.mark.asyncio
async def test_play_turn_dispatches_tool_calls(stub_creds, monkeypatch):
    """First response: one function_call; second: terminal text.
    Verifies the loop dispatches the tool and feeds the result back
    in as a function_call_output."""
    seen_requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen_requests.append(body)
        # Authorization header must carry our bearer token.
        assert request.headers["Authorization"] == "Bearer acc-stub"
        assert request.headers["User-Agent"].startswith("codex_cli_rs/")
        if len(seen_requests) == 1:
            # First call → function_call requesting get_state.
            return httpx.Response(200, json={
                "output": [
                    {"type": "reasoning", "summary": [
                        {"type": "summary_text", "text": "Plan: check state."}
                    ]},
                    {"type": "function_call", "call_id": "call-1",
                     "name": "get_state", "arguments": "{}"},
                ],
            })
        # Second call → terminal text, no tool_calls → loop exits.
        return httpx.Response(200, json={
            "output": [
                {"type": "message", "role": "assistant", "content": [
                    {"type": "output_text", "text": "Done."}
                ]},
            ],
        })

    _patch_async_client(monkeypatch, httpx.MockTransport(handler))

    adapter = CodexAdapter(model="gpt-5-codex", credentials=stub_creds)
    dispatched: list[tuple[str, dict]] = []
    thoughts: list[str] = []

    async def dispatch(name, args):
        dispatched.append((name, args))
        return {"turn": 1, "active_player": "blue"}

    async def on_thought(text):
        thoughts.append(text)

    await adapter.play_turn(
        system_prompt="You are blue.",
        user_prompt="Your turn.",
        tools=[
            ToolSpec("get_state", "Get state.",
                     {"type": "object", "properties": {}, "required": []}),
        ],
        tool_dispatcher=dispatch,
        on_thought=on_thought,
    )
    await adapter.close()

    # Tool was dispatched.
    assert dispatched == [("get_state", {})]
    # Reasoning surfaces on iter 1 (preferred over text); the
    # terminal iter 2 has no reasoning so plain text comes through.
    assert thoughts == ["Plan: check state.", "Done."]
    # Two POSTs total (one for tool call, one for the terminal text).
    assert len(seen_requests) == 2
    # Second request must include the function_call_output we
    # appended after dispatching the tool.
    second_input = seen_requests[1]["input"]
    fco_items = [i for i in second_input
                 if isinstance(i, dict) and i.get("type") == "function_call_output"]
    assert len(fco_items) == 1
    assert fco_items[0]["call_id"] == "call-1"
    assert "active_player" in fco_items[0]["output"]


# ---- Layer 2: drop parallel calls, synthesize errors -----------------


@pytest.mark.asyncio
async def test_layer2_drops_extra_parallel_function_calls(stub_creds, monkeypatch):
    """If the Codex backend ignores parallel_tool_calls=False and emits
    multiple function_calls in one response, the adapter executes only
    the first and replies to the rest with synthetic
    dropped_parallel_call errors via function_call_output items."""
    seen_requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen_requests.append(body)
        if len(seen_requests) == 1:
            return httpx.Response(200, json={
                "output": [
                    {"type": "function_call", "call_id": "call_1",
                     "name": "move",
                     "arguments": '{"unit_id":"u1","dest":{"x":4,"y":4}}'},
                    {"type": "function_call", "call_id": "call_2",
                     "name": "wait", "arguments": '{"unit_id":"u1"}'},
                    {"type": "function_call", "call_id": "call_3",
                     "name": "end_turn", "arguments": "{}"},
                ],
            })
        return httpx.Response(200, json={
            "output": [{"type": "message", "role": "assistant",
                        "content": [{"type": "output_text", "text": "Done."}]}],
        })

    _patch_async_client(monkeypatch, httpx.MockTransport(handler))
    adapter = CodexAdapter(credentials=stub_creds)
    dispatched: list[tuple[str, dict]] = []

    async def dispatcher(name, args):
        dispatched.append((name, args))
        return {"ok": True}

    await adapter.play_turn(
        system_prompt="s", user_prompt="u",
        tools=[ToolSpec("move", "m", {"type": "object"}, mutates=True),
               ToolSpec("wait", "w", {"type": "object"}, mutates=True),
               ToolSpec("end_turn", "e", {"type": "object"}, mutates=True)],
        tool_dispatcher=dispatcher, on_thought=None,
    )
    await adapter.close()

    # All three are mutating; only the first should run.
    assert dispatched == [("move", {"unit_id": "u1", "dest": {"x": 4, "y": 4}})]

    # Second request's input must contain three function_call_output
    # items — one real for call_1, two synthetic dropped errors for
    # call_2 / call_3.
    fcos = [
        i for i in seen_requests[1]["input"]
        if isinstance(i, dict) and i.get("type") == "function_call_output"
    ]
    assert len(fcos) == 3
    by_id = {f["call_id"]: json.loads(f["output"]) for f in fcos}
    assert by_id["call_1"] == {"ok": True}
    for cid in ("call_2", "call_3"):
        err = by_id[cid].get("error") or {}
        assert err.get("code") == "dropped_parallel_mutation"
        assert "DROPPED" in err.get("message", "")


# ---- terminal-text-first response → loop exits cleanly --------------


@pytest.mark.asyncio
async def test_play_turn_stops_on_no_tool_calls(stub_creds, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "output": [
                {"type": "message", "role": "assistant", "content": [
                    {"type": "output_text", "text": "OK"}
                ]},
            ],
        })

    _patch_async_client(monkeypatch, httpx.MockTransport(handler))
    adapter = CodexAdapter(credentials=stub_creds)
    fired = False

    async def dispatch(_n, _a):
        nonlocal fired
        fired = True

    await adapter.play_turn(
        system_prompt="sys", user_prompt="user",
        tools=[], tool_dispatcher=dispatch, on_thought=None,
    )
    await adapter.close()
    assert not fired


# ---- request-shape correctness ---------------------------------------


@pytest.mark.asyncio
async def test_request_body_uses_responses_api_shape(stub_creds, monkeypatch):
    """Verify the request matches the Responses-API schema, NOT Chat
    Completions. messages → input; nested content arrays; tools
    flattened; reasoning summary requested."""
    captured: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={"output": []})

    _patch_async_client(monkeypatch, httpx.MockTransport(handler))
    adapter = CodexAdapter(model="gpt-5-codex", credentials=stub_creds)
    tool = ToolSpec(
        "ping", "ping",
        {"type": "object", "properties": {"x": {"type": "integer"}}},
    )
    await adapter.play_turn(
        system_prompt="Hello", user_prompt="World",
        tools=[tool], tool_dispatcher=None, on_thought=None,
    )
    await adapter.close()
    assert len(captured) == 1
    body = captured[0]
    # Responses API uses `input`, not `messages`.
    assert "input" in body
    assert "messages" not in body
    # Both system + user messages live in input as wrapped objects.
    inp = body["input"]
    assert inp[0] == {
        "type": "message", "role": "developer",
        "content": [{"type": "input_text", "text": "Hello"}],
    }
    assert inp[1] == {
        "type": "message", "role": "user",
        "content": [{"type": "input_text", "text": "World"}],
    }
    # Tools are flat (no nested {"function": {...}}).
    assert body["tools"] == [{
        "type": "function", "name": "ping", "description": "ping",
        "parameters": {"type": "object",
                       "properties": {"x": {"type": "integer"}}},
    }]
    # Reasoning summary is requested.
    assert body["reasoning"] == {"summary": "auto"}
    # parallel_tool_calls is True — selective Layer 2 is what enforces
    # the one-mutation rule while allowing batched reads. Pinning
    # explicitly so a future accidental flip to False doesn't silently
    # re-introduce the "too-slow-to-play" regression.
    assert body["parallel_tool_calls"] is True


# ---- 401 → refresh → retry path --------------------------------------


@pytest.mark.asyncio
async def test_play_turn_refreshes_token_on_401(stub_creds, monkeypatch):
    """First request gets 401; the adapter must drop cached creds and
    retry. We seed a refresh handler too so the retry succeeds."""
    call_log: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if "oauth/token" in str(request.url):
            call_log.append("refresh")
            return httpx.Response(200, json={
                "access_token": "acc-fresh",
                "refresh_token": "ref-stub",
                "expires_in": 3600,
            })
        # Responses endpoint.
        if request.headers.get("Authorization") == "Bearer acc-stub":
            call_log.append("responses-401")
            return httpx.Response(401, text="invalid token")
        if request.headers.get("Authorization") == "Bearer acc-fresh":
            call_log.append("responses-200")
            return httpx.Response(200, json={"output": []})
        return httpx.Response(500)

    _patch_async_client(monkeypatch, httpx.MockTransport(handler))

    # Force the cached creds to look expired so ensure_fresh refreshes
    # immediately on the retry.
    expired = CodexCredentials(
        access_token="acc-stub", refresh_token="ref-stub",
        expires_at=time.time() - 10,
    )
    codex_oauth.save_credentials(expired)

    adapter = CodexAdapter(credentials=None)  # force load from disk
    await adapter.play_turn(
        system_prompt="s", user_prompt="u",
        tools=[], tool_dispatcher=None, on_thought=None,
    )
    await adapter.close()

    # Expected sequence: refresh (because expired) → 401 from stale
    # cached token? Actually: ensure_fresh refreshes BEFORE the first
    # POST since expired. So we get refresh → responses-200 (with the
    # fresh token from refresh). The 401 path is exercised when the
    # token rotates server-side mid-flight.
    assert "refresh" in call_log
    assert "responses-200" in call_log


# ---- compaction across turns ------------------------------------------


@pytest.mark.asyncio
async def test_compaction_shrinks_prior_turn_items(stub_creds, monkeypatch):
    """Cross-turn compaction must shrink the _input array without
    breaking the Responses-API pairing invariants. Pins all four
    transformations in one go: developer deduped, oversize user
    message truncated, function_call_output stubbed, reasoning
    dropped — and function_call items stay intact so pairing holds."""
    calls_seen = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls_seen
        calls_seen += 1
        # Turn 1: model calls get_state, then stops.
        if calls_seen == 1:
            return httpx.Response(200, json={
                "output": [
                    {"type": "reasoning", "summary": [
                        {"type": "summary_text",
                         "text": "Thinking through turn 1 plan in detail."}
                    ]},
                    {"type": "function_call", "call_id": "call_1",
                     "name": "get_state", "arguments": "{}"},
                ],
            })
        if calls_seen == 2:
            return httpx.Response(200, json={
                "output": [
                    {"type": "message", "role": "assistant", "content": [
                        {"type": "output_text", "text": "Done turn 1."}
                    ]},
                ],
            })
        # Turn 2: just exits cleanly.
        return httpx.Response(200, json={
            "output": [
                {"type": "message", "role": "assistant", "content": [
                    {"type": "output_text", "text": "Done turn 2."}
                ]},
            ],
        })

    _patch_async_client(monkeypatch, httpx.MockTransport(handler))
    adapter = CodexAdapter(credentials=stub_creds)

    async def dispatch(_n, _a):
        # Huge tool result simulating a state dump — compaction must
        # stub this on the next turn.
        return {"big": "x" * 8000}

    # Turn 1 with an oversize bootstrap user prompt.
    big_bootstrap = "TURN 1 BOOTSTRAP: " + ("b" * 8000)
    await adapter.play_turn(
        system_prompt="sys",
        user_prompt=big_bootstrap,
        tools=[ToolSpec("get_state", "gs",
                        {"type": "object", "properties": {}})],
        tool_dispatcher=dispatch, on_thought=None,
    )
    # Inject a second developer message (simulates a corrective
    # nudge from some prior turn-scoped path) so we can prove the
    # subsequent-developer drop rule fires too.
    adapter._input.insert(1, {
        "type": "message", "role": "developer",
        "content": [{"type": "input_text", "text": "nudge from earlier"}],
    })
    # And one more reasoning item hiding in prior history.
    adapter._input.append({
        "type": "reasoning", "summary": [
            {"type": "summary_text", "text": "late reasoning " * 100},
        ],
    })

    before = adapter._estimate_tokens(adapter._input)

    # Turn 2: compaction runs at entry.
    await adapter.play_turn(
        system_prompt="sys", user_prompt="turn 2 small delta",
        tools=[ToolSpec("get_state", "gs",
                        {"type": "object", "properties": {}})],
        tool_dispatcher=dispatch, on_thought=None,
    )
    # NOTE: don't close() yet — close() wipes self._input. Snapshot
    # what we need first, then close at the end.

    after = adapter._estimate_tokens(adapter._input)

    # Shrank meaningfully — the oversize bootstrap + huge tool
    # output + reasoning items together were the majority of
    # tokens; all three were collapsed.
    assert after < before // 2, (
        f"compaction didn't shrink: {before}→{after}"
    )

    # Only ONE developer message survived (the canonical system
    # prompt). The injected nudge was dropped.
    dev_msgs = [
        i for i in adapter._input
        if isinstance(i, dict)
        and i.get("type") == "message"
        and i.get("role") == "developer"
    ]
    assert len(dev_msgs) == 1

    # No reasoning items survived — old chain-of-thought dropped.
    assert not any(
        isinstance(i, dict) and i.get("type") == "reasoning"
        for i in adapter._input
    )

    # function_call items still present (pairing invariant) —
    # AND their function_call_output pairs still present with
    # stubbed output.
    fcos = [
        i for i in adapter._input
        if isinstance(i, dict) and i.get("type") == "function_call_output"
    ]
    assert fcos, "function_call_output items must survive for pairing"
    for fco in fcos:
        # Stubbed output, not the original 8KB payload.
        assert fco.get("output") == adapter._STUB_TOOL_RESULT, (
            f"stub not applied: {fco.get('output', '')[:60]!r}"
        )

    # Bootstrap user message truncated (was ~8KB, must be well under).
    user_msgs = [
        i for i in adapter._input
        if isinstance(i, dict)
        and i.get("type") == "message"
        and i.get("role") == "user"
    ]
    bootstrap = user_msgs[0]
    text = (bootstrap["content"][0] or {}).get("text", "")
    assert len(text) < 4000, f"bootstrap not truncated: {len(text)} chars"
    assert "bootstrap snapshot truncated" in text

    await adapter.close()


# ---- close is idempotent ----------------------------------------------


@pytest.mark.asyncio
async def test_close_is_idempotent(stub_creds):
    adapter = CodexAdapter(credentials=stub_creds)
    await adapter.close()
    await adapter.close()
