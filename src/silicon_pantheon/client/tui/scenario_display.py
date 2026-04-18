"""Scenario display helpers — win-condition prose, terrain summaries, unit names.

Pure functions that turn scenario-description dicts into human-readable
strings.  Used by the room screen, game screen, scenario picker, and
agent harness prompts.  Extracted from ``screens.room`` so every
consumer can import without pulling in the full room-screen dependency
tree.
"""

from __future__ import annotations

from typing import Any

from silicon_pantheon.client.locale import t


def localized_team(team: str, locale: str = "en") -> str:
    """Translate raw team name ('blue'/'red') to display name."""
    return t(f"team.{team}", locale)


def unit_cell_style(u: dict[str, Any]) -> tuple[str, str]:
    """Pick the (glyph, Rich style) for a unit cell on any map view.

    Color is ALWAYS team-based (cyan for blue, red for red) so the
    map is immediately readable at a glance. Per-class colors from
    the YAML are intentionally ignored here — they created a rainbow
    of yellows, greens, magentas, whites that made it impossible to
    tell teams apart. The glyph letter (uppercase=blue, lowercase=red)
    already differentiates unit classes; color's job is to show the
    team.

    Per-class colors still appear in the unit card (detailed stat
    view) and the player panel roster header."""
    cls = str(u.get("class", "") or "")
    owner = u.get("owner")
    glyph = u.get("glyph")
    if not glyph:
        glyph = (cls[:1] or "?")
    glyph = glyph.upper() if owner == "blue" else glyph.lower()
    # Team-based color: one hue per side, instantly readable.
    color = "cyan" if owner == "blue" else "red"
    return glyph, f"bold {color}"


def terrain_effect_summary(
    scenario_description: dict[str, Any] | None, terrain: str,
    locale: str = "en",
) -> str:
    """One-line summary of what a terrain does, built from the cached
    describe_scenario bundle. Empty if we don't have the data.
    Descriptions that ship in the bundle win; otherwise we compose
    from the individual fields."""
    if not scenario_description:
        return ""
    spec = (scenario_description.get("terrain_types") or {}).get(terrain)
    if not spec:
        return ""
    if spec.get("description"):
        return str(spec["description"])
    parts: list[str] = []
    mc = spec.get("move_cost")
    if mc is not None:
        parts.append(t("terrain_fx.move", locale).replace("{n}", str(mc)))
    db = spec.get("defense_bonus")
    if db:
        parts.append(t("terrain_fx.def_bonus", locale).replace("{n}", str(db)))
    rb = spec.get("res_bonus") or spec.get("magic_bonus")
    if rb:
        parts.append(t("terrain_fx.res_bonus", locale).replace("{n}", str(rb)))
    heals = spec.get("heals")
    if heals:
        sign = "+" if heals > 0 else ""
        parts.append(t("terrain_fx.hp_turn", locale).replace("{n}", f"{sign}{heals}"))
    if spec.get("blocks_sight"):
        parts.append(t("terrain_fx.blocks_los", locale))
    if spec.get("passable") is False:
        parts.append(t("terrain_fx.impassable", locale))
    if spec.get("effects_plugin"):
        parts.append(t("terrain_fx.plugin", locale).replace("{name}", str(spec["effects_plugin"])))
    return ", ".join(parts)


