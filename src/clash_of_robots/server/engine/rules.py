"""Game rules: legal actions, apply, win-condition checks.

Actions are modeled as typed dataclasses. `apply(state, action)` mutates the
state in place and returns a result dict describing what happened (used by
replay logging and `last_action`).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Union

from .board import in_attack_range, reachable_tiles
from .combat import predict_attack
from .state import (
    GameState,
    GameStatus,
    Pos,
    Team,
    Unit,
    UnitStatus,
)

# ---- action types ----


@dataclass
class MoveAction:
    unit_id: str
    dest: Pos


@dataclass
class AttackAction:
    unit_id: str
    target_id: str


@dataclass
class HealAction:
    healer_id: str
    target_id: str


@dataclass
class WaitAction:
    unit_id: str


@dataclass
class EndTurnAction:
    pass


Action = Union[MoveAction, AttackAction, HealAction, WaitAction, EndTurnAction]


class IllegalAction(Exception):
    """Raised when an agent attempts an action the rules forbid."""


# ---- legal-action computation ----


def legal_actions_for_unit(state: GameState, unit_id: str) -> dict:
    """Return the structured set of actions this unit can take right now.

    Keys:
      - status: current unit status
      - moves: list of {dest, cost} if unit can still move, else []
      - attacks: list of {target_id, from, damage, will_counter, counter_damage,
                          kills, counter_kills} reachable from current pos
                 PLUS from each reachable destination if the unit hasn't moved
      - heals: similar, only if unit.can_heal
      - can_wait: True unless the unit is already DONE
    """
    unit = state.units.get(unit_id)
    if unit is None or not unit.alive:
        raise IllegalAction(f"unit {unit_id} does not exist or is dead")
    if unit.owner is not state.active_player:
        raise IllegalAction(f"unit {unit_id} belongs to {unit.owner}, not active player")

    moves: list[dict] = []
    attacks: list[dict] = []
    heals: list[dict] = []

    if unit.status is UnitStatus.READY:
        reach = reachable_tiles(state, unit)
        moves = [
            {"dest": p.to_dict(), "cost": c}
            for p, c in sorted(reach.items(), key=lambda kv: (kv[1], kv[0].x, kv[0].y))
            if p != unit.pos
        ]
        origins = list(reach.keys())
    elif unit.status is UnitStatus.MOVED:
        origins = [unit.pos]
    else:  # DONE
        return {
            "unit_id": unit_id,
            "status": unit.status.value,
            "moves": [],
            "attacks": [],
            "heals": [],
            "can_wait": False,
        }

    # Enumerate attacks and heals from every valid origin tile.
    for origin in origins:
        # Attacks
        for enemy in state.units.values():
            if not enemy.alive or enemy.owner is unit.owner:
                continue
            if not in_attack_range(origin, enemy.pos, unit.stats):
                continue
            pred = predict_attack(
                unit,
                enemy,
                attacker_tile=state.board.tile(origin),
                defender_tile=state.board.tile(enemy.pos),
                attacker_pos=origin,
            )
            attacks.append(
                {
                    "target_id": enemy.id,
                    "from": origin.to_dict(),
                    "damage": pred.total_damage_to_defender,
                    "kills": pred.defender_dies,
                    "will_counter": pred.will_counter,
                    "counter_damage": pred.total_counter_damage,
                    "counter_kills": pred.attacker_dies,
                }
            )
        # Heals
        if unit.stats.can_heal:
            for ally in state.units.values():
                if not ally.alive or ally.owner is not unit.owner or ally.id == unit.id:
                    continue
                if origin.manhattan(ally.pos) != 1:
                    continue
                if ally.hp >= ally.stats.hp_max:
                    continue  # no effect; exclude
                heal_amt = min(unit.stats.heal_amount, ally.stats.hp_max - ally.hp)
                heals.append(
                    {
                        "target_id": ally.id,
                        "from": origin.to_dict(),
                        "heal_amount": heal_amt,
                    }
                )

    return {
        "unit_id": unit_id,
        "status": unit.status.value,
        "moves": moves,
        "attacks": attacks,
        "heals": heals,
        "can_wait": True,
    }


# ---- apply() ----


def apply(state: GameState, action: Action) -> dict:
    """Mutate state by applying action; return a result dict for logging.

    Raises IllegalAction on any rule violation.
    """
    if state.status is GameStatus.GAME_OVER:
        raise IllegalAction("game is already over")

    if isinstance(action, EndTurnAction):
        return _apply_end_turn(state)

    # All non-end-turn actions target a unit that must belong to active player.
    actor_id = getattr(action, "unit_id", None) or getattr(action, "healer_id", None)
    assert actor_id is not None
    actor = state.units.get(actor_id)
    if actor is None or not actor.alive:
        raise IllegalAction(f"unit {actor_id} does not exist or is dead")
    if actor.owner is not state.active_player:
        raise IllegalAction(f"unit {actor_id} is not owned by active player")

    if isinstance(action, MoveAction):
        return _apply_move(state, actor, action.dest)
    if isinstance(action, AttackAction):
        return _apply_attack(state, actor, action.target_id)
    if isinstance(action, HealAction):
        return _apply_heal(state, actor, action.target_id)
    if isinstance(action, WaitAction):
        return _apply_wait(state, actor)
    raise IllegalAction(f"unknown action: {action!r}")


def _apply_move(state: GameState, unit: Unit, dest: Pos) -> dict:
    if unit.status is not UnitStatus.READY:
        raise IllegalAction(f"{unit.id} has already moved this turn")
    reach = reachable_tiles(state, unit)
    if dest not in reach:
        raise IllegalAction(f"{dest} not reachable by {unit.id}")
    unit.pos = dest
    unit.status = UnitStatus.MOVED
    return {"type": "move", "unit_id": unit.id, "dest": dest.to_dict()}


def _apply_attack(state: GameState, attacker: Unit, target_id: str) -> dict:
    target = state.units.get(target_id)
    if target is None or not target.alive:
        raise IllegalAction(f"target {target_id} does not exist or is dead")
    if target.owner is attacker.owner:
        raise IllegalAction("cannot attack allied unit")
    if attacker.status is UnitStatus.DONE:
        raise IllegalAction(f"{attacker.id} has already acted this turn")
    if not in_attack_range(attacker.pos, target.pos, attacker.stats):
        raise IllegalAction(f"{target_id} out of attack range")

    pred = predict_attack(
        attacker,
        target,
        attacker_tile=state.board.tile(attacker.pos),
        defender_tile=state.board.tile(target.pos),
    )
    target.hp = max(0, target.hp - pred.total_damage_to_defender)
    if pred.will_counter:
        attacker.hp = max(0, attacker.hp - pred.total_counter_damage)

    killed = []
    if not target.alive:
        killed.append(target.id)
    if not attacker.alive:
        killed.append(attacker.id)

    # Remove dead units.
    for uid in killed:
        del state.units[uid]

    if attacker.alive:
        attacker.status = UnitStatus.DONE

    return {
        "type": "attack",
        "unit_id": attacker.id,
        "target_id": target_id,
        "damage_dealt": pred.total_damage_to_defender,
        "counter_damage": pred.total_counter_damage,
        "target_killed": target_id in killed,
        "attacker_killed": attacker.id in killed if not attacker.alive else False,
    }


def _apply_heal(state: GameState, healer: Unit, target_id: str) -> dict:
    if not healer.stats.can_heal:
        raise IllegalAction(f"{healer.id} cannot heal")
    if healer.status is UnitStatus.DONE:
        raise IllegalAction(f"{healer.id} has already acted this turn")
    target = state.units.get(target_id)
    if target is None or not target.alive:
        raise IllegalAction(f"target {target_id} does not exist or is dead")
    if target.owner is not healer.owner:
        raise IllegalAction("cannot heal enemy unit")
    if target.id == healer.id:
        raise IllegalAction("cannot self-heal")
    if healer.pos.manhattan(target.pos) != 1:
        raise IllegalAction("heal requires adjacent ally")

    heal_amt = min(healer.stats.heal_amount, target.stats.hp_max - target.hp)
    target.hp += heal_amt
    healer.status = UnitStatus.DONE
    return {
        "type": "heal",
        "unit_id": healer.id,
        "target_id": target.id,
        "heal_amount": heal_amt,
    }


def _apply_wait(state: GameState, unit: Unit) -> dict:
    if unit.status is UnitStatus.DONE:
        raise IllegalAction(f"{unit.id} has already acted this turn")
    unit.status = UnitStatus.DONE
    return {"type": "wait", "unit_id": unit.id}


def _apply_end_turn(state: GameState) -> dict:
    active = state.active_player

    # 1. Check fort-capture win: any of my units standing on an enemy-owned fort?
    enemy = active.other()
    for u in state.units_of(active):
        tile = state.board.tile(u.pos)
        if tile.is_fort and tile.fort_owner is enemy:
            state.status = GameStatus.GAME_OVER
            state.winner = active
            return {
                "type": "end_turn",
                "by": active.value,
                "winner": active.value,
                "reason": "seize",
                "seized_at": u.pos.to_dict(),
                "seized_by_unit": u.id,
            }

    # 2. Hand over to opponent.
    state.active_player = enemy

    # 3. If we wrapped back to first_player, increment turn counter.
    if state.active_player is state.first_player:
        state.turn += 1
        if state.turn > state.max_turns:
            state.status = GameStatus.GAME_OVER
            state.winner = None  # draw
            return {"type": "end_turn", "by": active.value, "winner": None, "reason": "max_turns"}

    # 4. Start-of-turn effects for the incoming player:
    #    - reset unit statuses to READY
    #    - apply fort heal
    reset_ids = []
    for u in state.units_of(state.active_player):
        u.status = UnitStatus.READY
        reset_ids.append(u.id)
        tile = state.board.tile(u.pos)
        if tile.is_fort and tile.fort_owner is state.active_player and u.hp < u.stats.hp_max:
            u.hp = min(u.stats.hp_max, u.hp + 3)
    import logging as _logging

    _logging.getLogger("clash.engine").info(
        "end_turn: active now=%s turn=%s reset_to_ready=%s",
        state.active_player.value,
        state.turn,
        reset_ids,
    )

    # 5. Check elimination win (opponent has no units after our turn ends).
    if not state.units_of(state.active_player):
        state.status = GameStatus.GAME_OVER
        state.winner = active
        return {
            "type": "end_turn",
            "by": active.value,
            "winner": active.value,
            "reason": "elimination",
        }

    return {"type": "end_turn", "by": active.value, "winner": None, "reason": None}


def check_winner(state: GameState) -> Team | None:
    return state.winner
