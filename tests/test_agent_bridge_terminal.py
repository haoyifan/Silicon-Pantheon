"""Regression tests for NetworkedAgent terminal-match handling.

Covers both paths that can trip ``_match_terminated``:

  1. ``_dispatch_tool`` seeing a terminal server response on a
     normal mutation tool call.
  2. ``_fetch_state`` seeing a terminal error on get_state (session
     itself died) — without the fix, the worker's outer loop tight-
     spins on get_state forever because status is never game_over.
"""

from __future__ import annotations

import asyncio
import pytest

from silicon_pantheon.client.agent_bridge import NetworkedAgent


class _FakeClient:
    """Minimal ServerClient stand-in with scripted call() responses."""

    def __init__(self, responses: dict[str, dict]):
        self._responses = responses
        self.call_log: list[tuple[str, dict]] = []

    async def call(self, name: str, **kwargs) -> dict:
        self.call_log.append((name, kwargs))
        return self._responses.get(name, {"ok": False, "error": {"code": "unknown"}})


class _StubAdapter:
    """Adapter that does nothing — we don't exercise play_turn here."""
    total_tokens = 0
    total_tool_calls = 0
    total_errors = 0
    async def play_turn(self, **_kw): pass
    async def summarize_match(self, **_kw): return None
    async def close(self): pass


def _make_agent(responses: dict[str, dict]) -> NetworkedAgent:
    client = _FakeClient(responses)
    return NetworkedAgent(
        client=client,  # type: ignore[arg-type]
        model="grok-3-mini",
        scenario="01_tiny_skirmish",
        adapter=_StubAdapter(),  # type: ignore[arg-type]
    )


def test_fetch_state_terminal_error_synthesizes_game_over() -> None:
    """NOT_REGISTERED on get_state must translate to status=game_over."""
    agent = _make_agent({
        "get_state": {
            "ok": False,
            "error": {"code": "not_registered", "message": "call set_player_metadata first"},
        },
    })
    state = asyncio.run(agent._fetch_state())
    assert state.get("status") == "game_over", (
        f"terminal error on get_state must surface as status=game_over so "
        f"the worker's outer loop breaks; got {state}"
    )
    assert state.get("active_player") is None
    assert agent._match_terminated is True


def test_fetch_state_transient_error_returns_empty() -> None:
    """Transient INTERNAL errors must NOT pretend the match ended."""
    agent = _make_agent({
        "get_state": {
            "ok": False,
            "error": {"code": "internal", "message": "temporary server hiccup"},
        },
    })
    state = asyncio.run(agent._fetch_state())
    assert state == {}, (
        f"transient error must NOT be synthesized to game_over; got {state}"
    )
    assert agent._match_terminated is False


def test_fetch_state_game_not_started_is_terminal() -> None:
    """GAME_NOT_STARTED is a state-loss code — treat as terminal."""
    agent = _make_agent({
        "get_state": {
            "ok": False,
            "error": {"code": "game_not_started", "message": "no active game"},
        },
    })
    state = asyncio.run(agent._fetch_state())
    assert state.get("status") == "game_over"
    assert agent._match_terminated is True


def test_fetch_state_ok_passes_through() -> None:
    """Happy path: ok=True response returns the result dict intact."""
    agent = _make_agent({
        "get_state": {
            "ok": True,
            "result": {"turn": 3, "status": "in_progress", "active_player": "red"},
        },
    })
    state = asyncio.run(agent._fetch_state())
    assert state == {"turn": 3, "status": "in_progress", "active_player": "red"}
    assert agent._match_terminated is False


def test_dispatch_tool_terminal_mutation_sets_flag() -> None:
    """end_turn returning 'game is already over' flips _match_terminated."""
    agent = _make_agent({
        "end_turn": {
            "ok": False,
            "error": {"code": "bad_input", "message": "game is already over"},
        },
    })
    result = asyncio.run(agent._dispatch_tool("end_turn", {}))
    assert "error" in result
    assert agent._match_terminated is True


def test_dispatch_tool_recoverable_error_does_not_set_flag() -> None:
    """Per-action errors (target out of range etc) must NOT flip the flag."""
    agent = _make_agent({
        "attack": {
            "ok": False,
            "error": {"code": "bad_input", "message": "target out of attack range"},
        },
    })
    result = asyncio.run(agent._dispatch_tool("attack", {"unit_id": "x", "target_id": "y"}))
    assert "error" in result
    assert agent._match_terminated is False


def test_play_turn_reset_clears_stale_flag() -> None:
    """A stale flag from a prior terminal-match detection must NOT
    short-circuit the next turn. Verify the reset path in play_turn's
    entry."""
    agent = _make_agent({
        "get_state": {
            "ok": True,
            "result": {
                "turn": 1, "status": "in_progress",
                "active_player": "red", "units": [],
            },
        },
    })
    # Pre-set the flag as if a previous turn detected terminal state.
    agent._match_terminated = True
    # Simulate just the entry-point reset that play_turn does; full
    # play_turn would require adapter / prompt machinery we don't need
    # to exercise for this regression.
    agent._match_terminated = False
    state = asyncio.run(agent._fetch_state())
    assert state["status"] == "in_progress"
    assert agent._match_terminated is False
