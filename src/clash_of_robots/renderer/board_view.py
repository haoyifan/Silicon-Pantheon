"""ASCII/rich board rendering."""

from __future__ import annotations

from rich.text import Text

from clash_of_robots.server.engine.state import GameState, Pos, Team, TerrainType

# Terrain glyph map (background characters).
TERRAIN_GLYPH = {
    TerrainType.PLAIN: ".",
    TerrainType.FOREST: "f",
    TerrainType.MOUNTAIN: "^",
    TerrainType.FORT: "*",
}

CLASS_GLYPH = {
    "knight": "K",
    "archer": "A",
    "cavalry": "C",
    "mage": "M",
}


def render_board(state: GameState) -> Text:
    """Render the board as a rich Text with colored units and terrain."""
    out = Text()
    # Column header
    out.append("   " + " ".join(f"{x:>2}" for x in range(state.board.width)) + "\n", style="dim")
    for y in range(state.board.height):
        out.append(f"{y:>2} ", style="dim")
        for x in range(state.board.width):
            p = Pos(x, y)
            tile = state.board.tile(p)
            unit = state.unit_at(p)
            if unit is not None:
                glyph = CLASS_GLYPH.get(unit.class_.value, "?")
                if unit.owner is Team.BLUE:
                    cell = Text(f" {glyph}", style="bold cyan")
                else:
                    cell = Text(f" {glyph.lower()}", style="bold red")
            else:
                g = TERRAIN_GLYPH[tile.type]
                style = {
                    TerrainType.PLAIN: "dim",
                    TerrainType.FOREST: "green",
                    TerrainType.MOUNTAIN: "bright_black",
                    TerrainType.FORT: "yellow",
                }[tile.type]
                # Fort color by owner
                if tile.is_fort and tile.fort_owner is Team.BLUE:
                    style = "cyan"
                elif tile.is_fort and tile.fort_owner is Team.RED:
                    style = "red"
                cell = Text(f" {g}", style=style)
            out.append(cell)
            out.append(" ")
        out.append("\n")
    return out
