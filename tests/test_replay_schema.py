"""Tests for replay-event parsing and action reconstruction."""

from __future__ import annotations

import pytest

from silicon_pantheon.match.replay_schema import (
    AgentThought,
    CoachMessage,
    ErrorPayload,
    ForcedEndTurn,
    MatchStart,
    UnreconstructibleAction,
    action_from_payload,
    parse_event,
)
from silicon_pantheon.server.engine.rules import (
    AttackAction,
    EndTurnAction,
    HealAction,
    MoveAction,
    WaitAction,
)


def test_parse_match_start() -> None:
    ev = parse_event(
        {
            "kind": "match_start",
            "turn": 1,
            "payload": {
                "scenario": "01_tiny_skirmish",
                "max_turns": 20,
                "first_player": "blue",
            },
        }
    )
    assert ev.kind == "match_start"
    assert isinstance(ev.data, MatchStart)
    assert ev.data.scenario == "01_tiny_skirmish"
    assert ev.data.max_turns == 20
    assert ev.data.first_player == "blue"


def test_parse_agent_thought() -> None:
    ev = parse_event(
        {
            "kind": "agent_thought",
            "turn": 3,
            "payload": {"team": "red", "text": "rush the mage", "turn": 3},
        }
    )
    assert isinstance(ev.data, AgentThought)
    assert ev.data.team == "red"
    assert ev.data.text == "rush the mage"
    assert ev.turn == 3


def test_parse_action_keeps_raw_dict() -> None:
    ev = parse_event(
        {
            "kind": "action",
            "turn": 2,
            "payload": {"type": "move", "unit_id": "u_b_archer_1", "dest": {"x": 2, "y": 1}},
        }
    )
    assert ev.kind == "action"
    assert isinstance(ev.data, dict)
    assert ev.data["type"] == "move"


def test_parse_coach_message() -> None:
    ev = parse_event(
        {
            "kind": "coach_message",
            "turn": 1,
            "payload": {"to": "blue", "text": "hold"},
        }
    )
    assert isinstance(ev.data, CoachMessage)
    assert ev.data.to == "blue"


def test_parse_forced_end_turn() -> None:
    ev = parse_event({"kind": "forced_end_turn", "turn": 5, "payload": {"team": "red"}})
    assert isinstance(ev.data, ForcedEndTurn)


def test_parse_error_variants() -> None:
    for kind in ("agent_error", "summarize_error", "lessons_load_error"):
        ev = parse_event(
            {"kind": kind, "turn": 1, "payload": {"team": "blue", "error": "boom"}}
        )
        assert isinstance(ev.data, ErrorPayload)
        assert ev.data.error == "boom"


def test_parse_unknown_kind_returns_raw() -> None:
    ev = parse_event({"kind": "some_future_thing", "turn": 0, "payload": {"x": 1}})
    assert ev.kind == "some_future_thing"
    assert ev.data == {"x": 1}


def test_parse_missing_payload_is_safe() -> None:
    ev = parse_event({"kind": "forced_end_turn", "turn": 1})
    assert isinstance(ev.data, ForcedEndTurn)
    assert ev.data.team == ""


def test_action_from_payload_move() -> None:
    a = action_from_payload(
        {"type": "move", "unit_id": "u_b_archer_1", "dest": {"x": 2, "y": 1}}
    )
    assert isinstance(a, MoveAction)
    assert a.unit_id == "u_b_archer_1"
    assert a.dest.x == 2 and a.dest.y == 1


def test_action_from_payload_attack() -> None:
    a = action_from_payload(
        {"type": "attack", "unit_id": "u_b_knight_1", "target_id": "u_r_archer_1"}
    )
    assert isinstance(a, AttackAction)


def test_action_from_payload_heal() -> None:
    a = action_from_payload(
        {"type": "heal", "healer_id": "u_b_mage_1", "target_id": "u_b_knight_1"}
    )
    assert isinstance(a, HealAction)
    assert a.healer_id == "u_b_mage_1"


def test_action_from_payload_wait_and_end_turn() -> None:
    assert isinstance(
        action_from_payload({"type": "wait", "unit_id": "u_r_cavalry_1"}),
        WaitAction,
    )
    assert isinstance(action_from_payload({"type": "end_turn"}), EndTurnAction)


def test_action_from_payload_rejects_unknown() -> None:
    with pytest.raises(UnreconstructibleAction):
        action_from_payload({"type": "teleport", "unit_id": "x"})


def test_heal_payload_uses_unit_id_not_healer_id():
    """Regression: silicon-play crashed on every replay containing a
    heal because the engine records heal results with the healer's id
    under `unit_id` (rules.py:_apply_heal), not `healer_id`. The
    parser was unconditionally reading `payload["healer_id"]` and
    raising KeyError."""
    from silicon_pantheon.shared.replay_schema import (
        HealAction,
        action_from_payload,
    )

    # Real-world shape from the engine.
    a = action_from_payload({
        "type": "heal",
        "unit_id": "u_b_mage_1",
        "target_id": "u_b_knight_1",
        "heal_amount": 5,
    })
    assert isinstance(a, HealAction)
    assert a.healer_id == "u_b_mage_1"
    assert a.target_id == "u_b_knight_1"

    # Backward-compatible: hand-written tests / hypothetical older
    # replays might use the explicit healer_id field.
    a2 = action_from_payload({
        "type": "heal",
        "healer_id": "u_b_mage_1",
        "target_id": "u_b_knight_1",
    })
    assert isinstance(a2, HealAction)
    assert a2.healer_id == "u_b_mage_1"


def test_agent_thought_roundtrip_through_session_log(tmp_path):
    """Pin the full path: Session.add_thought writes an `agent_thought`
    event to the replay file, and interactive_replay's parser reads
    it back as a renderable AgentThought. This is the new
    networked-replay reasoning capture path — see record_thought
    tool in server/game_tools.py."""
    from silicon_pantheon.server.engine.scenarios import load_scenario
    from silicon_pantheon.server.engine.state import Team
    from silicon_pantheon.server.session import new_session
    from silicon_pantheon.shared.replay_schema import (
        AgentThought,
        parse_event,
    )
    import json

    replay_path = tmp_path / "replay.jsonl"
    state = load_scenario("01_tiny_skirmish")
    session = new_session(
        state, replay_path=replay_path, scenario="01_tiny_skirmish"
    )
    session.add_thought(Team.BLUE, "I should attack the knight at (3,3).")
    session.add_thought(Team.RED, "Counter with the archer.")

    raw_lines = replay_path.read_text(encoding="utf-8").splitlines()
    thought_events = [parse_event(json.loads(ln)) for ln in raw_lines if "agent_thought" in ln]
    assert len(thought_events) == 2, f"expected 2 agent_thought events, got {len(thought_events)}"
    assert all(ev.kind == "agent_thought" for ev in thought_events)
    blue_t, red_t = thought_events
    assert isinstance(blue_t.data, AgentThought)
    assert blue_t.data.team == "blue"
    assert "knight" in blue_t.data.text
    assert isinstance(red_t.data, AgentThought)
    assert red_t.data.team == "red"
