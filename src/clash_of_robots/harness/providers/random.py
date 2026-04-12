"""Random provider: picks a random legal action for each of its ready units.

Biases slightly toward attacks so matches don't drag on forever.
"""

from __future__ import annotations

import random

from clash_of_robots.server.engine.state import Pos, Team, UnitStatus
from clash_of_robots.server.session import Session
from clash_of_robots.server.tools import ToolError, call_tool

from .base import Provider


class RandomProvider(Provider):
    name = "random"

    def __init__(self, seed: int | None = None):
        self.rng = random.Random(seed)

    def decide_turn(self, session: Session, viewer: Team) -> None:
        # Act with each of our ready units in a stable-but-shuffled order.
        state = session.state
        unit_ids = [u.id for u in state.units_of(viewer)]
        self.rng.shuffle(unit_ids)

        for uid in unit_ids:
            if uid not in state.units or not state.units[uid].alive:
                continue  # could have died this turn to a counter? (not possible as attacker now, but defensive)
            u = state.units[uid]
            if u.status is UnitStatus.DONE:
                continue
            self._act_with_unit(session, viewer, uid)

        # End turn (handle possible exceptions defensively).
        try:
            call_tool(session, viewer, "end_turn", {})
        except ToolError:
            # Shouldn't happen since we set every ready unit to done or moved+acted, but
            # if it does, force a wait on the offender then retry.
            for u in state.units_of(viewer):
                if u.status is UnitStatus.MOVED:
                    try:
                        call_tool(session, viewer, "wait", {"unit_id": u.id})
                    except ToolError:
                        pass
            call_tool(session, viewer, "end_turn", {})

    def _act_with_unit(self, session: Session, viewer: Team, unit_id: str) -> None:
        la = call_tool(session, viewer, "get_legal_actions", {"unit_id": unit_id})

        attacks = la["attacks"]
        heals = la["heals"]
        moves = la["moves"]

        # Prefer an attack if any exist (and we have any). Bias toward attacks
        # that kill the target.
        if attacks:
            lethal = [a for a in attacks if a["kills"] and not a["counter_kills"]]
            pool = lethal or attacks
            choice = self.rng.choice(pool)
            from_pos = Pos.from_dict(choice["from"])
            if from_pos != session.state.units[unit_id].pos:
                try:
                    call_tool(
                        session, viewer, "move", {"unit_id": unit_id, "dest": from_pos.to_dict()}
                    )
                except ToolError:
                    return
            try:
                call_tool(
                    session,
                    viewer,
                    "attack",
                    {"unit_id": unit_id, "target_id": choice["target_id"]},
                )
            except ToolError:
                # Fall back: wait
                try:
                    call_tool(session, viewer, "wait", {"unit_id": unit_id})
                except ToolError:
                    pass
            return

        # No attacks: consider heal if possible
        if heals:
            choice = self.rng.choice(heals)
            from_pos = Pos.from_dict(choice["from"])
            if from_pos != session.state.units[unit_id].pos:
                try:
                    call_tool(
                        session, viewer, "move", {"unit_id": unit_id, "dest": from_pos.to_dict()}
                    )
                except ToolError:
                    return
            try:
                call_tool(
                    session,
                    viewer,
                    "heal",
                    {"healer_id": unit_id, "target_id": choice["target_id"]},
                )
            except ToolError:
                try:
                    call_tool(session, viewer, "wait", {"unit_id": unit_id})
                except ToolError:
                    pass
            return

        # No attacks or heals: random move (or stay put), then wait.
        if moves:
            # 50% chance to actually move
            if self.rng.random() < 0.5:
                choice = self.rng.choice(moves)
                try:
                    call_tool(session, viewer, "move", {"unit_id": unit_id, "dest": choice["dest"]})
                except ToolError:
                    pass
        try:
            call_tool(session, viewer, "wait", {"unit_id": unit_id})
        except ToolError:
            pass
