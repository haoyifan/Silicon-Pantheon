"""Agent prompt content — scenario context in the system prompt,
slim per-unit fields in the per-turn + tool responses."""

from __future__ import annotations

from silicon_pantheon.harness.prompts import (
    build_system_prompt,
    build_turn_prompt_from_state_dict,
    _slim_unit,
)
from silicon_pantheon.server.engine.state import Team


def _fake_bundle() -> dict:
    return {
        "name": "Test Map",
        "description": "a little test map.",
        "armies": {
            "blue": [{"class": "hero", "pos": {"x": 1, "y": 1}}],
            "red": [{"class": "boss", "pos": {"x": 8, "y": 8}}],
        },
        "unit_classes": {
            "hero": {"display_name": "The Hero",
                     "description": "Test hero.",
                     "hp_max": 30, "atk": 10, "defense": 5, "res": 3,
                     "spd": 6, "rng_min": 1, "rng_max": 1, "move": 4,
                     "glyph": "H"},
            "boss": {"display_name": "The Boss",
                     "description": "Test boss.",
                     "hp_max": 40, "atk": 12, "defense": 7, "res": 4,
                     "spd": 5, "rng_min": 1, "rng_max": 1, "move": 3,
                     "glyph": "B"},
        },
        "terrain_types": {
            "plain": {"description": "No modifiers."},
            "swamp": {"move_cost": 2, "heals": -2, "description": "Hurts."},
        },
        "board": {
            "width": 10, "height": 10,
            "terrain": [{"x": 3, "y": 3, "type": "swamp"}],
            "forts": [{"x": 9, "y": 9, "owner": "red"}],
        },
        "win_conditions": [
            {"type": "protect_unit", "unit_id": "u_b_hero_1",
             "owning_team": "blue"},
            {"type": "eliminate_all_enemy_units"},
        ],
    }


def test_system_prompt_carries_scenario_specific_context():
    sp = build_system_prompt(
        team=Team.BLUE, max_turns=30, strategy=None, lessons=None,
        scenario_description=_fake_bundle(),
    )
    # Scenario name and story.
    assert "Test Map" in sp
    assert "a little test map" in sp
    # Classes with display names appear, not generic Knight/Archer.
    assert "The Hero" in sp
    assert "The Boss" in sp
    assert "Knight" not in sp  # old hardcoded Knight/Archer paragraph gone
    # Stats rendered.
    assert "HP 30" in sp and "HP 40" in sp
    # Terrain catalog includes custom types with effect summaries.
    assert "swamp" in sp
    assert "move 2" in sp
    # Win conditions side-explicit.
    assert "Red wins" in sp or "Either side wins" in sp
    # Map grid contains at least one unit glyph at a plausible row.
    assert "H" in sp
    # The describe_class escape hatch is documented.
    assert "describe_class" in sp


def test_system_prompt_teaches_batching_contract():
    """The Tool call batching rule is load-bearing — without it the
    model won't understand why its parallel mutations are dropped.
    A future refactor could accidentally lose the section. Pin it."""
    sp = build_system_prompt(
        team=Team.BLUE, max_turns=20, strategy=None, lessons=None,
        scenario_description=_fake_bundle(),
    )
    assert "Tool call batching rule" in sp
    # Both halves of the contract must be present.
    assert "Unlimited READ calls" in sp
    assert "ONE mutating call" in sp
    # Canonical mutation list must be named — if a new mutation is
    # added to GAME_TOOLS, the prompt here should also list it or
    # the model won't know it's capped.
    for tool in ("move", "attack", "heal", "wait", "end_turn"):
        assert f"`{tool}`" in sp, f"{tool} missing from batching rule"


def test_how_to_pace_section_is_merged_not_duplicated():
    """Regression: we used to have a separate 'How to pace your turn'
    section that said much the same thing as the batching rule. Two
    adjacent sections teaching the same rule with different examples
    is confusing. If that heading comes back, something regressed."""
    sp = build_system_prompt(
        team=Team.BLUE, max_turns=20, strategy=None, lessons=None,
        scenario_description=_fake_bundle(),
    )
    assert "How to pace your turn" not in sp


