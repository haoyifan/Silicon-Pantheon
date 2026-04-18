"""Mutation tools -- actions that modify game state."""

from __future__ import annotations

from ..engine.board import in_attack_range
from ..engine.rules import (
    AttackAction,
    EndTurnAction,
    HealAction,
    IllegalAction,
    MoveAction,
    WaitAction,
    apply,
)
from ..engine.state import GameState, Pos, Team, UnitStatus
from ..session import Session
from ._common import ToolError, _require_active, _require_own_unit, _visible_enemies


def _record_action(session: Session, result: dict) -> None:
    session.state.last_action = result
    session.state.history.append(result)
    session.log("action", result)
    # Drain any narrative events emitted by this action so they appear
    # in the replay (F.6) and can be surfaced to the TUI (F.7). Read
    # and clear atomically so the next action starts with a fresh log.
    log = getattr(session.state, "_narrative_log", None)
    if log:
        for entry in log:
            session.log("narrative_event", entry)
        log.clear()
    session.notify_action(result)


def move(session: Session, viewer: Team, unit_id: str, dest: dict) -> dict:
    _require_active(session, viewer)
    _require_own_unit(session.state, unit_id, viewer)

    # Pre-move visibility snapshot for fog-of-war reveal detection.
    # Under fog=none this is a no-op set comparison (everything is
    # always visible), so the cost is negligible.
    pre_visible_enemies = set(u.id for u in _visible_enemies(session, viewer))

    try:
        result = apply(session.state, MoveAction(unit_id=unit_id, dest=Pos.from_dict(dest)))
    except IllegalAction as e:
        raise ToolError(_enrich_move_error(session.state, unit_id, e)) from e

    # Post-move hints.
    result["next_actions"] = _post_move_next_actions(session, unit_id)

    # Fog-of-war reveal: which enemy units became visible because of
    # this move? The unit's new position changes the viewer's sight
    # footprint. Any enemy that's now visible but wasn't before is
    # "revealed" -- the agent should know immediately so it can react
    # without a follow-up get_state call.
    post_visible_enemies = _visible_enemies(session, viewer)
    newly_revealed = [
        u for u in post_visible_enemies if u.id not in pre_visible_enemies
    ]
    if newly_revealed:
        result["revealed_enemies"] = [
            {
                "id": u.id,
                "class": u.class_,
                "pos": {"x": u.pos.x, "y": u.pos.y},
                "hp": u.hp,
                "hp_max": u.stats.hp_max,
            }
            for u in newly_revealed
        ]

    _record_action(session, result)
    return result


def _post_move_next_actions(session: Session, unit_id: str) -> dict:
    """Compact summary of valid follow-ups after a move lands.

    Fields:
      status: "moved" (model occasionally loses track; spell it out)
      attack_targets: IDs of VISIBLE enemies in range from the new
                      position. Under fog modes this respects the
                      viewer's sight -- we do not leak enemies the
                      fog would hide.
      heal_targets: IDs of wounded adjacent friendlies (only if the
                    unit has can_heal; empty otherwise)
      must_resolve: True if the unit MUST still act before end_turn
                    (always True after a successful move; included so
                    the model has an unambiguous flag rather than
                    having to derive it from `status`)
    """
    state = session.state
    unit = state.units.get(unit_id)
    if unit is None:
        return {}
    # Visible enemies in range from the new position. Using
    # _visible_enemies ensures consistency with get_tactical_summary
    # + get_state's fog filter.
    visible_enemies = _visible_enemies(session, unit.owner)
    in_range = [
        u.id for u in visible_enemies
        if in_attack_range(unit.pos, u.pos, unit.stats)
    ]
    heal_tgts: list[str] = []
    if unit.stats.can_heal:
        heal_tgts = [
            u.id for u in state.units_of(unit.owner)
            if u.alive and u.id != unit.id
            and unit.pos.manhattan(u.pos) == 1
            and u.hp < u.stats.hp_max
        ]
    return {
        "status": "moved",
        "must_resolve": True,
        "attack_targets": in_range,
        "heal_targets": heal_tgts,
    }


