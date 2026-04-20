"""Fog-of-war invariants and filter round-trips.

The filter is the single audit surface for "no info leak"; these
tests pin the invariants listed in viewer_filter.py's docstring.

Several tests place units by mutation rather than relying on a
scenario's stock positions — this isolates us from changes to
scenario geometry or the sight-stat values.
"""

from __future__ import annotations

from silicon_pantheon.server.engine.scenarios import load_scenario
from silicon_pantheon.server.engine.state import GameState, Pos, Team
from silicon_pantheon.shared.fog import visible_tiles
from silicon_pantheon.shared.viewer_filter import (
    ViewerContext,
    currently_visible,
    filter_history,
    filter_state,
    filter_threat_map,
    filter_unit,
    update_ever_seen,
)


def _ctx(team: Team, mode: str, ever_seen=frozenset()):  # noqa: ANN001
    return ViewerContext(team=team, fog_mode=mode, ever_seen=ever_seen)  # type: ignore[arg-type]


def _spread_out(state: GameState) -> None:
    """Shove blue units into a corner and red into the opposite corner
    so they can't see each other regardless of sight stats."""
    w, h = state.board.width, state.board.height
    blue_positions = [Pos(0, 0), Pos(0, 1), Pos(1, 0)]
    red_positions = [Pos(w - 1, h - 1), Pos(w - 1, h - 2), Pos(w - 2, h - 1)]
    blue_iter = iter(blue_positions)
    red_iter = iter(red_positions)
    for u in list(state.units.values()):
        target = (
            next(blue_iter) if u.owner is Team.BLUE else next(red_iter)
        )
        u.pos = target


def test_visible_tiles_includes_own_units_tiles() -> None:
    state = load_scenario("01_tiny_skirmish")
    vis = visible_tiles(state, Team.BLUE)
    for u in state.units_of(Team.BLUE):
        assert u.pos in vis


def test_none_mode_is_identity() -> None:
    state = load_scenario("01_tiny_skirmish")
    ctx = _ctx(Team.BLUE, "none")
    filtered = filter_state(state, ctx)
    # In none mode, every unit on both teams is present.
    owners = {u["owner"] for u in filtered["units"]}
    assert owners == {"blue", "red"}


def test_classic_hides_out_of_sight_enemies() -> None:
    state = load_scenario("01_tiny_skirmish")
    _spread_out(state)
    ctx = _ctx(Team.BLUE, "classic")
    filtered = filter_state(state, ctx)
    owners = {u["owner"] for u in filtered["units"]}
    assert "blue" in owners
    assert "red" not in owners  # invisible when on opposite corners


def test_classic_ever_seen_reveals_past_terrain_not_units() -> None:
    state = load_scenario("01_tiny_skirmish")
    _spread_out(state)
    # Pretend blue's team once saw every tile on the board.
    all_tiles = frozenset(
        Pos(x, y) for x in range(state.board.width) for y in range(state.board.height)
    )
    ctx = _ctx(Team.BLUE, "classic", ever_seen=all_tiles)
    filtered = filter_state(state, ctx)
    # All tile terrain should be revealed (no "unknown" masks).
    types = {t["type"] for t in filtered["board"]["tiles"]}
    assert "unknown" not in types
    # But units out of current sight still hidden.
    owners = {u["owner"] for u in filtered["units"]}
    assert "red" not in owners


def test_line_of_sight_masks_more_than_classic() -> None:
    state = load_scenario("01_tiny_skirmish")
    # Build an ever_seen that mimics a few turns of exploration for classic.
    ever_seen = update_ever_seen(state, Team.BLUE, frozenset())

    classic = filter_state(state, _ctx(Team.BLUE, "classic", ever_seen=ever_seen))
    los = filter_state(state, _ctx(Team.BLUE, "line_of_sight"))

    known_classic = {
        (t["x"], t["y"]) for t in classic["board"]["tiles"] if t["type"] != "unknown"
    }
    known_los = {
        (t["x"], t["y"]) for t in los["board"]["tiles"] if t["type"] != "unknown"
    }
    # Classic reveals at least everything line-of-sight does.
    assert known_los <= known_classic


def test_filter_unit_hides_enemy_out_of_sight() -> None:
    state = load_scenario("01_tiny_skirmish")
    _spread_out(state)
    ctx = _ctx(Team.BLUE, "classic")
    # Pick a red unit.
    red = next(iter(state.units_of(Team.RED)))
    u_dict = {
        "id": red.id,
        "owner": red.owner.value,
        "pos": {"x": red.pos.x, "y": red.pos.y},
    }
    assert filter_unit(red.id, u_dict, state, ctx) is None