def strip_frontmatter(text: str) -> str:
    """Drop a leading `---\\n...\\n---` YAML frontmatter block if one
    is present. Strategy md files sometimes start with one; players
    don't need to see the metadata, just the prose playbook."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return text
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return "\n".join(lines[i + 1 :])
    return text


def unit_display_name(
    unit: dict[str, Any],
    scenario_description: dict[str, Any] | None,
) -> str:
    """Best human-readable name for a unit. Per-unit override first,
    then class display_name from the scenario bundle, then the slug."""
    if unit.get("display_name"):
        return str(unit["display_name"])
    cls = unit.get("class") or ""
    spec = (
        (scenario_description or {}).get("unit_classes") or {}
    ).get(cls)
    if spec and spec.get("display_name"):
        return str(spec["display_name"])
    return cls or str(unit.get("id", "?"))


def humanize_unit_id(unit_id: str, scenario_description: dict[str, Any] | None) -> str:
    """Translate `u_b_tang_monk_1` -> `Tang Monk` when the scenario
    bundle has a display_name for the class. Falls back to the slug
    derived from the id (tang_monk -> tang_monk) and finally to the
    raw id so we never silently drop information."""
    if not unit_id:
        return "?"
    parts = unit_id.split("_")
    # Convention: u_<team>_<class>_<index>. The class can itself
    # contain underscores (tang_monk, white_bone_demon), so re-join
    # everything between the team initial and the trailing index.
    if len(parts) >= 4 and parts[0] == "u":
        class_slug = "_".join(parts[2:-1])
    else:
        class_slug = unit_id
    spec = (
        (scenario_description or {}).get("unit_classes") or {}
    ).get(class_slug)
    if spec and spec.get("display_name"):
        return str(spec["display_name"])
    return class_slug or unit_id


def other_team(team: str) -> str:
    return "red" if team == "blue" else "blue"


def filter_win_conditions(wcs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Remove redundant win conditions that would confuse the reader.

    When ``protect_unit_survives`` is present, ``max_turns_draw`` is
    misleading — the VIP surviving to the turn cap is a WIN for the
    protector, not a draw.  Suppress the draw line so the rules don't
    contradict each other.
    """
    has_survives = any(wc.get("type") == "protect_unit_survives" for wc in wcs)
    if not has_survives:
        return wcs
    return [wc for wc in wcs if wc.get("type") != "max_turns_draw"]


def describe_win_condition(
    wc: dict[str, Any],
    scenario_description: dict[str, Any] | None = None,
    locale: str = "en",
) -> str:
    """Return a one-line, side-explicit explanation of a win condition.

    Both teams need to read this prose: protect_unit and
    reach_tile are asymmetric (only one side benefits), so each line
    must say WHO wins instead of just describing the trigger. Earlier
    versions said "keep Tang Monk (blue) alive" — true from blue's
    perspective, but a red player reading the same line wouldn't know
    they win when the monk dies.
    """
    wc_type = wc.get("type", "")
    if wc_type == "seize_enemy_fort":
        return t("win.seize_enemy_fort", locale)
    if wc_type == "eliminate_all_enemy_units":
        return t("win.eliminate_all", locale)
    if wc_type == "max_turns_draw":
        n = wc.get("turns")
        if n:
            return t("win_desc.max_turns_draw_n", locale).replace("{n}", str(n))
        return t("win.max_turns_draw", locale)
    if wc_type == "protect_unit":
        name = humanize_unit_id(wc.get("unit_id", ""), scenario_description)
        loser = wc.get("owning_team", "?")
        winner = other_team(loser)
        return (t("win.protect_unit", locale)
                .replace("{winner}", localized_team(winner, locale))
                .replace("{name}", name)
                .replace("{loser}", localized_team(loser, locale)))
    if wc_type == "protect_unit_survives":
        name = humanize_unit_id(wc.get("unit_id", ""), scenario_description)
        protector = wc.get("owning_team", "?")
        return (t("win.protect_unit_survives", locale)
                .replace("{protector}", localized_team(protector, locale))
                .replace("{name}", name))
    if wc_type == "reach_tile":
        pos = wc.get("pos") or {}
        team = wc.get("team", "?")
        u = wc.get("unit_id")
        who = humanize_unit_id(u, scenario_description) if u else t("button_val.any_unit", locale).replace("{team}", localized_team(team, locale))
        return (t("win.reach_tile", locale)
                .replace("{team}", localized_team(team, locale))
                .replace("{who}", who)
                .replace("{x}", str(pos.get("x", "?")))
                .replace("{y}", str(pos.get("y", "?"))))
    if wc_type == "hold_tile":
        pos = wc.get("pos") or {}
        n = wc.get("consecutive_turns", "?")
        team = wc.get("team", "?")
        return (t("win.hold_tile", locale)
                .replace("{team}", localized_team(team, locale))
                .replace("{x}", str(pos.get("x", "?")))
                .replace("{y}", str(pos.get("y", "?")))
                .replace("{n}", str(n)))
    if wc_type == "reach_goal_line":
        team = wc.get("team", "?")
        return (t("win.reach_goal_line", locale)
                .replace("{team}", localized_team(team, locale))
                .replace("{axis}", str(wc.get("axis", "?")))
                .replace("{value}", str(wc.get("value", "?"))))
    if wc_type == "plugin":
        desc = wc.get("description")
        if desc:
            return str(desc)
        return t("win_desc.custom_plugin", locale).replace("{fn}", str(wc.get("check_fn", "?")))
    return wc_type or t("win_desc.unknown", locale)