def _enrich_move_error(
    state: GameState, unit_id: str, e: IllegalAction
) -> str:
    """Hint on move failures. The "not reachable" case is the most
    common -- tell the agent the unit's pos + move budget so it can
    re-plan without a get_state round-trip. We intentionally DON'T
    enumerate reachable tiles (could be 30+); we point at
    get_legal_actions for the exhaustive list."""
    msg = str(e)
    unit = state.units.get(unit_id)
    if unit is None:
        return msg
    if "not reachable" in msg:
        return (
            f"{msg}. Unit {unit_id} is at ({unit.pos.x},{unit.pos.y}) "
            f"with move budget {unit.stats.move}. Call "
            f"`get_legal_actions(unit_id={unit_id!r})` for the "
            f"authoritative reachable-tile list; don't guess."
        )
    if "has already moved" in msg:
        return (
            f"{msg}. {unit_id} status is {unit.status.value}. "
            f"You can still call attack/heal/wait on it this turn."
        )
    return msg


def attack(session: Session, viewer: Team, unit_id: str, target_id: str) -> dict:
    _require_active(session, viewer)
    _require_own_unit(session.state, unit_id, viewer)
    try:
        result = apply(session.state, AttackAction(unit_id=unit_id, target_id=target_id))
    except IllegalAction as e:
        raise ToolError(_enrich_attack_error(session, unit_id, target_id, e)) from e
    # Post-action status hint. Attacker is DONE after attacking (or
    # removed from state.units if killed in counter). Explicit
    # `attacker_status` saves the model re-deriving the status rule.
    attacker_after = session.state.units.get(unit_id)
    result["attacker_status"] = (
        attacker_after.status.value if attacker_after else "killed"
    )
    _record_action(session, result)
    return result


def _enrich_attack_error(
    session: Session, unit_id: str, target_id: str, e: IllegalAction
) -> str:
    """Add agent-usable hints to attack failures so the model doesn't
    need a follow-up get_state + get_legal_actions to recover.

    Hint categories:
      - target dead / nonexistent -> list of alive enemy IDs
      - out of range -> attacker's pos + range + in-range enemy IDs
      - attacker already DONE -> "use a different unit this turn"
      - target is ally -> which team target belongs to (model confused
        blue<->red mapping)
    """
    msg = str(e)
    state = session.state
    attacker = state.units.get(unit_id)
    if attacker is None:
        return msg
    # Fog-aware: only surface enemy IDs the viewer can see. Without
    # this filter the enriched error leaks "alive enemies" + "in-range
    # enemies" lists under classic / line_of_sight modes.
    visible_enemies = _visible_enemies(session, attacker.owner)
    if "does not exist or is dead" in msg:
        alive_ids = [u.id for u in visible_enemies]
        return f"{msg}. Alive enemy units you can see: [{', '.join(alive_ids) or '(none)'}]"
    if "out of attack range" in msg:
        in_range = [
            u.id for u in visible_enemies
            if in_attack_range(attacker.pos, u.pos, attacker.stats)
        ]
        return (
            f"{msg}. Attacker {unit_id} is at ({attacker.pos.x},"
            f"{attacker.pos.y}) with range "
            f"[{attacker.stats.rng_min}, {attacker.stats.rng_max}]. "
            f"Visible enemies in range right now: "
            f"[{', '.join(in_range) or '(none)'}]."
        )
    if "already acted this turn" in msg:
        ready_or_moved = [
            u.id for u in state.units_of(attacker.owner)
            if u.status is not UnitStatus.DONE
        ]
        return (
            f"{msg}. Units that can still act this turn: "
            f"[{', '.join(ready_or_moved) or '(none)'}]."
        )
    if "cannot attack allied" in msg:
        return (
            f"{msg}. Target {target_id} belongs to your own team "
            f"({attacker.owner.value}). Pick an enemy unit."
        )
    return msg