def test_filter_unit_shows_own_team() -> None:
    state = load_scenario("01_tiny_skirmish")
    ctx = _ctx(Team.BLUE, "classic")
    blue = next(iter(state.units_of(Team.BLUE)))
    u_dict = {
        "id": blue.id,
        "owner": blue.owner.value,
        "pos": {"x": blue.pos.x, "y": blue.pos.y},
    }
    assert filter_unit(blue.id, u_dict, state, ctx) is not None


def test_filter_threat_map_drops_invisible_enemies() -> None:
    state = load_scenario("01_tiny_skirmish")
    _spread_out(state)
    ctx = _ctx(Team.BLUE, "classic")
    # Synthesize a threat map where every tile is threatened by one red unit
    # that's out of blue's sight.
    red = next(iter(state.units_of(Team.RED)))
    fake = {"threats": {"0,0": [red.id], "1,1": [red.id]}}
    out = filter_threat_map(fake, state, ctx)
    # Nothing should survive because the enemy is invisible.
    assert out == {"threats": {}}


def test_currently_visible_matches_visible_tiles() -> None:
    state = load_scenario("01_tiny_skirmish")
    ctx = _ctx(Team.BLUE, "classic")
    assert currently_visible(state, ctx) == visible_tiles(state, Team.BLUE)


def test_filter_history_drops_hidden_enemy_actions() -> None:
    """Regression: get_history was not fog-filtered, so the delta
    turn prompt surfaced enemy actions (with unit_ids + destination
    tiles) that fog should have hidden. Leaks both the existence of
    the unit and its movement pattern."""
    state = load_scenario("01_tiny_skirmish")
    _spread_out(state)
    ctx = _ctx(Team.BLUE, "classic")
    red = next(iter(state.units_of(Team.RED)))

    history = {
        "history": [
            # Our own action — must always pass through.
            {"type": "move", "unit_id": "u_b_knight_1",
             "dest": {"x": 0, "y": 0}},
            # Enemy action on a hidden tile — must be dropped.
            {"type": "move", "unit_id": red.id,
             "dest": {"x": state.board.width - 1, "y": state.board.height - 1}},
            # end_turn: carries no position, always passes.
            {"type": "end_turn", "by": "red"},
        ],
        "last_action": {"type": "move", "unit_id": red.id,
                        "dest": {"x": state.board.width - 1,
                                 "y": state.board.height - 1}},
    }
    out = filter_history(history, state, ctx)
    kept_types = [(e.get("type"), e.get("unit_id")) for e in out["history"]]
    assert ("move", "u_b_knight_1") in kept_types, "own action missing"
    assert ("move", red.id) not in kept_types, (
        "hidden enemy move leaked through — " + str(kept_types)
    )
    assert ("end_turn", None) in kept_types, "end_turn should pass through"
    # Redacted last_action since its actor isn't visible.
    assert out["last_action"] is None


def test_filter_history_passes_through_when_fog_none() -> None:
    """fog=none short-circuit."""
    state = load_scenario("01_tiny_skirmish")
    ctx = _ctx(Team.BLUE, "none")
    history = {
        "history": [
            {"type": "move", "unit_id": "u_r_knight_1",
             "dest": {"x": 99, "y": 99}},
        ],
        "last_action": None,
    }
    assert filter_history(history, state, ctx) is history


# ---- attack-path fog enforcement ----


def test_attack_blocked_against_fog_hidden_enemy() -> None:
    """Under fog=classic, an agent that has memorised an enemy's unit ID
    (from the scenario prompt, an earlier state snapshot before fog,
    history, etc.) must NOT be able to attack it by ID alone when it's
    currently out of sight. Without this enforcement, fog would be a
    one-way filter that offense bypasses.

    Regression guard for the 20_battle_of_hoth report: red attacked
    "韦奇·安蒂列斯" even though the user expected fog to hide it.
    """
    from silicon_pantheon.server.session import new_session
    from silicon_pantheon.server.tools.mutations import attack
    from silicon_pantheon.server.tools._common import ToolError

    state = load_scenario("01_tiny_skirmish")
    _spread_out(state)  # blue corner, red opposite corner
    state.active_player = Team.BLUE
    session = new_session(state, fog_of_war="classic")
    # Force a blue unit into a position where the FIRST red unit is
    # guaranteed not in sight. _spread_out already handles this.
    any_red = next(iter(state.units_of(Team.RED)))
    any_blue = next(iter(u for u in state.units_of(Team.BLUE) if u.alive))
    # Force statuses so _require_active / _require_own_unit pass.
    from silicon_pantheon.server.engine.state import UnitStatus
    any_blue.status = UnitStatus.READY

    try:
        attack(session, Team.BLUE, any_blue.id, any_red.id)
    except ToolError as e:
        assert "not visible" in str(e), f"unexpected ToolError: {e}"
    else:
        raise AssertionError(
            "attack on fog-hidden enemy should have been rejected with ToolError"
        )


