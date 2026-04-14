"""Narrative parsing + engine-driven firing."""

from __future__ import annotations

from clash_of_odin.server.engine.narrative import parse_narrative, fire
from clash_of_odin.server.engine.rules import EndTurnAction, apply
from clash_of_odin.server.engine.scenarios import build_state


def _base_cfg() -> dict:
    return {
        "board": {
            "width": 4, "height": 4,
            "terrain": [], "forts": [],
        },
        "armies": {
            "blue": [{"class": "knight", "pos": {"x": 0, "y": 1}}],
            "red": [{"class": "knight", "pos": {"x": 3, "y": 3}}],
        },
        "rules": {"max_turns": 30, "first_player": "blue"},
    }


def test_parse_narrative_absent_block_defaults_to_empty():
    n = parse_narrative({})
    assert n.title == ""
    assert n.events == []


def test_parse_narrative_reads_all_fields():
    n = parse_narrative({
        "narrative": {
            "title": "T", "description": "D", "intro": "I",
            "events": [
                {"trigger": "on_turn_start", "turn": 3, "text": "hi"},
            ],
        }
    })
    assert n.title == "T"
    assert n.intro == "I"
    assert len(n.events) == 1
    assert n.events[0].turn == 3


def test_narrative_events_written_to_replay(tmp_path):
    """F.6: narrative_event lines appear in the JSONL replay stream."""
    import json

    from clash_of_odin.server.engine.replay import ReplayWriter
    from clash_of_odin.server.session import Session
    from clash_of_odin.server.tools import end_turn
    from clash_of_odin.server.engine.state import Team

    cfg = _base_cfg()
    cfg["narrative"] = {
        "events": [{"trigger": "on_turn_start", "turn": 1, "text": "Go!"}],
    }
    state = build_state(cfg)
    writer = ReplayWriter(tmp_path / "replay.jsonl")
    session = Session(state=state, replay=writer)
    end_turn(session, Team.BLUE)  # blue ends → red starts turn 1
    writer.close()
    lines = [json.loads(ln) for ln in (tmp_path / "replay.jsonl").read_text().splitlines()]
    kinds = [ln["kind"] for ln in lines]
    assert "narrative_event" in kinds
    nev = next(ln for ln in lines if ln["kind"] == "narrative_event")
    assert nev["payload"]["text"] == "Go!"


def test_on_turn_start_fires_once_at_the_right_turn():
    cfg = _base_cfg()
    cfg["narrative"] = {
        "events": [
            {"trigger": "on_turn_start", "turn": 2, "text": "Day two dawns"},
        ],
    }
    state = build_state(cfg)
    # Turn 1 → end blue → red starts turn 1. End red → blue starts turn 2.
    apply(state, EndTurnAction())
    assert state._narrative_log == []
    apply(state, EndTurnAction())
    assert len(state._narrative_log) == 1
    assert state._narrative_log[0]["text"] == "Day two dawns"
    # Second pass: doesn't fire again.
    prev_len = len(state._narrative_log)
    apply(state, EndTurnAction())
    apply(state, EndTurnAction())
    assert len(state._narrative_log) == prev_len