def test_get_coach_messages_cued_to_turn_start_prompt():
    """Step 1 should tie `get_coach_messages` to the specific
    start-of-turn user message, not a generic 'start of your turn'
    which a literal-minded model might re-interpret on retries."""
    sp = build_system_prompt(
        team=Team.BLUE, max_turns=20, strategy=None, lessons=None,
        scenario_description=_fake_bundle(),
    )
    # The "It is turn N..." trigger must be explicit.
    assert "It is turn N" in sp
    # And the anti-cue on continuation/retry messages must be
    # explicit so the model doesn't re-drain coach messages on
    # every retry.
    assert "CONTINUATION" in sp or "continuation" in sp


def test_turn_prompt_mismatch_warning_prepends_when_not_my_turn():
    """Defensive: if build_turn_prompt_from_state_dict is called
    with a state that says active_player != viewer (e.g. a stale
    poll), we prepend a warning block so the model doesn't emit
    mutations that'd get rejected with 'not_your_turn' errors."""
    state = {
        "turn": 4,
        "active_player": "red",  # stale — blue shouldn't act
        "you": "blue",
        "board": {"width": 4, "height": 4, "forts": []},
        "units": [],
        "last_action": None,
    }
    p = build_turn_prompt_from_state_dict(state, Team.BLUE, is_first_turn=False)
    assert "WARNING" in p
    assert "active_player does not match" in p


def test_system_prompt_survives_empty_scenario_bundle():
    """If describe_scenario failed, the system prompt should still
    render without blowing up — we don't want a prompt error to
    kill the agent session."""
    sp = build_system_prompt(
        team=Team.BLUE, max_turns=20, strategy=None, lessons=None,
        scenario_description=None,
    )
    assert "unknown scenario" in sp or "(no scenario description" in sp


def test_slim_unit_keeps_combat_fields_drops_class_invariants():
    full_unit = {
        "id": "u_b_x_1", "owner": "blue", "class": "x", "pos": {"x": 1, "y": 2},
        "hp": 10, "hp_max": 30, "atk": 8, "def": 5, "res": 3,
        "spd": 6, "move": 4, "rng": [1, 1],
        "status": "ready", "alive": True,
        "is_magic": False, "can_heal": False,
        # Noise that shouldn't reach the agent.
        "display_name": "X", "glyph": "X", "color": "cyan",
        "description": "flavor text", "art_frames": ["frame"],
        "tags": ["hero"], "mp_max": 0, "mp_per_turn": 0,
        "abilities": [], "default_inventory": [],
        "damage_profile": {}, "defense_profile": {},
        "bonus_vs_tags": [], "vulnerability_to_tags": [],
    }
    slim = _slim_unit(full_unit)
    kept = set(slim.keys())
    assert "id" in kept and "hp" in kept and "pos" in kept and "status" in kept
    for junk in ("display_name", "glyph", "color", "description",
                 "art_frames", "tags", "mp_max", "abilities",
                 "damage_profile", "bonus_vs_tags"):
        assert junk not in kept


def test_turn_prompt_only_carries_dynamic_state():
    state_dict = {
        "turn": 3, "active_player": "blue", "you": "blue",
        "board": {"width": 10, "height": 10, "forts": []},
        "units": [
            {"id": "u_b_x_1", "owner": "blue", "class": "x",
             "pos": {"x": 1, "y": 1},
             "hp": 10, "hp_max": 30, "atk": 8, "def": 5, "res": 3,
             "spd": 6, "move": 4, "rng": [1, 1],
             "status": "ready", "alive": True,
             "is_magic": False, "can_heal": False,
             "display_name": "X", "art_frames": ["bloat"],
             "description": "should not appear"},
        ],
        "last_action": None,
    }
    p = build_turn_prompt_from_state_dict(state_dict, Team.BLUE)
    assert "hp" in p
    assert "art_frames" not in p
    assert "should not appear" not in p


