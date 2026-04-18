"""ASCII/rich board rendering."""

from __future__ import annotations

from rich.text import Text

from silicon_pantheon.server.engine.state import GameState, Pos, Team, TerrainType

# Terrain glyph map (background characters). Keys are strings so
# custom terrain types from scenarios land here too.
TERRAIN_GLYPH = {
    TerrainType.PLAIN.value: ".",
    TerrainType.FOREST.value: "f",
    TerrainType.MOUNTAIN.value: "^",
    TerrainType.FORT.value: "*",
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
                glyph = unit.stats.glyph or CLASS_GLYPH.get(unit.class_, unit.class_[:1] if unit.class_ else "?")
                base = "bold cyan" if unit.owner is Team.BLUE else "bold red"
                # Highlight when a unit is standing on a fort so it's obvious
                # whose fort it is and whether a seize is imminent. Underline
                # = own fort, underline+reverse = enemy fort (seize possible
                # at end_turn).
                if tile.is_fort:
                    if tile.fort_owner is unit.owner:
                        base = f"{base} underline"
                    else:
                        base = f"{base} underline reverse"
                rendered = glyph if unit.owner is Team.BLUE else glyph.lower()
                cell = Text(f" {rendered}", style=base)
            else:
                # Custom terrain types may supply their own glyph +
                # color on the Tile; fall back to built-in maps.
                g = tile.glyph or TERRAIN_GLYPH.get(tile.type, "?")
                default_colors = {
                    TerrainType.PLAIN.value: "dim",
                    TerrainType.FOREST.value: "green",
                    TerrainType.MOUNTAIN.value: "bright_black",
                    TerrainType.FORT.value: "yellow",
                }
                style = tile.color or default_colors.get(tile.type, "dim")
                # Fort color by owner (built-in forts only).
                if tile.is_fort and tile.fort_owner is Team.BLUE:
                    style = "cyan"
                elif tile.is_fort and tile.fort_owner is Team.RED:
                    style = "red"
                cell = Text(f" {g}", style=style)
            out.append(cell)
            out.append(" ")
        out.append("\n")
    return out
