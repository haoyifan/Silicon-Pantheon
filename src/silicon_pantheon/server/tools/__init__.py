"""In-process tool implementations. Each tool operates on a Session.

The MCP server (`server/main.py`) wraps these for remote use; harnesses call
them directly for in-process play.

Each tool:
- takes `(session, viewer: Team, **args)` and returns a JSON-serializable dict
- raises `ToolError` on rule violations (maps to an MCP error or an error dict)
- is registered in TOOL_REGISTRY with its JSON schema
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..engine.board import in_attack_range, tiles_in_attack_range
from ..engine.combat import predict_attack
from ..engine.rules import (
    AttackAction,
    EndTurnAction,
    HealAction,
    IllegalAction,
    MoveAction,
    WaitAction,
    apply,
    legal_actions_for_unit,
)
from ..engine.serialize import state_to_dict
from ..engine.state import GameState, Pos, Team, UnitStatus
from ..session import CoachMessage, Session
from ...shared.viewer_filter import ViewerContext, currently_visible


class ToolError(Exception):
    """Raised when a tool call cannot be fulfilled. The error message is
    returned to the agent so it can self-correct.
    """


# ---- helpers ----


def _require_active(session: Session, viewer: Team) -> None:
    if session.state.active_player is not viewer:
        raise ToolError(
            f"not your turn (active: {session.state.active_player.value}, you: {viewer.value})"
        )


def _require_own_unit(state: GameState, unit_id: str, viewer: Team) -> None:
    u = state.units.get(unit_id)
    if u is None or not u.alive:
        raise ToolError(f"unit {unit_id} does not exist or is dead")
    if u.owner is not viewer:
        raise ToolError(f"unit {unit_id} is not yours (owner={u.owner.value})")


def _visible_enemies(session: Session, viewer: Team) -> list:
    """Enemy units visible to `viewer` under the session's fog mode.

    Under fog=none this is every alive enemy. Under classic /
    line_of_sight it's filtered to enemies standing on currently-
    visible tiles, matching the fog contract the state-serializer
    uses at filter_state.

    Callers that generate agent-visible hints MUST use this instead
    of state.units_of(enemy) directly — otherwise the hint leaks
    enemy positions the agent shouldn't be able to see.
    """
    enemy = viewer.other()
    enemies = [u for u in session.state.units_of(enemy) if u.alive]
    if session.fog_of_war == "none":
        return enemies
    ctx = ViewerContext(
        team=viewer,
        fog_mode=session.fog_of_war,  # type: ignore[arg-type]
        ever_seen=session.ever_seen.get(viewer, frozenset()),
    )
    visible = currently_visible(session.state, ctx)
    return [u for u in enemies if u.pos in visible]


# ---- read-only tools ----


def get_state(session: Session, viewer: Team) -> dict:
    return state_to_dict(session.state, viewer=viewer)


def get_unit_range(session: Session, viewer: Team, unit_id: str) -> dict:
    """Return the full threat zone for a unit: tiles it can move to
    (BFS reachable set) AND tiles it can attack from any reachable
    position (the outer threat ring). Read-only, works for ANY alive
    unit (own or enemy), no turn-ownership check.

    Units with status DONE return empty sets (they can't act this
    turn — nothing to show).
    """
    from ..engine.board import reachable_tiles, tiles_in_attack_range

    state = session.state
    u = state.units.get(unit_id)
    if u is None or not u.alive:
        raise ToolError(f"unit {unit_id} does not exist or is dead")
    if u.status is UnitStatus.DONE:
        return {"unit_id": unit_id, "move_tiles": [], "attack_tiles": []}

    # Movement range — BFS from current position.
    reach = reachable_tiles(state, u)
    move_set = set(reach.keys())
    move_tiles = [{"x": p.x, "y": p.y} for p in sorted(move_set, key=lambda p: (p.y, p.x))]

    # Attack range — expand each reachable tile by the unit's
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
        # return because the damage fields match — then reasoned as if
        # the target was already dead. "kind": "prediction" and the
        # inline note give the LLM an unambiguous signal.
        "kind": "prediction",
        "note": (
            "This is a SIMULATION result — no state has changed. "
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
    calling simulate_attack/get_threat_map across every own-unit ×
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
                    just the tiles your units occupy — the signal
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

    # Drain unread coach messages for this viewer. Auto-delivery in
    # this digest replaces the old `get_coach_messages` tool — agents
    # were missing coach advice because they only polled the tool
    # once per session (Haiku's "checked once, no need to check
    # again" pattern). Now the messages are shipped proactively in
    # the same response the agent fetches every turn-start.
    coach_queue = session.coach_queues.get(viewer, [])
    coach_messages = [{"turn": m.turn, "text": m.text} for m in coach_queue]
    session.coach_queues[viewer] = []

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
            # tactical summary — skip it and log so we know.
            import logging as _logging
            _logging.getLogger("silicon.engine").exception(
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

    `last_n <= 0` is treated as "give me everything" — that's the
    convention agent_bridge.play_turn relies on when computing the
    opponent-actions delta from a history cursor. The previous
    behavior (last_n=0 → empty list) made the agent see "Opponent
    did not act since your last turn" on EVERY turn, even when the
    opponent had clearly moved — and the cursor-update call also
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


# ---- write tools ----


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
    # "revealed" — the agent should know immediately so it can react
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
                      viewer's sight — we do not leak enemies the
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
    common — tell the agent the unit's pos + move budget so it can
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
      - target dead / nonexistent → list of alive enemy IDs
      - out of range → attacker's pos + range + in-range enemy IDs
      - attacker already DONE → "use a different unit this turn"
      - target is ally → which team target belongs to (model confused
        blue↔red mapping)
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
    non-adjacent target — name the adjacent wounded friendlies so the
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
            f"[{', '.join(healers) or '(none — no can_heal class fielded)'}]."
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
    # gets a complete list in one error — not "fix unit A, retry, fail
    # on unit B, retry" back-and-forth. For grok-3-mini on a 5-unit
    # turn this used to cost 5 extra round-trips; now it's one.
    pending = [u.id for u in session.state.units_of(viewer)
               if u.status is UnitStatus.MOVED]
    if pending:
        pending_str = ", ".join(pending)
        raise ToolError(
            f"cannot end_turn yet: {len(pending)} unit(s) moved but "
            f"have not acted — [{pending_str}]. Call "
            f"attack/heal/wait on each before retrying end_turn."
        )
    try:
        result = apply(session.state, EndTurnAction())
    except IllegalAction as e:
        raise ToolError(str(e)) from e
    _record_action(session, result)
    return result


# ---- coach channel ----


def send_to_agent(session: Session, viewer: Team, team: str, text: str) -> dict:
    target = Team(team)
    session.coach_queues[target].append(CoachMessage(turn=session.state.turn, text=text))
    session.log("coach_message", {"to": target.value, "text": text, "turn": session.state.turn})
    return {"ok": True, "queued_for": target.value, "turn": session.state.turn}


# ---- registry ----

Tool = Callable[..., dict]

TOOL_REGISTRY: dict[str, dict[str, Any]] = {
    "get_state": {
        "fn": get_state,
        "description": "Get the current full game state visible to you.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    "get_unit": {
        "fn": get_unit,
        "description": "Get a single unit's details by id.",
        "input_schema": {
            "type": "object",
            "properties": {"unit_id": {"type": "string"}},
            "required": ["unit_id"],
        },
    },
    "get_unit_range": {
        "fn": get_unit_range,
        "description": (
            "Full threat zone for a unit: tiles it can move to (BFS "
            "reachable) + tiles it can attack from any reachable "
            "position (the outer threat ring). Works for any alive "
            "unit, own or enemy. Units with status=done return empty."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"unit_id": {"type": "string"}},
            "required": ["unit_id"],
        },
    },
    "get_legal_actions": {
        "fn": get_legal_actions,
        "description": "Get the legal moves/attacks/heals/wait for one of your units.",
        "input_schema": {
            "type": "object",
            "properties": {"unit_id": {"type": "string"}},
            "required": ["unit_id"],
        },
    },
    "simulate_attack": {
        "fn": simulate_attack,
        "description": "Predict outcome of attacker_id attacking target_id (optionally from a given tile). Does not modify state.",
        "input_schema": {
            "type": "object",
            "properties": {
                "attacker_id": {"type": "string"},
                "target_id": {"type": "string"},
                "from_tile": {
                    "type": "object",
                    "properties": {"x": {"type": "integer"}, "y": {"type": "integer"}},
                    "required": ["x", "y"],
                },
            },
            "required": ["attacker_id", "target_id"],
        },
    },
    "get_threat_map": {
        "fn": get_threat_map,
        "description": "For each tile, which enemy units could attack a unit standing there. Returns {threats: {'x,y': [unit_id,...]}}.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    "get_tactical_summary": {
        "fn": get_tactical_summary,
        "description": (
            "Precomputed 'what's worth doing this turn' digest: attack "
            "opportunities your units can execute from current positions "
            "(with predicted damage / counter / kill outcomes), threats "
            "against your units from currently-visible enemies, and the "
            "list of your units still in MOVED status pending action. "
            "Call once per turn-start instead of many simulate_attack / "
            "get_threat_map calls."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    "get_history": {
        "fn": get_history,
        "description": "Get recent action history.",
        "input_schema": {
            "type": "object",
            "properties": {"last_n": {"type": "integer", "default": 10}},
            "required": [],
        },
    },
    "move": {
        "fn": move,
        "description": "Move one of your units to a destination tile. Unit must be in 'ready' status.",
        "input_schema": {
            "type": "object",
            "properties": {
                "unit_id": {"type": "string"},
                "dest": {
                    "type": "object",
                    "properties": {"x": {"type": "integer"}, "y": {"type": "integer"}},
                    "required": ["x", "y"],
                },
            },
            "required": ["unit_id", "dest"],
        },
    },
    "attack": {
        "fn": attack,
        "description": "Attack an enemy unit from your current position. Resolves combat immediately including counter-attack. Unit becomes 'done'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "unit_id": {"type": "string"},
                "target_id": {"type": "string"},
            },
            "required": ["unit_id", "target_id"],
        },
    },
    "heal": {
        "fn": heal,
        "description": "Heal an adjacent ally (Mage only). Unit becomes 'done'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "healer_id": {"type": "string"},
                "target_id": {"type": "string"},
            },
            "required": ["healer_id", "target_id"],
        },
    },
    "wait": {
        "fn": wait_unit,
        "description": "End this unit's turn without attacking or healing. Unit becomes 'done'.",
        "input_schema": {
            "type": "object",
            "properties": {"unit_id": {"type": "string"}},
            "required": ["unit_id"],
        },
    },
    "end_turn": {
        "fn": end_turn,
        "description": "Pass control to the opponent. Rejects if any unit is mid-action (moved but not acted).",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    "send_to_agent": {
        "fn": send_to_agent,
        "description": "(Coach tool) send a message to a team's agent, delivered at start of their next turn.",
        "input_schema": {
            "type": "object",
            "properties": {
                "team": {"type": "string", "enum": ["blue", "red"]},
                "text": {"type": "string"},
            },
            "required": ["team", "text"],
        },
    },
}


def call_tool(session: Session, viewer: Team, name: str, args: dict) -> dict:
    """Dispatch a tool call by name. Raises ToolError on unknown tool / bad args."""
    spec = TOOL_REGISTRY.get(name)
    if spec is None:
        raise ToolError(f"unknown tool: {name}")
    fn: Tool = spec["fn"]
    try:
        return fn(session, viewer, **args)
    except TypeError as e:
        raise ToolError(f"bad arguments for {name}: {e}") from e