def test_fog_audit_detects_hidden_id_in_response(caplog) -> None:
    """The audit helper must flag any hidden-enemy unit_id that leaks
    through a filtered response, so we can chase down which tool
    forgot to redact it. We synthesize a response that leaks a
    hidden enemy ID and assert the audit logs a WARNING."""
    import logging as _logging
    from silicon_pantheon.server.session import new_session
    from silicon_pantheon.server.tools._common import audit_response_for_fog_leaks

    state = load_scenario("01_tiny_skirmish")
    _spread_out(state)
    session = new_session(state, fog_of_war="classic")
    any_red = next(iter(state.units_of(Team.RED)))
    # Build a fake response that (incorrectly) includes the hidden
    # enemy's ID — mimicking a bug where a tool forgot to redact.
    leaky_response = {
        "some_field": {
            "nested": {
                "enemy_ref": any_red.id,  # hidden under fog for blue
            },
            "list": [
                {"unit_id": any_red.id},
            ],
        },
    }
    caplog.set_level(_logging.WARNING, logger="silicon.fog")
    audit_response_for_fog_leaks(leaky_response, session, Team.BLUE, "my_bogus_tool")
    messages = [r.message for r in caplog.records if r.name == "silicon.fog"]
    assert any("fog_leak_suspect" in m for m in messages), (
        f"expected fog_leak_suspect WARNING; saw: {messages}"
    )
    assert any("my_bogus_tool" in m for m in messages), (
        f"WARNING should name the tool; saw: {messages}"
    )


def test_fog_audit_silent_on_clean_response() -> None:
    """The audit must NOT log WARNING when the response is clean (only
    references own-team units and dead enemies)."""
    import logging as _logging
    from silicon_pantheon.server.session import new_session
    from silicon_pantheon.server.tools._common import audit_response_for_fog_leaks

    state = load_scenario("01_tiny_skirmish")
    _spread_out(state)
    session = new_session(state, fog_of_war="classic")
    any_blue = next(iter(state.units_of(Team.BLUE)))
    clean_response = {
        "your_units": [{"id": any_blue.id}],
    }
    # Reset the silicon.fog handler so we can observe emissions.
    logger = _logging.getLogger("silicon.fog")
    # Count WARNING records emitted while calling the audit.
    class _Counter(_logging.Handler):
        def __init__(self):
            super().__init__(level=_logging.WARNING)
            self.count = 0

        def emit(self, record):
            if record.name == "silicon.fog" and record.levelno >= _logging.WARNING:
                self.count += 1

    counter = _Counter()
    logger.addHandler(counter)
    try:
        audit_response_for_fog_leaks(
            clean_response, session, Team.BLUE, "some_tool"
        )
    finally:
        logger.removeHandler(counter)
    assert counter.count == 0, (
        "audit should not warn when response only references own-team"
    )


def test_attack_log_only_mode_lets_hidden_attack_through(monkeypatch, caplog) -> None:
    """Repro mode: SILICON_FOG_ATTACK_ENFORCE=0 turns the fog gate
    into log-only. The attack must NOT raise, and a
    `fog_violation_allowed` WARNING must appear so the leak is
    visible in the log. Used by operators to reproduce reported
    fog bugs without altering agent behaviour."""
    import logging as _logging
    from silicon_pantheon.server.engine.state import UnitStatus
    from silicon_pantheon.server.session import new_session
    from silicon_pantheon.server.tools.mutations import attack

    monkeypatch.setenv("SILICON_FOG_ATTACK_ENFORCE", "0")

    state = load_scenario("01_tiny_skirmish")
    _spread_out(state)
    state.active_player = Team.BLUE
    session = new_session(state, fog_of_war="classic")
    any_red = next(iter(state.units_of(Team.RED)))
    any_blue = next(iter(u for u in state.units_of(Team.BLUE) if u.alive))
    any_blue.status = UnitStatus.READY

    caplog.set_level(_logging.WARNING, logger="silicon.fog")
    # Attack may still fail for unrelated reasons (out of range)
    # but NOT with "not visible" — that check must have been bypassed.
    from silicon_pantheon.server.tools._common import ToolError
    try:
        attack(session, Team.BLUE, any_blue.id, any_red.id)
    except ToolError as e:
        msg = str(e)
        assert "not visible" not in msg, (
            f"log-only mode rejected anyway: {msg}"
        )
    messages = [r.message for r in caplog.records if r.name == "silicon.fog"]
    assert any("fog_violation_allowed" in m for m in messages), (
        "log-only mode must emit fog_violation_allowed WARNING; "
        f"saw: {messages}"
    )


