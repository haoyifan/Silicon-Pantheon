"""Regressions for the agent-turn / API ownership audit.

Bug history: an agent received a turn prompt claiming "it's your
turn", then every action it called came back `not_your_turn` from
the server. Two defects stacked:

  1. NetworkedAgent.play_turn built the prompt unconditionally. The
     decision to spawn a play_turn was made by the TUI off polled
     state that can be ~1s stale; by the time the task fetched
     fresh state, active_player could disagree with `viewer`.
  2. build_turn_prompt_from_state_dict substituted `viewer.value`
     into "It is your ({team}) turn" without verifying that
     active_player actually matched. If called with mismatched
     state it silently lied to the model.

This test pins the fixes:
  - play_turn now re-checks active_player on fresh state and
    returns early if it's not our turn (no tool calls, no prompt
    sent).
  - build_turn_prompt_from_state_dict prepends a warning block
    when the snapshot's active_player disagrees with viewer, so
    any code path that bypasses the play_turn guard still can't
    mislead the LLM.
"""

from __future__ import annotations

import asyncio

from silicon_pantheon.harness.prompts import build_turn_prompt_from_state_dict
from silicon_pantheon.server.engine.state import Team


def _state(active: str, turn: int = 3) -> dict:
    return {
        "turn": turn,
        "active_player": active,
        "you": "blue",
        "board": {"width": 4, "height": 4, "forts": []},
        "units": [],
        "last_action": None,
        "status": "in_progress",
    }


def test_turn_prompt_is_truthful_when_ownership_matches() -> None:
    p = build_turn_prompt_from_state_dict(_state("blue"), Team.BLUE)
    assert "your (blue) turn" in p
    assert "WARNING" not in p


def test_bootstrap_prompt_has_full_snapshot_delta_doesnt() -> None:
    """Turn 1 sends a full state snapshot so the model can form a
    mental map. Turn 2+ sends only delta — previous turn's snapshot
    is already in the session, re-shipping it every turn was the
    main driver behind the 351k-token context blow-up."""
    s = _state("blue", turn=4)
    s["units"] = [
        {"id": "u_b_knight_1", "owner": "blue", "class": "knight",
         "pos": {"x": 0, "y": 0}, "hp": 22, "hp_max": 22,
         "status": "ready", "alive": True},
    ]

    bootstrap = build_turn_prompt_from_state_dict(
        s, Team.BLUE, is_first_turn=True
    )
    delta = build_turn_prompt_from_state_dict(
        s, Team.BLUE, is_first_turn=False, new_history=[]
    )

    # Bootstrap carries the JSON snapshot block.
    assert "board" in bootstrap
    assert "```json" in bootstrap

    # Delta is a text list — no JSON dump.
    assert "```json" not in delta
    # Delta is substantially shorter.
    assert len(delta) < len(bootstrap) // 2, (
        f"delta not shorter than bootstrap: {len(delta)} vs {len(bootstrap)}"
    )
    # Delta still tells the model it's their turn and names the team.
    assert "your (blue) turn" in delta


def test_delta_prompt_includes_opponent_actions() -> None:
    """When opponent acted since our last turn, the delta prompt
    lists what they did — that's the whole point of the delta."""
    s = _state("blue", turn=3)
    s["units"] = []
    history = [
        {"type": "move", "unit_id": "u_r_speedboat_1",
         "dest": {"x": 5, "y": 3}},
        {"type": "attack", "unit_id": "u_r_missile_1",
         "target_id": "u_b_destroyer_1",
         "damage_dealt": 8, "counter_damage": 3,
         "target_killed": False, "attacker_killed": False},
        {"type": "end_turn", "by": "red"},
    ]
    p = build_turn_prompt_from_state_dict(
        s, Team.BLUE, is_first_turn=False, new_history=history
    )
    assert "Opponent actions" in p
    assert "u_r_speedboat_1 moved to (5, 3)" in p
    assert "u_r_missile_1 attacked u_b_destroyer_1" in p
    assert "damage=8" in p


def test_delta_prompt_notes_when_no_opponent_actions() -> None:
    """First turn for second_player: no opponent actions yet, but
    we're still entering the delta path (is_first_turn=False can't
    happen on turn 1 for us, but defensive)."""
    p = build_turn_prompt_from_state_dict(
        _state("blue", turn=2),
        Team.BLUE,
        is_first_turn=False,
        new_history=[],
    )
    assert "Opponent did not act" in p


def test_turn_prompt_warns_when_not_your_turn() -> None:
    """If someone calls the prompt builder with a state that says
    it's red's turn but viewer=blue, the output must tell the model
    about the mismatch — not silently claim it's blue's turn."""
    p = build_turn_prompt_from_state_dict(_state("red"), Team.BLUE)
    assert "WARNING" in p
    assert "not_your_turn" in p
    # The downstream instruction should tell the model NOT to act.
    assert "Do NOT call" in p


def test_networked_agent_skips_turn_when_not_active() -> None:
    """play_turn must bail out when fresh state shows it's not the
    viewer's turn — no tool calls, no adapter invocation."""
    from silicon_pantheon.client.agent_bridge import NetworkedAgent

    tool_calls: list[tuple[str, dict]] = []
    adapter_calls: list[dict] = []

    class _StubClient:
        async def call(self, tool: str, **kw):
            tool_calls.append((tool, kw))
            if tool == "get_state":
                # Fresh state says RED is active — we are BLUE.
                return {
                    "ok": True,
                    "result": _state("red"),
                }
            if tool == "describe_scenario":
                return {"ok": True}
            return {"ok": True, "result": {}}

    class _StubAdapter:
        async def play_turn(self, **kwargs):
            adapter_calls.append(kwargs)

        async def close(self) -> None:
            return None

    agent = NetworkedAgent(
        client=_StubClient(),
        model="fake",
        scenario="01_tiny_skirmish",
        adapter=_StubAdapter(),
    )

    asyncio.run(agent.play_turn(Team.BLUE, max_turns=10))
    asyncio.run(agent.close())

    # The adapter must not have been invoked — that would have
    # spent tokens on a prompt the server would reject every call of.
    assert adapter_calls == [], (
        "adapter was invoked despite it not being the viewer's turn"
    )
    # We're allowed to call get_state (that's how we learned it's
    # not our turn) but no action tools.
    action_tools = {"move", "attack", "heal", "wait", "end_turn",
                    "get_legal_actions", "simulate_attack"}
    calls_made = {t for t, _ in tool_calls}
    assert calls_made.isdisjoint(action_tools), (
        f"action tools called during non-active turn: "
        f"{calls_made & action_tools}"
    )


def test_networked_agent_skips_turn_when_game_over() -> None:
    """Game already ended → no prompt, no adapter call."""
    from silicon_pantheon.client.agent_bridge import NetworkedAgent

    adapter_calls: list[dict] = []

    class _StubClient:
        async def call(self, tool: str, **kw):
            if tool == "get_state":
                s = _state("blue")
                s["status"] = "game_over"
                return {"ok": True, "result": s}
            return {"ok": True, "result": {}}

    class _StubAdapter:
        async def play_turn(self, **kwargs):
            adapter_calls.append(kwargs)

        async def close(self) -> None:
            return None

    agent = NetworkedAgent(
        client=_StubClient(),
        model="fake",
        scenario="01_tiny_skirmish",
        adapter=_StubAdapter(),
    )

    asyncio.run(agent.play_turn(Team.BLUE, max_turns=10))
    asyncio.run(agent.close())

    assert adapter_calls == []
