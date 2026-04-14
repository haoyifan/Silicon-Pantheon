"""Board helpers: terrain access, pathfinding, range computation."""

from __future__ import annotations

import heapq

from .state import Board, GameState, Pos, TerrainType, Tile, Unit, UnitStats


def can_enter(stats: UnitStats, tile: Tile, unit_class: str | None = None) -> bool:
    """Whether a unit of class `unit_class` with `stats` may enter `tile`.

    Consults the tile's per-class override first (so scenarios can say
    'cavalry cannot enter sand'), then falls back to the legacy
    can_enter_forest / can_enter_mountain flags for built-in types.
    """
    if unit_class is not None:
        override = tile.class_overrides.get(unit_class) or {}
        if "passable" in override:
            return bool(override["passable"])
    if not tile.passable:
        return False
    if tile.type == TerrainType.FOREST.value and not stats.can_enter_forest:
        return False
    if tile.type == TerrainType.MOUNTAIN.value and not stats.can_enter_mountain:
        return False
    return True


def reachable_tiles(state: GameState, unit: Unit) -> dict[Pos, int]:
    """Return {destination: cost} for every tile reachable within `unit.stats.move`.

    Rules:
    - Cannot cross tiles occupied by enemy units.
    - Can pass through tiles occupied by allied units but cannot end there.
    - Cannot enter terrain the unit's class forbids.
    - Unit's current tile is included with cost 0.
    """
    board = state.board
    stats = unit.stats
    start = unit.pos

    # Dijkstra: each tile's cost is paid on entry (so starting tile is 0).
    dist: dict[Pos, int] = {start: 0}
    pq: list[tuple[int, int, int]] = [(0, start.x, start.y)]

    while pq:
        d, x, y = heapq.heappop(pq)
        p = Pos(x, y)
        if d > dist[p]:
            continue
        for n in p.neighbors4():
            if not board.in_bounds(n):
                continue
            tile = board.tile(n)
            if not can_enter(stats, tile, unit.class_):
                continue
            occupant = state.unit_at(n)
            if occupant is not None and occupant.owner is not unit.owner:
                # cannot pass through enemies
                continue
            step = tile.move_cost(unit.class_)
            nd = d + step
            if nd > stats.move:
                continue
            if nd < dist.get(n, 10**9):
                dist[n] = nd
                heapq.heappush(pq, (nd, n.x, n.y))

    # Filter out tiles blocked by allies (cannot end there), keep starting tile.
    result: dict[Pos, int] = {}
    for p, d in dist.items():
        if p == start:
            result[p] = d
            continue
        occupant = state.unit_at(p)
        if occupant is not None:
            continue  # ally or enemy — can't end here
        result[p] = d
    return result


def in_attack_range(attacker_pos: Pos, target_pos: Pos, stats: UnitStats) -> bool:
    d = attacker_pos.manhattan(target_pos)
    return stats.rng_min <= d <= stats.rng_max


def tiles_in_attack_range(pos: Pos, stats: UnitStats, board: Board) -> list[Pos]:
    """All in-bounds tiles at Manhattan distance within the unit's attack range."""
    out: list[Pos] = []
    for dx in range(-stats.rng_max, stats.rng_max + 1):
        for dy in range(-stats.rng_max, stats.rng_max + 1):
            d = abs(dx) + abs(dy)
            if d < stats.rng_min or d > stats.rng_max:
                continue
            p = Pos(pos.x + dx, pos.y + dy)
            if board.in_bounds(p):
                out.append(p)
    return out
