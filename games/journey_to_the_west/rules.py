"""Journey to the West plugin: turn-10 bridge ambush."""

from __future__ import annotations

from clash_of_odin.server.engine.state import (
    Pos,
    Team,
    Unit,
    UnitStatus,
)


def spawn_ambush(state, turn: int, team: str, **_):
    """On turn 10, summon two extra skeletons near the bridge to make
    the middle crossing hairier. Only fires once — subsequent calls
    are no-ops because the ids already exist."""
    if turn != 10 or team != "red":
        return
    if "u_r_ambush_1" in state.units:
        return
    skel_stats = state.units[next(iter(state.units))].stats  # fallback source
    # Prefer copying from an existing skeleton if alive.
    for u in state.units.values():
        if u.class_ == "skeleton":
            skel_stats = u.stats
            break
    spawns = [("u_r_ambush_1", Pos(7, 3)), ("u_r_ambush_2", Pos(7, 5))]
    for uid, pos in spawns:
        # Skip if tile occupied.
        if any(u.pos == pos for u in state.units.values()):
            continue
        state.units[uid] = Unit(
            id=uid,
            owner=Team.RED,
            class_="skeleton",
            pos=pos,
            hp=skel_stats.hp_max,
            status=UnitStatus.READY,
            stats=skel_stats,
        )
