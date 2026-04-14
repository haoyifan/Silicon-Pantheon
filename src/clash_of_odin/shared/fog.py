"""Pure visibility computation for fog of war.

Given a GameState and a team, returns the set of tiles that team can
currently see. Uses Chebyshev-bounded sight cones plus a Bresenham
line-of-sight check in which forest and mountain tiles are opaque
(they are themselves visible, but they block sight of tiles beyond
them unless the viewer is directly adjacent).

Intended to be imported by both the server (for tool-response filtering)
and clients (for rendering their view). Must not touch server or client
state.
"""

from __future__ import annotations

from clash_of_odin.server.engine.state import GameState, Pos, Team, TerrainType


OPAQUE_TERRAIN = frozenset({TerrainType.FOREST.value, TerrainType.MOUNTAIN.value})


def _bresenham_line(a: Pos, b: Pos) -> list[Pos]:
    """Integer grid line from a to b, inclusive of both endpoints."""
    x0, y0 = a.x, a.y
    x1, y1 = b.x, b.y
    pts: list[Pos] = []
    dx = abs(x1 - x0)
    dy = -abs(y1 - y0)
    sx = 1 if x0 < x1 else -1
    sy = 1 if y0 < y1 else -1
    err = dx + dy
    x, y = x0, y0
    while True:
        pts.append(Pos(x, y))
        if x == x1 and y == y1:
            break
        e2 = 2 * err
        if e2 >= dy:
            err += dy
            x += sx
        if e2 <= dx:
            err += dx
            y += sy
    return pts


def _has_line_of_sight(state: GameState, viewer: Pos, target: Pos) -> bool:
    """True if `target` is visible from `viewer` under terrain rules.

    Adjacent tiles (Chebyshev distance 1) are always visible — you can
    always see your own square and your immediate neighbors. Otherwise
    we walk the Bresenham line and return False as soon as any
    intermediate tile is opaque terrain.
    """
    if viewer == target:
        return True
    if max(abs(viewer.x - target.x), abs(viewer.y - target.y)) <= 1:
        return True
    line = _bresenham_line(viewer, target)
    # Skip endpoints — blockers only count when they're strictly between.
    for p in line[1:-1]:
        tile = state.board.tile(p)
        # Both legacy built-in types and any custom type declaring
        # blocks_sight=True should block the line.
        if tile.type in OPAQUE_TERRAIN or tile.blocks_sight:
            return False
    return True


def _sight_cone(state: GameState, viewer: Pos, sight: int) -> set[Pos]:
    """All tiles within Chebyshev `sight` of `viewer` that have LOS."""
    visible: set[Pos] = set()
    board = state.board
    for dx in range(-sight, sight + 1):
        for dy in range(-sight, sight + 1):
            x, y = viewer.x + dx, viewer.y + dy
            if not (0 <= x < board.width and 0 <= y < board.height):
                continue
            target = Pos(x, y)
            if _has_line_of_sight(state, viewer, target):
                visible.add(target)
    return visible


def visible_tiles(state: GameState, team: Team) -> set[Pos]:
    """Union of sight cones over all of this team's living units."""
    seen: set[Pos] = set()
    for u in state.units_of(team):
        if u.stats.sight <= 0:
            continue
        seen.update(_sight_cone(state, u.pos, u.stats.sight))
    return seen
