"""Cross-screen terrain rendering must agree on glyph + color.

Regression: 06_agincourt's "mud" rendered as "," yellow in-game,
"m" magenta in the room preview, and "m" dim in the scenario picker
because each screen had its own hardcoded built-in table and a
different fallback heuristic. This test pins the new shared helper
so the divergence can't come back.
"""

from __future__ import annotations

from silicon_pantheon.client.tui.terrain import terrain_cell


# 06_agincourt declares: mud { glyph: ",", color: yellow }, stakes
# { glyph: "X", color: bright_red }, plus uses built-in forest.
AGINCOURT_TERRAIN = {
    "mud": {"glyph": ",", "color": "yellow"},
    "stakes": {"glyph": "X", "color": "bright_red"},
}


def test_scenario_declared_custom_terrain_uses_yaml_glyph_and_color():
    """The scenario author is the authority for custom terrain. If
    they declare glyph + color, those win — no renderer should
    substitute a fallback letter or default color."""
    assert terrain_cell("mud", AGINCOURT_TERRAIN) == (",", "yellow")
    assert terrain_cell("stakes", AGINCOURT_TERRAIN) == ("X", "bright_red")


def test_builtins_use_canonical_style():
    """The four canonical built-ins render the same regardless of
    whether the scenario declares terrain_types."""
    assert terrain_cell("plain", None) == (".", "dim")
    assert terrain_cell("forest", None) == ("f", "green")
    assert terrain_cell("mountain", None) == ("^", "bright_black")
    assert terrain_cell("fort", None) == ("*", "yellow")
    # Same with an empty terrain_types map.
    assert terrain_cell("forest", {}) == ("f", "green")


def test_scenario_can_override_builtin_appearance():
    """A scenario that wants forests to look different (e.g. dark
    autumn map) should be able to redeclare 'forest' in its
    terrain_types and have that win over the built-in default."""
    custom = {"forest": {"glyph": "F", "color": "dark_red"}}
    assert terrain_cell("forest", custom) == ("F", "dark_red")


def test_unknown_is_always_question_mark_for_fog():
    """Fog-of-war marker must render uniformly even if a scenario
    accidentally declares 'unknown'. The renderer is the authority
    here so the fog visualization stays predictable."""
    assert terrain_cell("unknown", None) == ("?", "bright_black")
    # And even if the scenario declares one, fog wins.
    assert terrain_cell("unknown", {"unknown": {"glyph": "Q", "color": "red"}}) == (
        "?",
        "bright_black",
    )


def test_undeclared_custom_terrain_falls_back_deterministically():
    """If a scenario uses a terrain type without declaring it (or
    declares only one of glyph/color), every renderer must agree on
    the fallback so the picker, room, and in-game views still match
    each other — even if the result is plain-looking."""
    # Fully undeclared.
    assert terrain_cell("swamp", None) == ("s", "dim")
    assert terrain_cell("swamp", {}) == ("s", "dim")
    # Glyph only.
    assert terrain_cell("swamp", {"swamp": {"glyph": "~"}}) == ("~", "dim")
    # Color only.
    assert terrain_cell("swamp", {"swamp": {"color": "blue"}}) == ("s", "blue")


def test_glyph_clipped_to_one_character():
    """Authors who write glyph: 'MUD' must not break the column
    grid — the renderer slices to one char."""
    assert terrain_cell(
        "mud", {"mud": {"glyph": "MUD", "color": "yellow"}}
    ) == ("M", "yellow")


def test_no_terrain_types_block_does_not_crash():
    """Picker calls happen before the bundle is loaded; renderer
    must accept None / empty / missing key without raising."""
    assert terrain_cell("forest")
    assert terrain_cell("plain", None)
    assert terrain_cell("mud", {})
