"""Room screen — five-panel grid: Map | Player / Actions | Description | Chat.

Tab cycles focus across the focusable panels. Arrows / j-k / Enter
all dispatch to the focused panel. The map panel carries a tile
cursor; Enter on a unit opens a unit-card modal.

Layout
------

    ┌──────────────────────────┬──────────┐
    │                          │ Player   │
    │           Map            ├──────────┤
    │                          │ Actions  │
    ├──────────────────────────┼──────────┤
    │       Description        │   Chat   │
    └──────────────────────────┴──────────┘
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from rich.align import Align
from rich.console import Group, RenderableType
from rich.layout import Layout
from rich.panel import Panel as RichPanel
from rich.table import Table
from rich.text import Text

from silicon_pantheon.client.tui.app import POLL_INTERVAL_S, Screen, TUIApp
from silicon_pantheon.client.tui.panels import Panel, border_style

log = logging.getLogger("silicon.tui.room")


# ---- shared helpers (used by both room and game renderers) ----


def _unit_cell_style(u: dict[str, Any]) -> tuple[str, str]:
    """Pick the (glyph, Rich style) for a unit cell on any map view.

    Honors scenario-provided glyph / color from the unit_classes block.
    Falls back to the first letter of the class name (uppercase for
    blue, lowercase for red) so legacy scenarios still render."""
    cls = str(u.get("class", "") or "")
    owner = u.get("owner")
    glyph = u.get("glyph")
    if not glyph:
        glyph = (cls[:1] or "?")
    glyph = glyph.upper() if owner == "blue" else glyph.lower()
    color = u.get("color")
    if not color:
        color = "cyan" if owner == "blue" else "red"
    return glyph, f"bold {color}"


# ---- modals (shared with the game screen via re-export) ----


@dataclass
class Dropdown:
    """Modal single-select list with an inline explanation box.

    When the caller supplies `option_descriptions`, the currently-
    highlighted option's description is rendered beneath the list so
    the player can read what 'classic' fog actually means before
    committing."""

    title: str
    options: list[str]
    selected_idx: int
    on_confirm: Callable[[str], Awaitable[None]]
    # Optional {option_value: markdown-free explanation}. Missing keys
    # render no description panel — the list stays minimal for truly
    # self-describing options (e.g. team colors).
    option_descriptions: dict[str, str] | None = None

    def render(self) -> RenderableType:
        lines: list[Text] = []
        for i, opt in enumerate(self.options):
            marker = "➤ " if i == self.selected_idx else "  "
            style = "bold yellow" if i == self.selected_idx else "white"
            lines.append(Text(f"{marker}{opt}", style=style))
        list_panel = RichPanel(
            Group(*lines), border_style="dim", padding=(0, 1),
        )
        footer = Text(
            "↑/k up   ↓/j down   Enter select   Esc cancel", style="dim"
        )
        body_parts: list[RenderableType] = [list_panel]
        desc = (self.option_descriptions or {}).get(
            self.options[self.selected_idx] if self.options else ""
        )
        if desc:
            # overflow="fold" + no_wrap=False makes Rich wrap the
            # description across multiple lines inside the panel's
            # fixed width — otherwise a long explanation blew the
            # whole dropdown sideways and the shape changed per
            # option, which read as a shifting modal.
            body_parts.append(
                RichPanel(
                    Text(desc, style="white", no_wrap=False, overflow="fold"),
                    title=self.options[self.selected_idx],
                    border_style="yellow",
                    padding=(0, 1),
                )
            )
        body_parts.append(Text(""))
        body_parts.append(footer)
        # Fixed width (~60 cols) so the modal shape stays stable as
        # the highlight moves between options with different
        # description lengths. Vertical height grows to fit wrapped
        # content; horizontal stays constant.
        return Align.center(
            RichPanel(
                Group(*body_parts),
                title=self.title,
                border_style="yellow",
                padding=(1, 3),
                width=60,
            ),
            vertical="middle",
        )

    async def handle_key(self, key: str) -> bool:
        if key == "esc":
            return True
        if key in ("up", "k"):
            self.selected_idx = (self.selected_idx - 1) % len(self.options)
            return False
        if key in ("down", "j"):
            self.selected_idx = (self.selected_idx + 1) % len(self.options)
            return False
        if key == "enter":
            chosen = self.options[self.selected_idx]
            await self.on_confirm(chosen)
            return True
        return False


@dataclass
class ConfirmModal:
    """Yes/No confirmation overlay. Esc cancels. Enter invokes
    `on_confirm` on the currently-highlighted option."""

    prompt: str
    on_confirm: Callable[[bool], Awaitable[None]]
    selected_yes: bool = False  # default: No, so accidental Enter cancels

    def render(self) -> RenderableType:
        # Yes is the destructive option (leave / concede / quit), so
        # red genuinely conveys "this is the dangerous side". Not a
        # team-color collision in this context — confirm modals never
        # render team status alongside.
        yes = Text(
            "[Yes]",
            style="bold red" if self.selected_yes else "dim",
        )
        no = Text(
            "[No]",
            style="bold green" if not self.selected_yes else "dim",
        )
        row = Text()
        row.append(yes)
        row.append("    ")
        row.append(no)
        body = Group(
            Text(self.prompt, style="white"),
            Text(""),
            Align.center(row),
            Text(""),
            Text("← / → select   Enter confirm   Esc cancel", style="dim"),
        )
        return Align.center(
            RichPanel(body, title="Confirm", border_style="yellow", padding=(1, 3)),
            vertical="middle",
        )

    async def handle_key(self, key: str) -> bool:
        """True = close modal. on_confirm already fired if chosen."""
        if key == "esc":
            return True
        if key in ("left", "h"):
            self.selected_yes = True
            return False
        if key in ("right", "l"):
            self.selected_yes = False
            return False
        if key == "enter":
            await self.on_confirm(self.selected_yes)
            return True
        return False


ART_FRAME_SECONDS = 2.0


@dataclass
class UnitCard:
    """Read-only card showing a unit's description / stats / tags /
    abilities / inventory.

    Holds an ordered list of units the player can browse with
    h/left and l/right while the card is open — the unit_classes
    lookup lets us repaint stats and ASCII art when the highlighted
    unit changes class. The owning MapPanel snaps its cursor to the
    card's currently-displayed unit when the card dismisses, so
    closing always lands you back on the unit you were inspecting."""

    units: list[dict[str, Any]]
    index: int
    unit_classes: dict[str, Any] | None = None
    _opened_at: float | None = None

    @property
    def unit(self) -> dict[str, Any]:
        return self.units[self.index]

    @property
    def class_spec(self) -> dict[str, Any] | None:
        if self.unit_classes is None:
            return None
        return self.unit_classes.get(self.unit.get("class"))

    def _stat(self, key: str, default: str = "?") -> str:
        """Prefer the unit's live value, fall back to class_spec, then
        to the placeholder."""
        u_val = self.unit.get(key)
        if u_val is not None and u_val != "":
            return str(u_val)
        if self.class_spec is not None:
            spec_val = self.class_spec.get(key)
            if spec_val is not None:
                return str(spec_val)
        return default

    def render(self) -> RenderableType:
        u = self.unit
        spec = self.class_spec or {}
        owner = u.get("owner", "?")
        team_color = "cyan" if owner == "blue" else "red"
        display = (
            u.get("display_name")
            or spec.get("display_name")
            or u.get("class")
            or u.get("id", "?")
        )
        title = f"{display} ({owner})"
        frames = u.get("art_frames") or spec.get("art_frames") or []
        text_body = self._render_text_body(team_color)
        if not frames:
            return RichPanel(
                text_body,
                title=title,
                border_style=team_color,
                padding=(0, 2),
            )
        # Two-column layout: text on the left, animated portrait on
        # the right. The portrait column auto-sizes to its widest
        # frame so descriptions on the left always have predictable
        # space and never get clipped by the art.
        import time as _time

        if self._opened_at is None:
            self._opened_at = _time.monotonic()
        elapsed = _time.monotonic() - self._opened_at
        idx = int(elapsed / ART_FRAME_SECONDS) % len(frames)
        frame = frames[idx]
        art_width = max(
            (len(line) for f in frames for line in f.split("\n")),
            default=0,
        )
        # Add a small gutter so art doesn't kiss the right border.
        art_col_width = art_width + 2
        grid = Table.grid(expand=True, padding=(0, 1))
        grid.add_column(ratio=1)
        grid.add_column(no_wrap=True, width=art_col_width)
        grid.add_row(text_body, Text(frame, style=team_color))
        return RichPanel(
            grid,
            title=title,
            border_style=team_color,
            padding=(0, 2),
        )

    def _render_text_body(self, team_color: str) -> RenderableType:
        u = self.unit
        spec = self.class_spec or {}
        rows: list[RenderableType] = []
        desc = spec.get("description") or u.get("description") or ""
        if desc:
            rows.append(Text(desc, style="italic"))
            rows.append(Text(""))

        stats = Table.grid(padding=(0, 2))
        stats.add_column(style="dim")
        stats.add_column()
        hp_now = u.get("hp")
        hp_max = self._stat("hp_max")
        stats.add_row(
            "HP",
            f"{hp_now if hp_now is not None else hp_max} / {hp_max}",
        )
        stats.add_row("ATK", self._stat("atk"))
        def_val = u.get("def")
        if def_val is None:
            def_val = spec.get("defense") or spec.get("def") or "?"
        stats.add_row("DEF", str(def_val))
        stats.add_row("RES", self._stat("res"))
        stats.add_row("SPD", self._stat("spd"))
        stats.add_row("MOVE", self._stat("move"))
        rng = u.get("rng") or [
            spec.get("rng_min", self._stat("rng_min")),
            spec.get("rng_max", self._stat("rng_max")),
        ]
        stats.add_row("RANGE", f"{rng[0]}–{rng[1]}")
        if u.get("is_magic") or spec.get("is_magic"):
            stats.add_row("type", "magic")
        if u.get("can_heal") or spec.get("can_heal"):
            stats.add_row("can_heal", "yes")
        rows.append(stats)

        tags = u.get("tags") or spec.get("tags") or []
        if tags:
            rows.append(Text(""))
            rows.append(Text("tags: " + ", ".join(tags), style="dim"))

        abilities = u.get("abilities") or spec.get("abilities") or []
        if abilities:
            rows.append(Text(""))
            rows.append(Text("abilities: " + ", ".join(abilities)))

        inv = u.get("default_inventory") or spec.get("default_inventory") or []
        if inv:
            rows.append(Text(""))
            rows.append(Text("inventory: " + ", ".join(inv)))

        rows.append(Text(""))
        if len(self.units) > 1:
            rows.append(
                Text("←/h previous · l/→ next · Esc/Enter close", style="dim")
            )
        else:
            rows.append(Text("Esc / Enter to close", style="dim"))
        return Group(*rows)

    def navigate(self, step: int) -> None:
        """Move the highlighted unit by `step` (wraps). Resets the
        animation clock so the new portrait starts at frame 0."""
        if not self.units:
            return
        self.index = (self.index + step) % len(self.units)
        self._opened_at = None

    async def handle_key(self, key: str) -> bool:
        return key in ("esc", "enter", "q")


# ---- panel: Player ----


class PlayerPanel(Panel):
    title = "Player"

    def __init__(self, app: TUIApp) -> None:
        self.app = app

    def key_hints(self) -> str:
        return "(read-only)"

    def render(self, focused: bool) -> RenderableType:
        s = self.app.state
        rs = s.last_room_state or {}
        seats = rs.get("seats", {})
        my_slot = s.slot or "?"
        # When team_assignment is "random", colors haven't been
        # decided yet — using cyan/red for the two slots would imply
        # blue/red assignment that doesn't actually exist. Render
        # neutrally until the assignment is fixed.
        team_mode = rs.get("team_assignment", "fixed")
        rows: list[RenderableType] = []
        for slot_id in ("a", "b"):
            seat = seats.get(slot_id, {})
            player = seat.get("player") or {}
            name = player.get("display_name") or "(empty)"
            if slot_id == my_slot and name == "(empty)":
                name = s.display_name or "(anonymous)"
            tag = " (you)" if slot_id == my_slot else ""
            ready = "✓" if seat.get("ready") else "…"
            if team_mode == "random":
                style = "bold yellow" if slot_id == my_slot else "bold white"
            else:
                # In fixed mode we know who plays what — color slot a
                # by the configured host_team and slot b by the other.
                host_team = rs.get("host_team", "blue")
                slot_team = host_team if slot_id == "a" else (
                    "red" if host_team == "blue" else "blue"
                )
                style = f"bold {'cyan' if slot_team == 'blue' else 'red'}"
            rows.append(
                Text(f"{slot_id} [{ready}] {name}{tag}", style=style)
            )
        rows.append(Text(""))
        rows.append(Text(f"model: {s.model or 'random'}", style="dim"))
        return RichPanel(
            Group(*rows),
            title=self.title,
            border_style=border_style(focused),
            padding=(0, 1),
        )


# ---- panel: Description ----


class DescriptionPanel(Panel):
    title = "Description"

    def __init__(self, app: TUIApp) -> None:
        self.app = app
        self.scroll = 0  # number of rows scrolled down from the top

    def key_hints(self) -> str:
        return "↑/↓ (or k/j) scroll"

    async def handle_key(self, key: str) -> "Screen | None":
        if key in ("down", "j"):
            self.scroll += 1
            return None
        if key in ("up", "k"):
            self.scroll = max(0, self.scroll - 1)
            return None
        return None

    def render(self, focused: bool) -> RenderableType:
        s = self.app.state
        desc = s.scenario_description or {}
        name = desc.get("name") or (s.last_room_state or {}).get("scenario", "?")
        story = (desc.get("description") or "").strip()
        narrative = desc.get("narrative") or {}
        intro = (narrative.get("intro") or "").strip()
        win_conds = desc.get("win_conditions") or []
        armies = desc.get("armies") or {}
        unit_classes = desc.get("unit_classes") or {}

        rows: list[RenderableType] = []
        rows.append(Text(name, style="bold yellow"))
        if story:
            rows.append(Text(""))
            rows.append(Text(story))
        if intro:
            rows.append(Text(""))
            rows.append(Text(intro, style="italic"))
        if win_conds:
            rows.append(Text(""))
            rows.append(Text("How to win:", style="bold"))
            for wc in win_conds:
                rows.append(
                    Text(
                        f"  • {_describe_win_condition(wc, desc)}",
                        style="dim",
                    )
                )
        # Army composition — same shape as the scenario picker so the
        # game-room Description and the picker preview agree on what
        # each side fields. Lists per-class display_name with counts;
        # players read this to plan focus / matchups before ready-up.
        if armies:
            rows.append(Text(""))
            rows.append(Text("Armies:", style="bold"))
            for owner in ("blue", "red"):
                units = armies.get(owner) or []
                if not units:
                    continue
                cls_counts: dict[str, int] = {}
                for u in units:
                    cls_counts[u.get("class", "?")] = (
                        cls_counts.get(u.get("class", "?"), 0) + 1
                    )
                team_color = "cyan" if owner == "blue" else "red"

                def _label(slug: str) -> str:
                    spec = unit_classes.get(slug) or {}
                    return str(spec.get("display_name") or slug)

                summary = ", ".join(
                    f"{n}×{_label(c)}" if n > 1 else _label(c)
                    for c, n in cls_counts.items()
                )
                rows.append(Text(f"  {owner}: {summary}", style=team_color))
        # Per-class details: display name + one-line description from
        # the scenario bundle. Only include classes that are actually
        # in play (cuts noise for scenarios that ship a big roster
        # but only field a few classes).
        # Map each class to the team(s) that field it, so the Units
        # block can color names by side instead of a neutral yellow —
        # the reader wants to know "is this a blue unit or red" at a
        # glance.
        class_teams: dict[str, set[str]] = {}
        for team_name, army in armies.items():
            for u in (army or []):
                class_teams.setdefault(u.get("class"), set()).add(team_name)
        in_play = set(class_teams.keys())
        described = [
            (slug, unit_classes[slug])
            for slug in sorted(in_play)
            if slug and slug in unit_classes and unit_classes[slug].get("description")
        ]
        if described:
            rows.append(Text(""))
            rows.append(Text("Units:", style="bold"))
            for slug, spec in described:
                name_str = spec.get("display_name") or slug
                teams = class_teams.get(slug, set())
                if teams == {"blue"}:
                    name_style = "bold cyan"
                elif teams == {"red"}:
                    name_style = "bold red"
                else:
                    # Mirror matches etc. — class fielded by both sides.
                    name_style = "bold yellow"
                rows.append(Text(f"  {name_str}", style=name_style))
                rows.append(
                    Text(f"    {spec['description'].strip()}", style="dim")
                )
        if not (story or intro or win_conds or armies):
            rows.append(Text("(no scenario description loaded)", style="dim italic"))
        # Simple line-based scroll: drop the first `scroll` rows. Clamp
        # so scrolling past the end doesn't produce an empty panel.
        if self.scroll > 0 and rows:
            self.scroll = min(self.scroll, max(0, len(rows) - 1))
            rows = rows[self.scroll :]
        return RichPanel(
            Group(*rows),
            title=self.title,
            border_style=border_style(focused),
            padding=(0, 1),
        )


def _terrain_cell_style(ttype: str) -> tuple[str, str] | None:
    """Map glyph + style for a terrain type. None means "let the
    default empty-cell render handle it" (i.e. plain). Built-ins are
    hard-coded to keep visuals consistent across renderers; scenarios
    that introduce custom terrain names render via the first letter
    of the name in dim until they're added here too."""
    if ttype == "plain":
        return None
    if ttype == "forest":
        return ("f", "green")
    if ttype == "mountain":
        return ("^", "bright_black")
    if ttype == "fort":
        return ("*", "yellow")
    return (ttype[:1] or "?", "magenta")