def heal(session: Session, viewer: Team, healer_id: str, target_id: str) -> dict:
    _require_active(session, viewer)
    _require_own_unit(session.state, healer_id, viewer)
    try:
        result = apply(session.state, HealAction(healer_id=healer_id, target_id=target_id))
    except IllegalAction as e:
        raise ToolError(_enrich_heal_error(session.state, healer_id, target_id, e)) from e
    # Healer is DONE after healing. Status hint lets the model skip
    # re-deriving the rule.
    healer_after = session.state.units.get(healer_id)
    result["healer_status"] = (
        healer_after.status.value if healer_after else "killed"
    )
    _record_action(session, result)
    return result


def _enrich_heal_error(
    state: GameState, healer_id: str, target_id: str, e: IllegalAction
) -> str:
    """Hint on heal failures. The most frequent miss is picking a
    non-adjacent target -- name the adjacent wounded friendlies so the
    agent doesn't burn a get_state + distance calc to recover."""
    msg = str(e)
    healer = state.units.get(healer_id)
    if healer is None:
        return msg
    if "cannot heal" in msg and "enemy" not in msg and "self" not in msg:
        # Class lacks can_heal.
        healers = [
            u.id for u in state.units_of(healer.owner)
            if u.alive and u.stats.can_heal
        ]
        return (
            f"{msg}. Your healers are: "
            f"[{', '.join(healers) or '(none -- no can_heal class fielded)'}]."
        )
    if "requires adjacent ally" in msg:
        adjacent_wounded = [
            u.id for u in state.units_of(healer.owner)
            if u.alive and u.id != healer.id
            and healer.pos.manhattan(u.pos) == 1
            and u.hp < u.stats.hp_max
        ]
        return (
            f"{msg}. Healer {healer_id} at ({healer.pos.x},"
            f"{healer.pos.y}); wounded friendly units adjacent right "
            f"now: [{', '.join(adjacent_wounded) or '(none)'}]."
        )
    if "cannot heal enemy" in msg:
        return (
            f"{msg}. Target {target_id} is on the opposing team. "
            f"Heal targets your own team only."
        )
    if "cannot self-heal" in msg:
        return (
            f"{msg}. Pick a wounded teammate at Manhattan distance 1."
        )
    return msg


def wait_unit(session: Session, viewer: Team, unit_id: str) -> dict:
    _require_active(session, viewer)
    _require_own_unit(session.state, unit_id, viewer)
    try:
        result = apply(session.state, WaitAction(unit_id=unit_id))
    except IllegalAction as e:
        raise ToolError(str(e)) from e
    # Unit flips to DONE after wait.
    unit_after = session.state.units.get(unit_id)
    result["unit_status"] = (
        unit_after.status.value if unit_after else "killed"
    )
    _record_action(session, result)
    return result


def end_turn(session: Session, viewer: Team) -> dict:
    _require_active(session, viewer)
    # Collect ALL units still pending action in one pass so the agent
    # gets a complete list in one error -- not "fix unit A, retry, fail
    # on unit B, retry" back-and-forth. For grok-3-mini on a 5-unit
    # turn this used to cost 5 extra round-trips; now it's one.
    pending = [u.id for u in session.state.units_of(viewer)
               if u.status is UnitStatus.MOVED]
    if pending:
        pending_str = ", ".join(pending)
        raise ToolError(
            f"cannot end_turn yet: {len(pending)} unit(s) moved but "
            f"have not acted -- [{pending_str}]. Call "
            f"attack/heal/wait on each before retrying end_turn."
        )
    # Record turn duration for telemetry.
    import time as _time
    if session.turn_start_time > 0:
        dt = _time.monotonic() - session.turn_start_time
        session.turn_times_by_team[viewer].append(dt)
    try:
        result = apply(session.state, EndTurnAction())
    except IllegalAction as e:
        raise ToolError(str(e)) from e
    _record_action(session, result)
    # Mark the start of the next team's turn.
    session.turn_start_time = _time.monotonic()
    return result
