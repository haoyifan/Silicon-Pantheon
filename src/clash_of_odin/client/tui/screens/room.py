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

from clash_of_odin.client.tui.app import POLL_INTERVAL_S, Screen, TUIApp
from clash_of_odin.client.tui.panels import Panel, border_style

log = logging.getLogger("clash.tui.room")


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
            style = "bold cyan" if i == self.selected_idx else "white"
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
            body_parts.append(
                RichPanel(
                    Text(desc, style="white"),
                    title=self.options[self.selected_idx],
                    border_style="yellow",
                    padding=(0, 1),
                )
            )
        body_parts.append(Text(""))
        body_parts.append(footer)
        return Align.center(
            RichPanel(
                Group(*body_parts),
                title=self.title,
                border_style="cyan",
                padding=(1, 3),
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


@dataclass
class UnitCard:
    """Read-only card showing a unit's description / stats / tags /
    abilities / inventory. Rendered inline inside the Map panel — the
    rest of the layout stays visible around it. Esc dismisses.

    Stats come from two sources: the unit dict itself (live match
    state carries them) or the class_spec pulled from
    describe_scenario.unit_classes (room preview only has id+pos so
    stats must come from class_spec). Unit values take priority so
    match-time mutations (HP, status) override the class baseline."""

    unit: dict[str, Any]
    class_spec: dict[str, Any] | None

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
        title = f"{u.get('id', u.get('class', '?'))} — {u.get('class', '?')} ({owner})"

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
        # Engine uses "def" in the unit dict and "defense" in class_spec.
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
        rows.append(Text("Esc to close", style="dim"))

        return RichPanel(
            Group(*rows),
            title=title,
            border_style=team_color,
            padding=(0, 2),
        )

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
        rows: list[RenderableType] = []
        for slot_id in ("a", "b"):
            seat = seats.get(slot_id, {})
            player = seat.get("player") or {}
            name = player.get("display_name") or "(empty)"
            if slot_id == my_slot and name == "(empty)":
                name = s.display_name or "(anonymous)"
            tag = " (you)" if slot_id == my_slot else ""
            ready = "✓" if seat.get("ready") else "…"
            color = "cyan" if slot_id == my_slot else "red"
            rows.append(
                Text(f"{slot_id} [{ready}] {name}{tag}", style=f"bold {color}")
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
                rows.append(Text(f"  • {_describe_win_condition(wc)}", style="dim"))
        if not (story or intro or win_conds):
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


def _describe_win_condition(wc: dict[str, Any]) -> str:
    t = wc.get("type", "")
    if t == "seize_enemy_fort":
        return "seize the enemy fort"
    if t == "eliminate_all_enemy_units":
        return "eliminate every enemy unit"
    if t == "max_turns_draw":
        n = wc.get("turns")
        return f"draw at turn {n}" if n else "draw at the turn cap"
    if t == "protect_unit":
        return f"keep {wc.get('unit_id', '?')} alive ({wc.get('owning_team', '?')})"
    if t == "reach_tile":
        pos = wc.get("pos") or {}
        team = wc.get("team", "?")
        u = wc.get("unit_id")
        who = u or f"any {team} unit"
        return f"{who} reaches ({pos.get('x', '?')}, {pos.get('y', '?')})"
    if t == "hold_tile":
        pos = wc.get("pos") or {}
        n = wc.get("consecutive_turns", "?")
        return (
            f"{wc.get('team', '?')} holds ({pos.get('x', '?')}, {pos.get('y', '?')})"
            f" for {n} turns"
        )
    if t == "reach_goal_line":
        return f"{wc.get('team', '?')} crosses {wc.get('axis', '?')}={wc.get('value', '?')}"
    if t == "plugin":
        return f"plugin rule: {wc.get('check_fn', '?')}"
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
                    style = "bold cyan"
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
        buttons: list[Button] = [
            Button(label="Toggle Ready", action="toggle_ready", enabled=editable),
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
        # Footer: tile / unit info, OR an inline unit card when the
        # player has hit Enter on a unit. The card stays inside the
        # Map panel so the surrounding layout remains visible.
        card = self.screen.unit_card
        if card is not None:
            footer: RenderableType = card.render()
        else:
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
            line.append("   ")
            line.append(
                f"{u.get('class', '?')} ({owner})", style=f"bold {color}"
            )
            line.append("   ")
            line.append("Enter for details", style="dim italic")
        return line

    async def handle_key(self, key: str) -> Screen | None:
        p = self._board()
        w = int(p.get("width", 0))
        h = int(p.get("height", 0))
        if w == 0 or h == 0:
            return None
        # Esc dismisses an open unit card before any cursor movement.
        if key == "esc" and self.screen.unit_card is not None:
            self.screen.unit_card = None
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
        header_line.append(f"Room {room_id[:10]}  ", style="bold cyan")
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
            hints.append("Tab next panel   q quit", style="dim")
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
        # card (Esc/Enter/q close the card instead).
        if key == "q" and self.unit_card is None:
            self.app.exit()
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
        spec = (
            (self.app.state.scenario_description or {}).get("unit_classes") or {}
        ).get(unit.get("class"))
        self.unit_card = UnitCard(unit=unit, class_spec=spec)

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
        return None

    # ---- modal openers ----

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
            prompt="Quit Clash of Odin?",
            on_confirm=_on_confirm,
        )

    def _open_scenario_modal(self) -> None:
        from clash_of_odin.client.tui.screens.scenario_picker import ScenarioPicker

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
            from clash_of_odin.client.tui.screens.game import GameScreen

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
        from clash_of_odin.client.tui.screens.lobby import LobbyScreen

        self.app.state.room_id = None
        self.app.state.slot = None
        return LobbyScreen(self.app)
