"""Read-only tools -- query game state without modifying it."""

from __future__ import annotations

from ..engine.board import in_attack_range, tiles_in_attack_range
from ..engine.combat import predict_attack
from ..engine.rules import IllegalAction, legal_actions_for_unit
from ..engine.serialize import state_to_dict
from ..engine.state import Pos, Team, UnitStatus
from ..session import Session
from ._common import ToolError, _require_active, _require_own_unit, _visible_enemies


def get_state(session: Session, viewer: Team) -> dict:
    return state_to_dict(session.state, viewer=viewer)


def get_unit_range(session: Session, viewer: Team, unit_id: str) -> dict:
    """Return the full threat zone for a unit: tiles it can move to
    (BFS reachable set) AND tiles it can attack from any reachable
    position (the outer threat ring). Read-only, works for ANY alive
    unit (own or enemy), no turn-ownership check.

    Units with status DONE return empty sets (they can't act this
    turn -- nothing to show).
    """
    from ..engine.board import reachable_tiles, tiles_in_attack_range

    state = session.state
    u = state.units.get(unit_id)
    if u is None or not u.alive:
        raise ToolError(f"unit {unit_id} does not exist or is dead")
    # Fog check: enemy units hidden by fog must not be queryable.
    if u.owner != viewer:
        visible_enemies = _visible_enemies(session, viewer)
        if u not in visible_enemies:
            raise ToolError(f"unit {unit_id} does not exist or is dead")

    # Always show full hypothetical range from the unit's current
    # tile, regardless of status (ready/moved/done). This is a
    # visualization aid — "what could this unit reach?" — not tied
    # to whether it can actually act this turn.
    reach = reachable_tiles(state, u)
    move_set = set(reach.keys())
    move_tiles = [{"x": p.x, "y": p.y} for p in sorted(move_set, key=lambda p: (p.y, p.x))]

    # Attack range -- expand each reachable tile by the unit's
    # attack range, subtract the move set itself. This is the
    # "outer ring" of threat: tiles the unit can hit if it moves
    # optimally first. Current position is included in reach so
    # standing attacks are covered.
    attack_set: set[Pos] = set()
    for p in move_set:
        for t in tiles_in_attack_range(p, u.stats, state.board):
            if t not in move_set:
                attack_set.add(t)
    attack_tiles = [{"x": p.x, "y": p.y} for p in sorted(attack_set, key=lambda p: (p.y, p.x))]

    return {
        "unit_id": unit_id,
        "move_tiles": move_tiles,
        "attack_tiles": attack_tiles,
    }


def get_unit(session: Session, viewer: Team, unit_id: str) -> dict:
    u = session.state.units.get(unit_id)
    if u is None or not u.alive:
        raise ToolError(f"unit {unit_id} does not exist or is dead")
    return {
        "id": u.id,
        "owner": u.owner.value,
        "class": u.class_,
        "pos": u.pos.to_dict(),
        "hp": u.hp,
        "hp_max": u.stats.hp_max,
        "atk": u.stats.atk,
        "def": u.stats.defense,
        "res": u.stats.res,
        "spd": u.stats.spd,
        "rng": [u.stats.rng_min, u.stats.rng_max],
        "move": u.stats.move,
        "is_magic": u.stats.is_magic,
        "can_heal": u.stats.can_heal,
        "status": u.status.value,
    }


def get_legal_actions(session: Session, viewer: Team, unit_id: str) -> dict:
    _require_active(session, viewer)
    _require_own_unit(session.state, unit_id, viewer)
    try:
        return legal_actions_for_unit(session.state, unit_id)
    except IllegalAction as e:
        raise ToolError(str(e)) from e


