"""Shared terrain-cell renderer for every TUI map view.

Three screens render the scenario terrain map (in-game map panel,
room preview, scenario picker) and they MUST agree on how each tile
type looks. Before this module existed each screen had its own
hardcoded built-in table plus a different fallback for unknown
terrain — 06_agincourt's custom "mud" tile rendered as `,` yellow
in-game, `m` magenta in the room, and `m` dim in the picker.

The contract:
  - If the scenario declares the type in its `terrain_types` block
    and supplies BOTH glyph and color, that wins. Period. This is
    the only correct rendering for custom terrain — the scenario
    author is the authority.
  - If the type is one of the four canonical built-ins
    (plain / forest / mountain / fort), use the built-in style.
  - "unknown" (fog of war) is special-cased to `?` bright_black.
  - Anything else (a custom type that didn't supply both glyph and
    color, or a name we just don't recognize) falls through to a
    deterministic last-resort: first letter of the name, dim. This
    is intentionally unflashy so it's obvious to a scenario author
    that they forgot to declare glyph/color.

A scenario YAML can override a built-in by declaring it in
`terrain_types` — e.g. forest as `glyph: F`, `color: dark_green`.
The lookup hits the scenario block first.
"""

from __future__ import annotations

# Built-in defaults. These exist so a minimal scenario YAML that just
# uses "plain"/"forest"/etc. without declaring terrain_types still
# renders correctly. Any scenario that wants a different look can
# override by declaring the same key in its terrain_types block.
_BUILTIN: dict[str, tuple[str, str]] = {
    "plain": (".", "dim"),
    "forest": ("f", "green"),
    "mountain": ("^", "bright_black"),
    "fort": ("*", "yellow"),
}


def terrain_cell(
    ttype: str, scenario_terrain_types: dict | None = None
) -> tuple[str, str]:
    """Return (glyph, Rich style) for a terrain type.

    `scenario_terrain_types` is the `terrain_types` field from the
    `describe_scenario` bundle (or None if unavailable — fog-of-war
    edges, picker preview before bundle is loaded, etc.). When it
    contains a spec for `ttype` with both glyph and color, that's
    authoritative; otherwise we fall back to built-ins, then to a
    first-letter heuristic.

    `unknown` (the fog-of-war hidden-tile marker) is always rendered
    as `?` bright_black regardless of scenario.
    """
    if ttype == "unknown":
        return "?", "bright_black"

    spec = (scenario_terrain_types or {}).get(ttype) or {}
    glyph = spec.get("glyph")
    color = spec.get("color")
    if glyph and color:
        # Scenario-declared custom terrain wins. Slice glyph to one
        # char so a scenario author who writes "MUD" doesn't break
        # the column-aligned grid.
        return str(glyph)[:1], str(color)

    if ttype in _BUILTIN:
        return _BUILTIN[ttype]

    # Half-declared (only glyph or only color) or fully-undeclared
    # custom type. Use whatever the scenario provided + a default
    # for the missing piece, so authors get a usable rendering even
    # when they forget one field.
    if glyph:
        return str(glyph)[:1], "dim"
    if color:
        return (ttype[:1] or "?"), str(color)
    return (ttype[:1] or "?"), "dim"