def test_attack_allowed_under_no_fog_baseline() -> None:
    """Baseline: under fog=none, the visibility gate is a no-op and
    a valid attack proceeds (or fails only for normal rules like
    out-of-range). This confirms _require_target_visible doesn't
    over-reject."""
    from silicon_pantheon.server.session import new_session
    from silicon_pantheon.server.tools.mutations import attack
    from silicon_pantheon.server.tools._common import ToolError

    state = load_scenario("01_tiny_skirmish")
    _spread_out(state)
    state.active_player = Team.BLUE
    session = new_session(state, fog_of_war="none")
    any_red = next(iter(state.units_of(Team.RED)))
    any_blue = next(iter(u for u in state.units_of(Team.BLUE) if u.alive))
    from silicon_pantheon.server.engine.state import UnitStatus
    any_blue.status = UnitStatus.READY

    try:
        attack(session, Team.BLUE, any_blue.id, any_red.id)
    except ToolError as e:
        # Expected: out-of-range (not the fog rejection we'd get under fog).
        msg = str(e)
        assert "not visible" not in msg, (
            f"fog check fired under fog=none: {msg}"
        )


def test_filter_state_redacts_last_action_of_hidden_enemy() -> None:
    """Regression: get_state response included last_action field from
    state_to_dict, but filter_state did not redact it. This leaked
    enemy unit IDs and destination tiles to viewers even when the unit
    was fog-hidden. The fix: extract action-visibility logic into
    _action_is_visible helper and apply it to both filter_state and
    filter_history."""
    state = load_scenario("01_tiny_skirmish")
    _spread_out(state)
    ctx = _ctx(Team.BLUE, "classic")
    red = next(iter(state.units_of(Team.RED)))
    
    # Synthesize a state where last_action is a hidden enemy's move.
    state.last_action = {
        "type": "move",
        "unit_id": red.id,  # hidden under fog for blue
        "dest": {"x": state.board.width - 1, "y": state.board.height - 1}
    }
    
    # Before the fix, filter_state would pass last_action through verbatim,
    # leaking the hidden enemy's ID.
    filtered = filter_state(state, ctx)
    
    # After the fix, last_action should be redacted (set to None).
    assert filtered["last_action"] is None, (
        f"last_action containing hidden enemy {red.id} was not redacted; "
        f"got {filtered['last_action']}"
    )
    
    # Sanity check: own-team last_action should pass through.
    blue = next(iter(state.units_of(Team.BLUE)))
    state.last_action = {
        "type": "move",
        "unit_id": blue.id,
        "dest": {"x": 0, "y": 0}
    }
    filtered = filter_state(state, ctx)
    assert filtered["last_action"] is not None, (
        "own-team last_action should not be redacted"
    )
    assert filtered["last_action"]["unit_id"] == blue.id


def test_filter_state_redacts_end_turn_with_hidden_win_unit() -> None:
    """Regression (thermopylae / cannae): when a win-condition fires
    on end_turn, engine.rules._apply_end_turn splices result.details
    into the end_turn payload via payload.update(). reach_tile /
    reach_goal_line details carry {"unit": <winning_id>, "pos": ...},
    so last_action becomes

        {"type": "end_turn", "by": "red", "winner": "red",
         "reason": "reach_goal_line",
         "unit": "u_r_mardonius_1",
         "pos": {"x": 1, "y": 7}}

    If the winning unit is currently fog-hidden from the loser, that
    id leaks through last_action.unit. Pre-fix _action_is_visible
    returned True unconditionally for t=="end_turn" so the redaction
    never fired; the audit caught it in production (thermopylae log
    showed 518 `fog leak in get_state` errors on blue's polling)."""
    state = load_scenario("01_tiny_skirmish")
    _spread_out(state)
    ctx = _ctx(Team.BLUE, "classic")
    red = next(iter(state.units_of(Team.RED)))

    state.last_action = {
        "type": "end_turn",
        "by": "red",
        "winner": "red",
        "reason": "reach_goal_line",
        "unit": red.id,
        "pos": {
            "x": state.board.width - 1,
            "y": state.board.height - 1,
        },
    }
    filtered = filter_state(state, ctx)
    assert filtered["last_action"] is None, (
        f"end_turn payload with hidden winning unit {red.id} was not "
        f"redacted; got {filtered['last_action']}"
    )

    # Sanity: a plain end_turn with no unit field still passes through.
    state.last_action = {"type": "end_turn", "by": "red"}
    filtered = filter_state(state, ctx)
    assert filtered["last_action"] == {"type": "end_turn", "by": "red"}