def _terrain_effect_summary(
    scenario_description: dict[str, Any] | None, terrain: str
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
        parts.append(f"move={mc}")
    db = spec.get("defense_bonus")
    if db:
        parts.append(f"+{db} DEF")
    rb = spec.get("res_bonus") or spec.get("magic_bonus")
    if rb:
        parts.append(f"+{rb} RES")
    heals = spec.get("heals")
    if heals:
        sign = "+" if heals > 0 else ""
        parts.append(f"{sign}{heals} HP/turn")
    if spec.get("blocks_sight"):
        parts.append("blocks LoS")
    if spec.get("passable") is False:
        parts.append("impassable")
    if spec.get("effects_plugin"):
        parts.append(f"plugin: {spec['effects_plugin']}")
    return ", ".join(parts)


def _strip_frontmatter(text: str) -> str:
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


def _unit_display_name(
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


def _humanize_unit_id(unit_id: str, scenario_description: dict[str, Any] | None) -> str:
    """Translate `u_b_tang_monk_1` → `Tang Monk` when the scenario
    bundle has a display_name for the class. Falls back to the slug
    derived from the id (tang_monk → tang_monk) and finally to the
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


def _other(team: str) -> str:
    return "red" if team == "blue" else "blue"


def _describe_win_condition(
    wc: dict[str, Any],
    scenario_description: dict[str, Any] | None = None,
) -> str:
    """Return a one-line, side-explicit explanation of a win condition.

    Both teams need to read this prose: protect_unit and
    reach_tile are asymmetric (only one side benefits), so each line
    must say WHO wins instead of just describing the trigger. Earlier
    versions said "keep Tang Monk (blue) alive" — true from blue's
    perspective, but a red player reading the same line wouldn't know
    they win when the monk dies.
    """
    t = wc.get("type", "")
    if t == "seize_enemy_fort":
        return "Either side wins by moving a unit onto the opponent's fort."
    if t == "eliminate_all_enemy_units":
        return "Either side wins by eliminating every enemy unit."
    if t == "max_turns_draw":
        n = wc.get("turns")
        return (
            f"Match ends in a draw at turn {n}."
            if n
            else "Match ends in a draw at the turn cap."
        )
    if t == "protect_unit":
        name = _humanize_unit_id(wc.get("unit_id", ""), scenario_description)
        loser = wc.get("owning_team", "?")
        winner = _other(loser)
        return f"{winner.capitalize()} wins if {name} dies (protected by {loser})."
    if t == "reach_tile":
        pos = wc.get("pos") or {}
        team = wc.get("team", "?")
        u = wc.get("unit_id")
        who = _humanize_unit_id(u, scenario_description) if u else f"any {team} unit"
        return (
            f"{team.capitalize()} wins if {who} ends a turn on "
            f"({pos.get('x', '?')}, {pos.get('y', '?')})."
        )
    if t == "hold_tile":
        pos = wc.get("pos") or {}
        n = wc.get("consecutive_turns", "?")
        team = wc.get("team", "?")
        return (
            f"{team.capitalize()} wins by holding "
            f"({pos.get('x', '?')}, {pos.get('y', '?')}) for {n} consecutive turns."
        )
    if t == "reach_goal_line":
        team = wc.get("team", "?")
        return (
            f"{team.capitalize()} wins by crossing "
            f"{wc.get('axis', '?')}={wc.get('value', '?')} with any unit."
        )
    if t == "plugin":
        # Scenarios can attach a description to their check_fn (an
        # explicit `.description` attribute or its docstring's first
        # line). Server-side `describe_scenario` resolves that into
        # the `description` field on this dict, so the client just
        # reads it straight — no plugin imports in the TUI process.
        desc = wc.get("description")
        if desc:
            return str(desc)
        return f"Custom plugin rule: {wc.get('check_fn', '?')}."
    return t or "(unknown rule)"


# ---- panel: Chat (placeholder) ----


class ChatPanel(Panel):
    title = "Chat"

    def key_hints(self) -> str:
        return "(chat not wired yet)"

    def render(self, focused: bool) -> RenderableType:
        body = Text(
            "(chat pipeline not wired yet — see TODO.md)\n\n"
            "Players will be able to type here to chat with each other\n"
            "and with the AI agents while a match is being set up or played.",
            style="dim italic",
        )
        return RichPanel(
            body,
            title=self.title,
            border_style=border_style(focused),
            padding=(0, 1),
        )


# ---- panel: Actions ----


@dataclass
class Button:
    label: str
    action: str
    enabled: bool = True
    value: str | None = None


_FOG_OPTIONS = ("none", "classic", "line_of_sight")
_TEAM_MODE_OPTIONS = ("fixed", "random")
_HOST_TEAM_OPTIONS = ("blue", "red")

_FOG_DESCRIPTIONS = {
    "none": "No fog. Both sides see the entire board at all times. "
            "Best for learning a new scenario.",
    "classic": "Fire Emblem-style fog. A tile is visible if any of "
               "your units is within sight range of it. Once seen, "
               "the tile stays visible for the rest of the match.",
    "line_of_sight": "Strict fog. A tile is visible only while a unit "
                     "can see it THIS turn. Forests and mountains block "
                     "sight past them — use them for ambushes.",
}
_TEAM_MODE_DESCRIPTIONS = {
    "fixed": "Host picks which color they play (see 'Change Host "
             "Team'). The other player gets the opposite color.",
    "random": "Teams are assigned randomly at match start. Useful for "
              "tournaments where first-player advantage matters.",
}
_HOST_TEAM_DESCRIPTIONS = {
    "blue": "Host plays blue, who moves first. The game reveals the "
            "map from blue's perspective in line_of_sight fog.",
    "red": "Host plays red, the second-mover. Good if you want to "
           "give a new player the simpler first-move option.",
}


class ActionsPanel(Panel):
    title = "Actions"

    def __init__(self, screen: "RoomScreen") -> None:
        self.screen = screen
        self.focus = 0

    def key_hints(self) -> str:
        return "↑/↓ select   Enter activate"

    def render(self, focused: bool) -> RenderableType:
        buttons = self._buttons()
        if not buttons:
            body: RenderableType = Text("(no actions)", style="dim")
        else:
            self.focus = max(0, min(self.focus, len(buttons) - 1))
            lines: list[Text] = []
            for i, btn in enumerate(buttons):
                is_focused = focused and i == self.focus
                marker = "➤ " if is_focused else "  "
                label = btn.label
                if btn.value is not None:
                    label = f"{btn.label}: {btn.value}"
                if not btn.enabled:
                    style = "dim strike" if is_focused else "dim"
                elif is_focused:
                    style = "bold yellow"
                else:
                    style = "white"
                lines.append(Text(f"{marker}{label}", style=style))
            body = Group(*lines)
        return RichPanel(
            body,
            title=self.title,
            border_style=border_style(focused),
            padding=(0, 1),
        )

    def _buttons(self) -> list[Button]:
        rs = self.screen.app.state.last_room_state or {}
        is_host = self.screen.app.state.slot == "a"
        editable = rs.get("status") in ("waiting_for_players", "waiting_ready")
        # Strategy is per-player (each side picks their own playbook),
        # not a host-only setting. Renders the current file's stem so
        # the player can see what's loaded without opening the picker.
        strat_label = "(none)"
        sp = self.screen.app.state.strategy_path
        if sp is not None:
            strat_label = sp.stem
        # Lessons toggle: per-player (each side's agent reads/writes
        # its own store), only editable pre-match like strategy.
        lessons_label = "on" if self.screen.app.state.use_lessons else "off"
        buttons: list[Button] = [
            Button(label="Toggle Ready", action="toggle_ready", enabled=editable),
            Button(
                label="Strategy",
                action="change_strategy",
                value=strat_label,
                enabled=editable,
            ),
            Button(
                label="Lessons",
                action="toggle_lessons",
                value=lessons_label,
                enabled=editable,
            ),
        ]
        if is_host:
            buttons.extend(
                [
                    Button(
                        label="Change Scenario",
                        action="change_scenario",
                        value=rs.get("scenario", "?"),
                        enabled=editable and bool(self.screen.scenarios),
                    ),
                    Button(
                        label="Change Fog",
                        action="change_fog",
                        value=rs.get("fog_of_war", "?"),
                        enabled=editable,
                    ),
                    Button(
                        label="Change Team Mode",
                        action="change_teams",
                        value=rs.get("team_assignment", "?"),
                        enabled=editable,
                    ),
                    Button(
                        label="Change Host Team",
                        action="change_host_team",
                        value=rs.get("host_team", "?"),
                        enabled=editable
                        and rs.get("team_assignment") == "fixed",
                    ),
                ]
            )
        buttons.extend(
            [
                Button(label="Leave Room", action="leave"),
                Button(label="Quit", action="quit"),
            ]
        )
        return buttons

    async def handle_key(self, key: str) -> Screen | None:
        buttons = self._buttons()
        if not buttons:
            return None
        if key in ("down", "j"):
            self.focus = (self.focus + 1) % len(buttons)
            return None
        if key in ("up", "k"):
            self.focus = (self.focus - 1) % len(buttons)
            return None
        if key == "enter":
            btn = buttons[self.focus]
            if not btn.enabled:
                return None
            return await self.screen.run_action(btn.action)
        return None


# ---- panel: Map (with tile cursor + unit card modal) ----


class MapPanel(Panel):
    title = "Map"

    def __init__(self, screen: "RoomScreen") -> None:
        self.screen = screen
        self.cx = 0
        self.cy = 0

    def key_hints(self) -> str:
        return "←↑↓→ (or h/j/k/l) move   Enter unit stats"

    def _board(self) -> dict[str, Any]:
        return self.screen.scenario_preview or {}

    def render(self, focused: bool) -> RenderableType:
        # While a unit card is up, give it the entire panel — board
        # + tooltip are hidden so the description / stats / portrait
        # have room to breathe instead of being squeezed between the
        # board and the panel border. Esc / Enter / q closes the card
        # and the board comes back.
        card = self.screen.unit_card
        if card is not None:
            return card.render()

        p = self._board()
        w = int(p.get("width", 0))
        h = int(p.get("height", 0))
        units = p.get("units", [])
        forts = p.get("forts", [])

        # Clamp cursor to current board.
        if w > 0 and h > 0:
            self.cx = max(0, min(self.cx, w - 1))
            self.cy = max(0, min(self.cy, h - 1))

        styled: dict[tuple[int, int], tuple[str, str]] = {}
        # Paint terrain first (forest/mountain/swamp/lava/etc.) so units
        # and forts can overlay it. Without this, scenarios that paint
        # forests in their YAML show up as bare dots in the room
        # preview — mismatching what the live game-state map renders.
        tiles = p.get("tiles") or []
        tile_lookup: dict[tuple[int, int], dict] = {
            (int(t.get("x", -1)), int(t.get("y", -1))): t for t in tiles
        }
        for (tx, ty), t in tile_lookup.items():
            if not (0 <= tx < w and 0 <= ty < h):
                continue
            ttype = str(t.get("type", "plain"))
            cell = _terrain_cell_style(ttype)
            if cell is not None:
                styled[(tx, ty)] = cell
        for f in forts:
            pos = f.get("pos") or {}
            x, y = int(pos.get("x", -1)), int(pos.get("y", -1))
            if 0 <= x < w and 0 <= y < h:
                styled[(x, y)] = ("*", "yellow")
        unit_at: dict[tuple[int, int], dict] = {}
        for u in units:
            pos = u.get("pos") or {}
            x, y = int(pos.get("x", -1)), int(pos.get("y", -1))
            if 0 <= x < w and 0 <= y < h:
                glyph, style = _unit_cell_style(u)
                styled[(x, y)] = (glyph, style)
                unit_at[(x, y)] = u

        text = Text()
        text.append("   " + " ".join(f"{x:>2}" for x in range(w)) + "\n", style="dim")
        for y in range(h):
            text.append(f"{y:>2} ", style="dim")
            for x in range(w):
                cell = styled.get((x, y))
                g, st = (".", "dim") if cell is None else cell
                if focused and x == self.cx and y == self.cy:
                    # Square brackets around the cursor cell — visible even
                    # in monochrome terminals where the reverse style might
                    # not pop.
                    text.append(f"[{g}]", style=f"reverse {st}")
                else:
                    text.append(f" {g} ", style=st)
            text.append("\n")
        footer = self._cursor_tooltip(w, h, unit_at)
        body = Group(text, Text(""), footer)
        return RichPanel(
            body,
            title=self.title,
            border_style=border_style(focused),
            padding=(0, 1),
        )

    def _cursor_tooltip(
        self, w: int, h: int, unit_at: dict[tuple[int, int], dict]
    ) -> RenderableType:
        if w == 0 or h == 0:
            return Text("(loading map…)", style="dim italic")
        pos = (self.cx, self.cy)
        terrain = "plain"
        for t in (self._board().get("tiles") or []):
            if int(t.get("x", -1)) == self.cx and int(t.get("y", -1)) == self.cy:
                terrain = str(t.get("type", "plain"))
                break
        # Forts live in their own list in scenario_preview (legacy
        # compatibility — the engine treats them as a tile type but
        # the preview keeps them separate). Fold them in here so the
        # tooltip says "fort" instead of "plain" on a fort tile.
        for f in self._board().get("forts") or []:
            fpos = f.get("pos") or {}
            if int(fpos.get("x", -1)) == self.cx and int(fpos.get("y", -1)) == self.cy:
                terrain = "fort"
                break
        line = Text()
        line.append(f"({self.cx}, {self.cy}) ", style="dim")
        line.append(f"terrain: {terrain}", style="yellow")
        # Terrain effect summary from the cached scenario bundle.
        summary = _terrain_effect_summary(
            self.screen.app.state.scenario_description, terrain
        )
        if summary:
            line.append(f" — {summary}", style="dim")
        u = unit_at.get(pos)
        if u:
            owner = u.get("owner", "?")
            color = "cyan" if owner == "blue" else "red"
            name = _unit_display_name(u, self.screen.app.state.scenario_description)
            line.append("   ")
            line.append(f"{name} ({owner})", style=f"bold {color}")
            line.append("   ")
            line.append("Enter for details", style="dim italic")
        return line

    async def handle_key(self, key: str) -> Screen | None:
        p = self._board()
        w = int(p.get("width", 0))
        h = int(p.get("height", 0))
        if w == 0 or h == 0:
            return None
        card = self.screen.unit_card
        if card is not None:
            # Card-mode keys win over board navigation.
            #   h / ←   previous unit
            #   l / →   next unit
            #   esc / enter / q   close (and snap cursor to the
            #                      currently-displayed unit, so closing
            #                      always lands you on the unit you
            #                      were inspecting)
            if key in ("left", "h"):
                card.navigate(-1)
                return None
            if key in ("right", "l"):
                card.navigate(1)
                return None
            if key in ("esc", "enter", "q"):
                pos = card.unit.get("pos") or {}
                self.cx = int(pos.get("x", self.cx))
                self.cy = int(pos.get("y", self.cy))
                self.screen.unit_card = None
                return None
            # Other keys (up/down/Tab handled higher) are no-ops while
            # the card is up.
            return None
        if key in ("up", "k"):
            self.cy = (self.cy - 1) % h
            return None
        if key in ("down", "j"):
            self.cy = (self.cy + 1) % h
            return None
        if key in ("left", "h"):
            self.cx = (self.cx - 1) % w
            return None
        if key in ("right", "l"):
            self.cx = (self.cx + 1) % w
            return None
        if key == "enter":
            for u in p.get("units", []):
                pos = u.get("pos") or {}
                if int(pos.get("x", -1)) == self.cx and int(pos.get("y", -1)) == self.cy:
                    self.screen.open_unit_card(u)
                    break
            return None
        return None


# ---- the screen itself ----


class RoomScreen(Screen):
    def __init__(self, app: TUIApp):
        self.app = app
        self.scenario_preview: dict[str, Any] | None = None
        self.scenarios: list[str] = []
        self._last_poll = 0.0
        # Dropdowns (change scenario / fog / teams / host_team) still
        # render full-screen — they're modal single-select lists that
        # make no sense to scope to one panel. UnitCard is different:
        # it renders *inside* the Map panel as an inline overlay so
        # the rest of the UI stays visible.
        self._dropdown: Dropdown | None = None
        self._confirm: ConfirmModal | None = None
        self.unit_card: UnitCard | None = None
        # Full-screen scenario picker (richer than a one-line dropdown
        # — see screens/scenario_picker.py for layout). Only the host
        # opens this.
        self._scenario_picker = None
        # Pending screen transition queued from a ConfirmModal callback.
        self._pending_transition: Screen | None = None

        # Build panels. Order matters: Tab cycles in this order.
        self.map_panel = MapPanel(self)
        self.actions_panel = ActionsPanel(self)
        self._panels: list[Panel] = [
            self.map_panel,
            PlayerPanel(app),
            self.actions_panel,
            DescriptionPanel(app),
            ChatPanel(),
        ]
        # Start focus on the Actions panel — that's where Toggle Ready
        # lives, which is what the player almost always wants first.
        self._focus_idx = self._panels.index(self.actions_panel)

    async def on_enter(self, app: TUIApp) -> None:
        await self._load_preview()
        await self._refresh_state()
        await self._load_scenarios()

    # ---- render ----

    def render(self) -> RenderableType:
        if self._confirm is not None:
            return self._confirm.render()
        if self._scenario_picker is not None:
            return self._scenario_picker.render()
        if self._dropdown is not None:
            return self._dropdown.render()

        rs = self.app.state.last_room_state or {}
        room_id = rs.get("room_id") or self.app.state.room_id or "?"
        scenario = rs.get("scenario", "?")
        status = rs.get("status", "?")
        countdown = rs.get("autostart_in_s")

        header_line = Text()
        # Room id + scenario header are neutral chrome — use yellow so
        # they don't read as "this room belongs to the blue team".
        header_line.append(f"Room {room_id[:10]}  ", style="bold white")
        header_line.append(scenario, style="yellow")
        header_line.append(f"   fog={rs.get('fog_of_war', '?')}", style="dim")
        header_line.append(f"   teams={rs.get('team_assignment', '?')}", style="dim")
        if countdown is not None:
            header_line.append(
                f"   match starting in {countdown:.1f}s", style="bold yellow"
            )
        elif status == "waiting_for_players":
            header_line.append("   waiting for opponent…", style="dim")
        elif status == "waiting_ready":
            header_line.append("   ready up to start", style="green")

        # Footer: errors take priority, otherwise the focused panel's
        # own key hints + the always-on Tab/q hints.
        if self.app.state.error_message:
            footer_line: RenderableType = Text(
                self.app.state.error_message, style="red"
            )
        else:
            focused = self._panels[self._focus_idx]
            hints = Text()
            panel_hints = focused.key_hints()
            if panel_hints:
                hints.append(f"[{focused.title}] ", style="bold yellow")
                hints.append(panel_hints, style="white")
                hints.append("   ", style="dim")
            hints.append("Tab next panel   F2 help   q quit", style="dim")
            footer_line = hints

        # Put header / body / footer all inside a single Layout with
        # fixed-size header+footer and a flexible body. Rich's Live
        # sizes the root Layout to the full console, so every row is
        # accounted for — the bottom panels no longer get clipped in
        # tmux or short terminals.
        root = Layout()
        root.split_column(
            Layout(name="hdr", size=1),
            Layout(name="body"),
            Layout(name="ftr", size=1),
        )
        root["hdr"].update(header_line)
        root["body"].update(self._build_body())
        root["ftr"].update(footer_line)
        return root

    def _build_body(self) -> Layout:
        body = Layout()
        body.split_column(
            Layout(name="top", ratio=3),
            Layout(name="bottom", ratio=2),
        )
        body["top"].split_row(
            Layout(name="map", ratio=2),
            Layout(name="right", ratio=1),
        )
        body["top"]["right"].split_column(
            Layout(name="player", ratio=2),
            Layout(name="actions", ratio=3),
        )
        body["bottom"].split_row(
            Layout(name="description", ratio=2),
            Layout(name="chat", ratio=1),
        )

        focused_panel = self._panels[self._focus_idx]
        body["top"]["map"].update(
            self.map_panel.render(focused_panel is self.map_panel)
        )
        body["top"]["right"]["player"].update(
            self._panels[1].render(focused_panel is self._panels[1])
        )
        body["top"]["right"]["actions"].update(
            self.actions_panel.render(focused_panel is self.actions_panel)
        )
        body["bottom"]["description"].update(
            self._panels[3].render(focused_panel is self._panels[3])
        )
        body["bottom"]["chat"].update(
            self._panels[4].render(focused_panel is self._panels[4])
        )
        return body

    # ---- input ----

    async def handle_key(self, key: str) -> Screen | None:
        # Confirmation overlays win over everything. When the modal
        # closes and staged a transition (e.g. Leave Room → Lobby),
        # surface it here.
        if self._confirm is not None:
            close = await self._confirm.handle_key(key)
            if close:
                self._confirm = None
                pending = self._pending_transition
                self._pending_transition = None
                return pending
            return None
        if self._scenario_picker is not None:
            close = await self._scenario_picker.handle_key(key)
            if close:
                self._scenario_picker = None
            return None
        # Full-screen dropdowns swallow all input while open.
        if self._dropdown is not None:
            close = await self._dropdown.handle_key(key)
            if close:
                self._dropdown = None
            return None

        # Global exit — but not when the Map panel has an open unit
        # card (Esc/Enter/q close the card instead). Route through
        # the same ConfirmModal the Quit button uses so q and the
        # button behave identically — quitting silently used to be
        # a footgun with another player waiting in the room.
        if key == "q" and self.unit_card is None:
            self._open_quit_confirm()
            return None
        if key == "\t":
            # Close any stale unit card on Tab so the next panel has
            # a clean state.
            self.unit_card = None
            self._focus_next(1)
            return None
        focused = self._panels[self._focus_idx]
        return await focused.handle_key(key)

    def _focus_next(self, step: int) -> None:
        n = len(self._panels)
        if n == 0:
            return
        i = self._focus_idx
        for _ in range(n):
            i = (i + step) % n
            if self._panels[i].can_focus():
                self._focus_idx = i
                return

    # ---- public API used by panels ----

    def open_unit_card(self, unit: dict[str, Any]) -> None:
        units = self._navigable_units()
        try:
            idx = units.index(unit)
        except ValueError:
            idx = 0
            units = [unit] + units
        unit_classes = (
            self.app.state.scenario_description or {}
        ).get("unit_classes") or {}
        self.unit_card = UnitCard(units=units, index=idx, unit_classes=unit_classes)

    def _navigable_units(self) -> list[dict[str, Any]]:
        """Units the unit-card cycle (h / l) walks through. Sorted by
        (y, x) so 'next' moves left-to-right top-to-bottom across the
        board, which matches how players read the map."""
        p = self.scenario_preview or {}
        units = list(p.get("units") or [])
        units.sort(
            key=lambda u: (
                int((u.get("pos") or {}).get("y", 0)),
                int((u.get("pos") or {}).get("x", 0)),
            )
        )
        return units

    async def run_action(self, action: str) -> Screen | None:
        if action == "toggle_ready":
            await self._toggle_ready()
            return None
        if action == "leave":
            self._open_leave_confirm()
            return None
        if action == "quit":
            self._open_quit_confirm()
            return None
        if action == "change_scenario":
            self._open_scenario_modal()
            return None
        if action == "change_fog":
            self._open_fog_modal()
            return None
        if action == "change_teams":
            self._open_teams_modal()
            return None
        if action == "change_host_team":
            self._open_host_team_modal()
            return None
        if action == "change_strategy":
            self._open_strategy_modal()
            return None
        if action == "toggle_lessons":
            self.app.state.use_lessons = not self.app.state.use_lessons
            return None
        return None

    # ---- modal openers ----

    def _open_strategy_modal(self) -> None:
        """Pick a playbook from strategies/*.md (plus a `(none)` opt
        to clear). Each player sets their own — doesn't affect the
        opponent. Saved on the local SharedState only; the agent
        reads it at game start to inject into its system prompt."""
        from pathlib import Path as _Path

        # Discover strategies/*.md relative to either the CWD or the
        # repo root (works for `uv run silicon-join` from any depth).
        candidates: list[_Path] = []
        for root in (_Path("."), _Path.cwd(), _Path(__file__).resolve().parents[5]):
            d = root / "strategies"
            if d.is_dir():
                candidates = sorted(d.glob("*.md"))
                if candidates:
                    break
        # Strip a README if present — it's not a playbook.
        candidates = [p for p in candidates if p.stem.lower() != "readme"]
        options = ["(none)"] + [p.stem for p in candidates]
        # Highlight the currently-selected stem if any.
        cur = "(none)"
        if self.app.state.strategy_path is not None:
            cur = self.app.state.strategy_path.stem
        idx = options.index(cur) if cur in options else 0

        async def _on_pick(chosen: str) -> None:
            if chosen == "(none)":
                self.app.state.strategy_path = None
                self.app.state.strategy_text = None
                return
            for p in candidates:
                if p.stem == chosen:
                    self.app.state.strategy_path = p
                    try:
                        self.app.state.strategy_text = p.read_text(encoding="utf-8")
                    except OSError as e:
                        self.app.state.error_message = f"strategy read failed: {e}"
                        self.app.state.strategy_text = None
                    return

        # Show the full strategy text in the description box so the
        # player can read the playbook before committing. Rich wraps
        # it inside the fixed-width modal; the box grows vertically.
        # Drop YAML frontmatter if any so it's just the prose.
        descriptions: dict[str, str] = {"(none)": "No playbook. The agent uses only its baseline behavior."}
        for p in candidates:
            try:
                txt = p.read_text(encoding="utf-8")
            except OSError:
                continue
            descriptions[p.stem] = _strip_frontmatter(txt).strip()

        self._dropdown = Dropdown(
            title="Pick Strategy (your side only)",
            options=options,
            selected_idx=idx,
            on_confirm=_on_pick,
            option_descriptions=descriptions,
        )

    def _open_leave_confirm(self) -> None:
        async def _on_confirm(yes: bool) -> None:
            if yes:
                self._pending_transition = await self._leave()

        self._confirm = ConfirmModal(
            prompt="Leave this room and return to the lobby?",
            on_confirm=_on_confirm,
        )

    def _open_quit_confirm(self) -> None:
        async def _on_confirm(yes: bool) -> None:
            if yes:
                self.app.exit()

        self._confirm = ConfirmModal(
            prompt="Quit SiliconPantheon?",
            on_confirm=_on_confirm,
        )

    def _open_scenario_modal(self) -> None:
        from silicon_pantheon.client.tui.screens.scenario_picker import ScenarioPicker

        current = (self.app.state.last_room_state or {}).get("scenario")
        options = list(self.scenarios) or [current or "01_tiny_skirmish"]

        async def _on_confirm(chosen: str) -> None:
            await self._apply_config({"scenario": chosen})

        picker = ScenarioPicker(
            scenarios=options,
            current=current or options[0],
            client=self.app.client,
            on_confirm=_on_confirm,
        )
        self._scenario_picker = picker
        # Kick off the first scenario's data fetch so the preview
        # shows up without waiting for the user to move.
        import asyncio

        asyncio.create_task(picker.prefetch_current())

    def _open_fog_modal(self) -> None:
        current = (self.app.state.last_room_state or {}).get("fog_of_war", "none")
        idx = _FOG_OPTIONS.index(current) if current in _FOG_OPTIONS else 0
        self._dropdown = Dropdown(
            title="Change Fog of War",
            options=list(_FOG_OPTIONS),
            selected_idx=idx,
            on_confirm=lambda v: self._apply_config({"fog_of_war": v}),
            option_descriptions=dict(_FOG_DESCRIPTIONS),
        )

    def _open_teams_modal(self) -> None:
        current = (self.app.state.last_room_state or {}).get(
            "team_assignment", "fixed"
        )
        idx = (
            _TEAM_MODE_OPTIONS.index(current)
            if current in _TEAM_MODE_OPTIONS
            else 0
        )
        self._dropdown = Dropdown(
            title="Change Team Assignment",
            options=list(_TEAM_MODE_OPTIONS),
            selected_idx=idx,
            on_confirm=lambda v: self._apply_config({"team_assignment": v}),
            option_descriptions=dict(_TEAM_MODE_DESCRIPTIONS),
        )

    def _open_host_team_modal(self) -> None:
        current = (self.app.state.last_room_state or {}).get("host_team", "blue")
        idx = (
            _HOST_TEAM_OPTIONS.index(current)
            if current in _HOST_TEAM_OPTIONS
            else 0
        )
        self._dropdown = Dropdown(
            title="Change Host Team",
            options=list(_HOST_TEAM_OPTIONS),
            selected_idx=idx,
            on_confirm=lambda v: self._apply_config({"host_team": v}),
            option_descriptions=dict(_HOST_TEAM_DESCRIPTIONS),
        )

    # ---- server interactions ----

    async def tick(self) -> None:
        import time as _time

        now = _time.time()
        if now - self._last_poll >= POLL_INTERVAL_S:
            await self._refresh_state()
        if self._scenario_picker is not None:
            await self._scenario_picker.tick()

    async def _load_preview(self) -> None:
        if self.app.client is None or self.app.state.room_id is None:
            return
        try:
            r = await self.app.client.call(
                "preview_room", room_id=self.app.state.room_id
            )
        except Exception as e:
            self.app.state.error_message = f"preview failed: {e}"
            return
        if not r.get("ok"):
            return
        room = r.get("room") or {}
        self.scenario_preview = room.get("scenario_preview", {})
        # Read the scenario name directly from preview_room so this
        # works on the very first on_enter call — before
        # _refresh_state has populated last_room_state. Previously we
        # read it from last_room_state, which was None on first load,
        # so describe_scenario silently never fired for the non-host
        # and the Description panel stayed empty + the UnitCard had
        # no stats to render (preview units carry only pos+glyph).
        scenario_name = room.get("scenario")
        if scenario_name:
            try:
                desc = await self.app.client.call(
                    "describe_scenario", name=scenario_name
                )
            except Exception:
                desc = None
            if desc and desc.get("ok"):
                self.app.state.scenario_description = desc

    async def _load_scenarios(self) -> None:
        if self.app.client is None:
            return
        try:
            r = await self.app.client.call("list_scenarios")
        except Exception:
            return
        if r.get("ok"):
            self.scenarios = r.get("scenarios", [])

    async def _refresh_state(self) -> Screen | None:
        import time as _time

        self._last_poll = _time.time()
        if self.app.client is None:
            return None
        try:
            r = await self.app.client.call("get_room_state")
        except Exception as e:
            self.app.state.error_message = f"get_room_state failed: {e}"
            return None
        if not r.get("ok"):
            self.app.state.error_message = r.get("error", {}).get(
                "message", "get_room_state rejected"
            )
            log.warning("get_room_state rejected: %s", r)
            return None
        self.app.state.error_message = ""
        room = r.get("room", {})
        prior_scenario = (self.app.state.last_room_state or {}).get("scenario")
        self.app.state.last_room_state = room
        if room.get("status") == "in_game":
            from silicon_pantheon.client.tui.screens.game import GameScreen

            next_screen = GameScreen(self.app)
            await self.app.transition(next_screen)
            return next_screen
        if prior_scenario and room.get("scenario") != prior_scenario:
            await self._load_preview()
        return None

    async def _apply_config(self, fields: dict[str, Any]) -> None:
        if self.app.client is None:
            return
        try:
            r = await self.app.client.call("update_room_config", **fields)
        except Exception as e:
            self.app.state.error_message = f"update_room_config failed: {e}"
            return
        if not r.get("ok"):
            self.app.state.error_message = r.get("error", {}).get(
                "message", "update_room_config rejected"
            )
            return
        self.app.state.error_message = ""
        if "scenario" in fields:
            await self._load_preview()
        await self._refresh_state()

    async def _toggle_ready(self) -> None:
        if self.app.client is None:
            return
        rs = self.app.state.last_room_state or {}
        slot = self.app.state.slot
        seats = rs.get("seats", {})
        my_seat = seats.get(slot or "", {})
        currently_ready = bool(my_seat.get("ready"))
        try:
            r = await self.app.client.call(
                "set_ready", ready=not currently_ready
            )
        except Exception as e:
            self.app.state.error_message = f"set_ready failed: {e}"
            return
        if not r.get("ok"):
            self.app.state.error_message = r.get("error", {}).get(
                "message", "set_ready rejected"
            )
            return
        self.app.state.error_message = ""
        await self._refresh_state()

    async def _leave(self) -> Screen | None:
        if self.app.client is None:
            return None
        try:
            await self.app.client.call("leave_room")
        except Exception as e:
            self.app.state.error_message = f"leave_room failed: {e}"
            return None
        from silicon_pantheon.client.tui.screens.lobby import LobbyScreen

        self.app.state.room_id = None
        self.app.state.slot = None
        return LobbyScreen(self.app)