def simulate_attack(
    session: Session,
    viewer: Team,
    attacker_id: str,
    target_id: str,
    from_tile: dict | None = None,
) -> dict:
    state = session.state
    attacker = state.units.get(attacker_id)
    target = state.units.get(target_id)
    if attacker is None or not attacker.alive:
        raise ToolError(f"attacker {attacker_id} does not exist or is dead")
    if target is None or not target.alive:
        raise ToolError(f"target {target_id} does not exist or is dead")
    if attacker.owner is target.owner:
        raise ToolError("attacker and target are on the same team")
    # Fog check: cannot simulate attacks involving units hidden by fog.
    # Attacker must be own-team; target must be visible.
    if target.owner != viewer:
        visible_enemies = _visible_enemies(session, viewer)
        if target not in visible_enemies:
            raise ToolError(f"target {target_id} does not exist or is dead")

    origin = Pos.from_dict(from_tile) if from_tile else attacker.pos
    if not in_attack_range(origin, target.pos, attacker.stats):
        raise ToolError(f"target is not in attack range from {origin.to_dict()}")

    pred = predict_attack(
        attacker,
        target,
        attacker_tile=state.board.tile(origin),
        defender_tile=state.board.tile(target.pos),
        attacker_pos=origin,
    )
    return {
        # "kind" flags this as a prediction, not an executed attack.
        # Models have conflated simulate_attack's return with attack's
        # return because the damage fields match -- then reasoned as if
        # the target was already dead. "kind": "prediction" and the
        # inline note give the LLM an unambiguous signal.
        "kind": "prediction",
        "note": (
            "This is a SIMULATION result -- no state has changed. "
            "The target is still alive and unharmed. To actually "
            "deal this damage, call attack(unit_id, target_id)."
        ),
        "attacker_id": attacker_id,
        "target_id": target_id,
        "from": origin.to_dict(),
        "damage_per_hit": pred.damage_per_hit,
        "attacker_hits": pred.attacker_hits,
        "predicted_damage_to_defender": pred.total_damage_to_defender,
        "predicted_defender_dies": pred.defender_dies,
        "will_counter": pred.will_counter,
        "counter_damage_per_hit": pred.counter_damage_per_hit,
        "counter_hits": pred.counter_hits,
        "predicted_counter_damage": pred.total_counter_damage,
        "predicted_attacker_dies": pred.attacker_dies,
    }


def get_threat_map(session: Session, viewer: Team) -> dict:
    """For each tile, which enemy units could attack a unit standing there."""
    state = session.state
    enemy = viewer.other()
    threats: dict[str, list[str]] = {}
    for eu in state.units_of(enemy):
        for p in tiles_in_attack_range(eu.pos, eu.stats, state.board):
            key = f"{p.x},{p.y}"
            threats.setdefault(key, []).append(eu.id)
    return {"threats": threats}