def test_retry_prompt_is_continuation_not_start_of_turn():
    """Regression: on no_progress retries, shipping the normal
    TURN_PROMPT_TEMPLATE_DELTA ("It is turn N and it is your turn
    to play") caused the model to re-run its "step 1: call
    get_coach_messages" routine, producing 34 coach-messages calls
    across 12 real turns. The retry prompt must frame the message
    as a CONTINUATION so the model resumes instead of restarting."""
    state = {
        "turn": 5,
        "active_player": "blue",
        "you": "blue",
        "board": {"width": 10, "height": 10, "forts": []},
        "units": [
            {"id": "u_b_h_1", "owner": "blue", "class": "hero",
             "pos": {"x": 1, "y": 1}, "hp": 30, "hp_max": 30,
             "status": "ready", "alive": True},
            {"id": "u_b_h_2", "owner": "blue", "class": "hero",
             "pos": {"x": 2, "y": 1}, "hp": 30, "hp_max": 30,
             "status": "done", "alive": True},
        ],
        "last_action": None,
    }
    # retry_n > 0 → continuation framing.
    p = build_turn_prompt_from_state_dict(state, Team.BLUE, retry_n=1)
    # Must NOT say "turn N and it is your turn to play" — that's
    # the start-of-turn phrasing that confuses the model.
    assert "it is your (blue) turn to play" not in p
    # Must explicitly be a continuation.
    assert "CONTINUATION" in p
    assert "turn 5" in p
    # Must tell the model NOT to re-drain coach messages — the
    # whole point of this prompt is to suppress that routine.
    assert "get_coach_messages" in p
    assert "do NOT" in p.lower() or "not" in p.lower()
    # Units section still present so the model knows what's left.
    assert "u_b_h_1" in p
    assert "ready" in p

    # retry_n=0 → normal delta path (no continuation framing).
    p0 = build_turn_prompt_from_state_dict(
        state, Team.BLUE, is_first_turn=False, retry_n=0,
    )
    assert "CONTINUATION" not in p0
    assert "it is your (blue) turn to play" in p0


def test_slim_tool_response_drops_board_tiles_from_get_state():
    """Regression: get_state's board.tiles array (180+ entries on
    an 18x10 board, ~5KB per call) was the dominant within-turn
    token sink. Agents call get_state many times per turn and the
    terrain map is invariant — they have it from the system prompt's
    map_grid. _slim_tool_response must drop it for the agent
    payload while keeping it intact for the TUI's own get_state
    call path (which doesn't go through this slimmer)."""
    from silicon_pantheon.client.agent_bridge import _slim_tool_response

    raw = {
        "turn": 3,
        "active_player": "blue",
        "you": "blue",
        "board": {
            "width": 18,
            "height": 10,
            "tiles": [{"x": x, "y": y, "type": "plain"} for x in range(18) for y in range(10)],
            "forts": [{"x": 0, "y": 0, "owner": "blue"}],
        },
        "units": [
            {"id": "u_b_x_1", "owner": "blue", "class": "knight",
             "pos": {"x": 1, "y": 1}, "hp": 22, "hp_max": 22,
             "status": "ready", "alive": True},
        ],
        "_visible_tiles": [[0, 0], [1, 1]],
    }
    slim = _slim_tool_response("get_state", raw)
    # Tiles dropped → savings.
    assert "tiles" not in slim["board"], (
        "board.tiles must be dropped from agent-bound get_state — "
        "it's invariant during a match and the system prompt's "
        "map_grid already shows it"
    )
    # Forts and dimensions retained.
    assert slim["board"]["width"] == 18
    assert slim["board"]["forts"]
    # Visibility annotation also dropped.
    assert "_visible_tiles" not in slim
    # Other state still there.
    assert slim["turn"] == 3
    assert slim["active_player"] == "blue"
    assert slim["units"]