def get_tactical_summary(session: Session, viewer: Team) -> dict:
    """One-shot "what's worth doing this turn" digest.

    Precomputes the observations a thoughtful player would reach by
    calling simulate_attack/get_threat_map across every own-unit x
    enemy pair. For a 5-unit-per-side scenario this replaces ~10-20
    model round-trips with one server call.

    Output:
      opportunities: predicted-attack pairs your live ready/moved
                     units can execute right now from their CURRENT
                     positions. Each entry is the same shape as
                     simulate_attack's response so the agent can
                     reason with familiar fields.
      threats:      for each of your living units, which visible
                    enemy units can reach (and attack) its current
                    tile. A subset of get_threat_map filtered to
                    just the tiles your units occupy -- the signal
                    is "which of your units is in danger now?".
      pending_action: unit IDs currently in MOVED status that MUST
                     still act before end_turn. The same info
                     end_turn's error would give you, but surfaced
                     proactively so the retry loop never fires.
    """
    state = session.state
    my_units = [u for u in state.units_of(viewer) if u.alive]
    # Fog-aware: enemies we can't see are NOT listed in opportunities
    # or threats. Otherwise the tool would leak positions the fog
    # filter redacts from get_state. Under fog=none this is all
    # alive enemies.
    enemy_units = _visible_enemies(session, viewer)

    # Opportunities: every pair where my unit can attack the enemy
    # from its current position and is still able to act this turn.
    opportunities: list[dict] = []
    for atk in my_units:
        if atk.status is UnitStatus.DONE:
            continue
        for tgt in enemy_units:
            if not in_attack_range(atk.pos, tgt.pos, atk.stats):
                continue
            pred = predict_attack(
                atk, tgt,
                attacker_tile=state.board.tile(atk.pos),
                defender_tile=state.board.tile(tgt.pos),
                attacker_pos=atk.pos,
            )
            opportunities.append({
                "attacker_id": atk.id,
                "target_id": tgt.id,
                "predicted_damage_to_defender": pred.total_damage_to_defender,
                "predicted_counter_damage": pred.total_counter_damage,
                "predicted_defender_dies": pred.defender_dies,
                "predicted_attacker_dies": pred.attacker_dies,
            })

    # Threats: which enemies can reach (and attack) my units at their
    # current positions. Uses the same "tiles_in_attack_range" logic
    # as get_threat_map but scoped just to my occupied tiles.
    tiles_at_risk: dict[str, list[str]] = {}
    for eu in enemy_units:
        for p in tiles_in_attack_range(eu.pos, eu.stats, state.board):
            tiles_at_risk.setdefault(f"{p.x},{p.y}", []).append(eu.id)
    threats: list[dict] = []
    for u in my_units:
        k = f"{u.pos.x},{u.pos.y}"
        if k in tiles_at_risk:
            threats.append({
                "defender_id": u.id,
                "defender_hp": u.hp,
                "defender_hp_max": u.stats.hp_max,
                "threatened_by": list(tiles_at_risk[k]),
            })

    pending = [u.id for u in my_units if u.status is UnitStatus.MOVED]

    # Include ALL pending coach messages for this viewer. Messages
    # accumulate until end_turn clears them — so the human can type
    # multiple messages during the opponent's turn and ALL of them
    # are delivered. We do NOT clear here; clearing happens in
    # end_turn (mutations.py) so repeated get_tactical_summary calls
    # (agent tool calls mid-turn) still see the full set.
    coach_queue = session.coach_queues.get(viewer, [])
    coach_messages = [{"turn": m.turn, "text": m.text} for m in coach_queue]

    # Win-condition progress: one line per condition, describing
    # where the viewer stands on the scoreboard. Lets the model
    # reason about "am I winning" without enumerating conditions
    # and counting units itself each turn. See
    # engine/win_conditions/rules.py for per-type formatters.
    win_progress: list[str] = []
    conds = getattr(state, "_win_conditions", None) or []
    for wc in conds:
        describe = getattr(wc, "describe_progress", None)
        if not callable(describe):
            continue
        try:
            line = describe(state, viewer)
        except Exception:
            # Don't let one misbehaving rule take down the whole
            # tactical summary -- skip it and log so we know.
            import logging as _logging
            from silicon_pantheon.shared.debug import reraise_in_debug
            _log = _logging.getLogger("silicon.engine")
            reraise_in_debug(
                _log,
                f"win_condition {type(wc).__name__!r} describe_progress raised",
            )
            _log.exception(
                "win condition %r describe_progress raised; skipping",
                type(wc).__name__,
            )
            continue
        if isinstance(line, str) and line.strip():
            win_progress.append(line.strip())

    return {
        "opportunities": opportunities,
        "threats": threats,
        "pending_action": pending,
        "win_progress": win_progress,
        "coach_messages": coach_messages,
    }


def get_history(session: Session, viewer: Team, last_n: int = 10) -> dict:
    """Return the full action history (or the last `last_n` events).

    `last_n <= 0` is treated as "give me everything" -- that's the
    convention agent_bridge.play_turn relies on when computing the
    opponent-actions delta from a history cursor. The previous
    behavior (last_n=0 -> empty list) made the agent see "Opponent
    did not act since your last turn" on EVERY turn, even when the
    opponent had clearly moved -- and the cursor-update call also
    used last_n=0, so the history cursor was stuck at 0 forever.
    """
    if last_n <= 0:
        hist = list(session.state.history)
    else:
        hist = session.state.history[-last_n:]
    return {
        "history": hist,
        "last_action": session.state.last_action,
        "turn": session.state.turn,
        "active_player": session.state.active_player.value,
    }
