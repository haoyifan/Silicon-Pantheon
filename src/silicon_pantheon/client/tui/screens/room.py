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

from silicon_pantheon.client.locale import t
from silicon_pantheon.client.tui.app import POLL_INTERVAL_S, Screen, TUIApp
from silicon_pantheon.client.tui.panels import Panel, border_style, estimate_panel_height, wrap_rows_to_width
from silicon_pantheon.client.tui.widgets import (
    ART_FRAME_SECONDS,
    ConfirmModal,
    Dropdown,
    UnitCard,
)
from silicon_pantheon.client.tui.scenario_display import (
    describe_win_condition,
    filter_win_conditions,
    other_team,
    strip_frontmatter,
    terrain_effect_summary,
    unit_cell_style,
    unit_display_name,
)

# Re-export for backward compatibility (external tools may import from here)
__all__ = [
    "ART_FRAME_SECONDS", "ConfirmModal", "Dropdown", "UnitCard",
    "describe_win_condition", "other_team", "terrain_effect_summary",
    "unit_cell_style", "unit_display_name", "strip_frontmatter",
]

log = logging.getLogger("silicon.tui.room")


# ---- panel: Player ----


class PlayerPanel(Panel):
    @property
    def title(self):
        return t("room_panels.player", self.app.state.locale)

    def __init__(self, app: TUIApp) -> None:
        self.app = app

    def key_hints(self) -> str:
        return t("room_player.read_only", self.app.state.locale)

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
            lc = self.app.state.locale
            _empty = t("room_player.empty", lc)
            name = player.get("display_name") or _empty
            if slot_id == my_slot and name == _empty:
                name = s.display_name or t("room_player.anonymous", lc)
            tag = f" {t('room_player.you_tag', lc)}" if slot_id == my_slot else ""
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
            # Show provider/model for each player — from server metadata
            # for the opponent, from local state for ourselves.
            if slot_id == my_slot:
                p_model = s.model
                p_provider = s.provider
            else:
                p_model = player.get("model")
                p_provider = player.get("provider")
            if p_model or p_provider:
                label = p_model or ""
                if p_provider and p_provider != label:
                    label = f"{p_provider}/{label}" if label else p_provider
                rows.append(Text(f"     {t('room_player.model_label', lc)}: {label}", style="dim"))
        return RichPanel(
            Group(*rows),
            title=self.title,
            border_style=border_style(focused),
            padding=(0, 1),
        )


# ---- panel: Description ----


class DescriptionPanel(Panel):
    @property
    def title(self):
        return t("room_panels.description", self.app.state.locale)

    def __init__(self, app: TUIApp, *, fullscreen: bool = False) -> None:
        self.app = app
        self.scroll = 0  # number of rows scrolled down from the top
        self._gg: list[bool] = [False]
        self._fullscreen = fullscreen  # True when used as F3 overlay

    def key_hints(self) -> str:
        return t("room_desc.key_hints", self.app.state.locale)

    async def handle_key(self, key: str) -> "Screen | None":
        from silicon_pantheon.client.tui.panels import apply_vim_scroll

        nxt = apply_vim_scroll(key, current=self.scroll, gg_state=self._gg)
        if nxt is not None:
            self.scroll = nxt
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

        difficulty = desc.get("difficulty", 3)
        lc = self.app.state.locale

        rows: list[RenderableType] = []
        rows.append(Text(name, style="bold yellow"))
        if story:
            rows.append(Text(""))
            rows.append(Text(story))
        if intro:
            rows.append(Text(""))
            rows.append(Text(intro, style="italic"))
        rows.append(Text(""))
        level_name = t(f"difficulty.{difficulty}", lc)
        diff_text = Text()
        diff_text.append(f"{t('difficulty.label', lc)}: ", style="bold")
        diff_text.append("★" * difficulty + "☆" * (5 - difficulty), style="yellow")
        diff_text.append(f" ({level_name})", style="dim")
        rows.append(diff_text)
        if win_conds:
            rows.append(Text(""))
            rows.append(Text(t("section.how_to_win", self.app.state.locale), style="bold"))
            for wc in filter_win_conditions(win_conds):
                rows.append(
                    Text(
                        f"  • {describe_win_condition(wc, desc, self.app.state.locale)}",
                        style="dim",
                    )
                )
        # Army composition — same shape as the scenario picker so the
        # game-room Description and the picker preview agree on what
        # each side fields. Lists per-class display_name with counts;
        # players read this to plan focus / matchups before ready-up.
        if armies:
            rows.append(Text(""))
            rows.append(Text(t("section.armies", self.app.state.locale), style="bold"))
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
                from silicon_pantheon.client.tui.scenario_display import localized_team
                rows.append(Text(f"  {localized_team(owner, self.app.state.locale)}: {summary}", style=team_color))
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
            rows.append(Text(t("section.units", self.app.state.locale), style="bold"))
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
            rows.append(Text(t("room_desc.no_description", self.app.state.locale), style="dim italic"))
        # Pre-wrap long logical lines (scenario story / multi-line
        # plugin win-rule descriptions / unit blurbs) into one Text
        # per visible row so scroll advances per-line, not per-block.
        # Description panel occupies the full width of its layout
        # slot; approximate inner width off the console width.
        try:
            cw = self.app.console.width
            ch = self.app.console.height
        except Exception:
            cw = 100
            ch = 30
        rows = wrap_rows_to_width(rows, max(20, int(cw * 2 / 3) - 6))
        # Clamp scroll to keep a FULL visible window at the end —
        # without this the panel shrinks to 1 row when you scroll
        # past the content.  When used as the F3 overlay the panel
        # gets nearly the full terminal; in the room screen it sits
        # in the bottom row of a 3:2 column split (~2/5 of height).
        if self._fullscreen:
            panel_height = max(1, ch - 4)  # full screen minus footer + borders
        else:
            panel_height = estimate_panel_height(ch, 2/5)
        visible_window = max(1, panel_height)
        max_scroll = max(0, len(rows) - visible_window)
        if self.scroll > max_scroll:
            self.scroll = max_scroll
        if self.scroll > 0 and rows:
            rows = rows[self.scroll :]
        return RichPanel(
            Group(*rows),
            title=self.title,
            border_style=border_style(focused),
            padding=(0, 1),
        )


from silicon_pantheon.client.tui.terrain import terrain_cell as _terrain_cell  # noqa: E402


# ---- panel: Chat (placeholder) ----


class ChatPanel(Panel):
    def __init__(self, app: TUIApp) -> None:
        self.app = app

    @property
    def title(self):
        return t("room_panels.chat", self.app.state.locale)

    def key_hints(self) -> str:
        return t("room_chat.not_wired", self.app.state.locale)

    def render(self, focused: bool) -> RenderableType:
        lc = self.app.state.locale
        body = Text(
            t("room_panels.chat_placeholder", lc) + "\n" + t("room_chat.not_wired", lc),
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
# Per-turn time limit (seconds). Same knob drives both the server's
# turn-timer forfeit and the AI agent's own per-turn loop budget.
# Short values punish weak models; long values are necessary for
# scenarios with many units or reasoning models that take ~15-20s
# per tool call.
_TURN_TIME_OPTIONS = ("60", "180", "600", "1800", "3600")
def _turn_time_descriptions(lc: str) -> dict[str, str]:
    return {
        "60": t("dropdown_desc.turn_60", lc),
        "180": t("dropdown_desc.turn_180", lc),
        "600": t("dropdown_desc.turn_600", lc),
        "1800": t("dropdown_desc.turn_1800", lc),
        "3600": t("dropdown_desc.turn_3600", lc),
    }

def _fog_descriptions(lc: str) -> dict[str, str]:
    return {
        "none": t("dropdown_desc.fog_none", lc),
        "classic": t("dropdown_desc.fog_classic", lc),
        "line_of_sight": t("dropdown_desc.fog_los", lc),
    }

def _team_mode_descriptions(lc: str) -> dict[str, str]:
    return {
        "fixed": t("dropdown_desc.team_fixed", lc),
        "random": t("dropdown_desc.team_random", lc),
    }

def _host_team_descriptions(lc: str) -> dict[str, str]:
    return {
        "blue": t("dropdown_desc.host_blue", lc),
        "red": t("dropdown_desc.host_red", lc),
    }


class ActionsPanel(Panel):
    @property
    def title(self):
        return t("room_panels.actions", self.screen.app.state.locale)

    def __init__(self, screen: "RoomScreen") -> None:
        self.screen = screen
        self.focus = 0

    def key_hints(self) -> str:
        return t("room_actions.key_hints", self.screen.app.state.locale)

    def render(self, focused: bool) -> RenderableType:
        buttons = self._buttons()
        if not buttons:
            body: RenderableType = Text(t("room_actions.no_actions", self.screen.app.state.locale), style="dim")
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
        lc = self.screen.app.state.locale
        strat_label = t("button_val.none", lc)
        sp = self.screen.app.state.strategy_path
        if sp is not None:
            strat_label = sp.stem
        sel = self.screen.app.state.selected_lessons
        if sel:
            lessons_label = f"{len(sel)} selected"
        else:
            lessons_label = t("button_val.none", lc)
        buttons: list[Button] = [
            Button(label=t("room_buttons.toggle_ready", lc), action="toggle_ready", enabled=editable),
            Button(
                label=t("room_buttons.strategy", lc),
                action="change_strategy",
                value=strat_label,
                enabled=editable,
            ),
            Button(
                label=t("room_buttons.lessons", lc),
                action="change_lessons",
                value=lessons_label,
                enabled=editable,
            ),
        ]
        if is_host:
            buttons.extend(
                [
                    Button(
                        label=t("room_buttons.change_scenario", lc),
                        action="change_scenario",
                        value=rs.get("scenario", "?"),
                        enabled=editable and bool(self.screen.scenarios),
                    ),
                    Button(
                        label=t("room_buttons.change_fog", lc),
                        action="change_fog",
                        value=rs.get("fog_of_war", "?"),
                        enabled=editable,
                    ),
                    Button(
                        label=t("room_buttons.change_teams", lc),
                        action="change_teams",
                        value=rs.get("team_assignment", "?"),
                        enabled=editable,
                    ),
                    Button(
                        label=t("room_buttons.change_host_team", lc),
                        action="change_host_team",
                        value=rs.get("host_team", "?"),
                        enabled=editable
                        and rs.get("team_assignment") == "fixed",
                    ),
                    Button(
                        label=t("room_buttons.turn_time", lc),
                        action="change_turn_time",
                        value=f"{rs.get('turn_time_limit_s', '?')}s",
                        enabled=editable,
                    ),
                ]
            )
            # Kick button: host can kick the joiner pre-game.
            seats = rs.get("seats", {})
            joiner_occupied = seats.get("b", {}).get("occupied", False)
            buttons.append(
                Button(
                    label=t("room_buttons.kick", lc),
                    action="kick_player",
                    enabled=editable and joiner_occupied,
                ),
            )
        buttons.extend(
            [
                Button(label=t("room_buttons.leave", lc), action="leave"),
                Button(label=t("room_buttons.quit_game", lc), action="quit"),
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
    @property
    def title(self):
        return t("room_panels.map", self.screen.app.state.locale)

    def __init__(self, screen: "RoomScreen") -> None:
        self.screen = screen
        self.cx = 0
        self.cy = 0

    def key_hints(self) -> str:
        return t("room_map.key_hints", self.screen.app.state.locale)

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
        # Pull scenario-declared terrain_types so custom terrain
        # (06_agincourt's mud, Troy's xanthus, etc.) renders with the
        # author-specified glyph + color rather than a fallback letter.
        scenario_terrain_types = (
            self.screen.app.state.scenario_description or {}
        ).get("terrain_types") or {}
        for (tx, ty), t in tile_lookup.items():
            if not (0 <= tx < w and 0 <= ty < h):
                continue
            ttype = str(t.get("type", "plain"))
            # Skip plain so we don't fill the styled dict with the
            # default cell — the render path falls back to (".", "dim")
            # already. (Same effect, smaller dict.)
            if ttype == "plain":
                continue
            styled[(tx, ty)] = _terrain_cell(ttype, scenario_terrain_types)
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
                glyph, style = unit_cell_style(u)
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
        _lc = self.screen.app.state.locale
        if w == 0 or h == 0:
            return Text(t("room_map.loading", _lc), style="dim italic")
        pos = (self.cx, self.cy)
        terrain = "plain"
        for tile in (self._board().get("tiles") or []):
            if int(tile.get("x", -1)) == self.cx and int(tile.get("y", -1)) == self.cy:
                terrain = str(tile.get("type", "plain"))
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
        line.append(f"{t('game_map.terrain_label', _lc)}: {terrain}", style="yellow")
        # Terrain effect summary from the cached scenario bundle.
        summary = terrain_effect_summary(
            self.screen.app.state.scenario_description, terrain, _lc
        )
        if summary:
            line.append(f" — {summary}", style="dim")
        u = unit_at.get(pos)
        if u:
            owner = u.get("owner", "?")
            color = "cyan" if owner == "blue" else "red"
            name = unit_display_name(u, self.screen.app.state.scenario_description)
            line.append("   ")
            line.append(f"{name} ({owner})", style=f"bold {color}")
            line.append("   ")
            line.append(t("room_map.enter_details", _lc), style="dim italic")
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
            ChatPanel(app),
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
        lc = self.app.state.locale
        header_line.append(f"{t('room_status.room_label', lc)} {room_id[:10]}  ", style="bold white")
        header_line.append(scenario, style="yellow")
        header_line.append(f"   {t('room_status.fog_label', lc)}={rs.get('fog_of_war', '?')}", style="dim")
        header_line.append(f"   {t('room_status.teams_label', lc)}={rs.get('team_assignment', '?')}", style="dim")
        if countdown is not None:
            header_line.append(
                f"   {t('room_status.match_starting', lc).replace('{s}', f'{countdown:.1f}')}", style="bold yellow"
            )
        elif status == "waiting_for_players":
            header_line.append(f"   {t('room_status.waiting_opponent', lc)}", style="dim")
        elif status == "waiting_ready":
            header_line.append(f"   {t('room_status.ready_up', lc)}", style="green")

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
            hints.append(f"{t('keys.tab_next', lc)}   {t('keys.help', lc)}   {t('keys.quit', lc)}", style="dim")
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
        self.unit_card = UnitCard(units=units, index=idx, unit_classes=unit_classes, locale=self.app.state.locale)

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
        if action == "change_turn_time":
            self._open_turn_time_modal()
            return None
        if action == "change_strategy":
            self._open_strategy_modal()
            return None
        if action == "change_lessons":
            self._open_lessons_modal()
            return None
        if action == "kick_player":
            await self._kick_player()
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
        descriptions: dict[str, str] = {"(none)": t("room_strategy.no_playbook", self.app.state.locale)}
        for p in candidates:
            try:
                txt = p.read_text(encoding="utf-8")
            except OSError:
                continue
            descriptions[p.stem] = strip_frontmatter(txt).strip()

        self._dropdown = Dropdown(
            title=t("room_buttons.pick_strategy", self.app.state.locale),
            options=options,
            selected_idx=idx,
            on_confirm=_on_pick,
            option_descriptions=descriptions,
            locale=self.app.state.locale,
        )

    def _open_lessons_modal(self) -> None:
        from pathlib import Path as _Path

        from silicon_pantheon.lessons import LessonStore

        scenario = (self.app.state.last_room_state or {}).get("scenario") or ""
        store = LessonStore(_Path("lessons"))
        all_lessons = store.list_for_scenario(scenario) if scenario else []

        options = [t("button_val.none", self.app.state.locale)]
        # Map option label → lesson path for the callback.
        label_to_path: dict[str, _Path] = {}
        descriptions: dict[str, str] = {
            options[0]: t("room_strategy.no_playbook", self.app.state.locale),
        }
        for lesson in all_lessons:
            path = store.root / lesson.scenario / f"{lesson.slug}.md"
            label = lesson.title or lesson.slug
            # Disambiguate duplicate titles with a suffix.
            base = label
            n = 2
            while label in label_to_path:
                label = f"{base} ({n})"
                n += 1
            options.append(label)
            label_to_path[label] = path
            # Description: outcome + body preview.
            header = f"[{lesson.outcome}] {lesson.model} — {lesson.created_at}"
            descriptions[label] = f"{header}\n\n{lesson.body[:500]}"

        cur = options[0]
        if self.app.state.selected_lessons:
            # Try to highlight the first currently-selected lesson.
            for lbl, p in label_to_path.items():
                if p in self.app.state.selected_lessons:
                    cur = lbl
                    break
        idx = options.index(cur) if cur in options else 0

        async def _on_pick(chosen: str) -> None:
            if chosen == options[0]:  # "(none)"
                self.app.state.selected_lessons = []
                return
            p = label_to_path.get(chosen)
            if p is None:
                return
            # Toggle: if already selected, deselect; otherwise add.
            if p in self.app.state.selected_lessons:
                self.app.state.selected_lessons.remove(p)
            else:
                self.app.state.selected_lessons.append(p)

        self._dropdown = Dropdown(
            title=t("room_buttons.lessons", self.app.state.locale),
            options=options,
            selected_idx=idx,
            on_confirm=_on_pick,
            option_descriptions=descriptions,
            locale=self.app.state.locale,
        )

    async def _kick_player(self) -> None:
        if self.app.client is None:
            return
        try:
            r = await self.app.client.call("kick_player")
        except Exception as e:
            self.app.state.error_message = f"kick failed: {e}"
            return
        if not r.get("ok"):
            self.app.state.error_message = (r.get("error") or {}).get(
                "message", "kick rejected"
            )
            return
        self.app.state.error_message = ""
        await self._refresh_state()

    def _open_leave_confirm(self) -> None:
        async def _on_confirm(yes: bool) -> None:
            if yes:
                self._pending_transition = await self._leave()

        self._confirm = ConfirmModal(
            prompt=t("room_buttons.leave_confirm", self.app.state.locale),
            on_confirm=_on_confirm,
            locale=self.app.state.locale,
        )

    def _open_quit_confirm(self) -> None:
        async def _on_confirm(yes: bool) -> None:
            if yes:
                self.app.exit()

        self._confirm = ConfirmModal(
            prompt=t("room_buttons.quit_confirm", self.app.state.locale),
            on_confirm=_on_confirm,
            locale=self.app.state.locale,
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
            locale=self.app.state.locale,
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
            title=t("room_buttons.change_fog_title", self.app.state.locale),
            options=list(_FOG_OPTIONS),
            selected_idx=idx,
            on_confirm=lambda v: self._apply_config({"fog_of_war": v}),
            option_descriptions=_fog_descriptions(self.app.state.locale),
            locale=self.app.state.locale,
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
            title=t("room_buttons.change_teams_title", self.app.state.locale),
            options=list(_TEAM_MODE_OPTIONS),
            selected_idx=idx,
            on_confirm=lambda v: self._apply_config({"team_assignment": v}),
            option_descriptions=_team_mode_descriptions(self.app.state.locale),
            locale=self.app.state.locale,
        )

    def _open_host_team_modal(self) -> None:
        current = (self.app.state.last_room_state or {}).get("host_team", "blue")
        idx = (
            _HOST_TEAM_OPTIONS.index(current)
            if current in _HOST_TEAM_OPTIONS
            else 0
        )
        self._dropdown = Dropdown(
            title=t("room_buttons.change_host_title", self.app.state.locale),
            options=list(_HOST_TEAM_OPTIONS),
            selected_idx=idx,
            on_confirm=lambda v: self._apply_config({"host_team": v}),
            option_descriptions=_host_team_descriptions(self.app.state.locale),
            locale=self.app.state.locale,
        )

    def _open_turn_time_modal(self) -> None:
        current = str(
            (self.app.state.last_room_state or {}).get("turn_time_limit_s", 1800)
        )
        idx = (
            _TURN_TIME_OPTIONS.index(current)
            if current in _TURN_TIME_OPTIONS
            else _TURN_TIME_OPTIONS.index("1800")
        )

        async def _on_confirm(v: str) -> None:
            # update_room_config expects turn_time_limit_s as an int.
            await self._apply_config({"turn_time_limit_s": int(v)})

        self._dropdown = Dropdown(
            title=t("room_buttons.turn_time_title", self.app.state.locale),
            options=list(_TURN_TIME_OPTIONS),
            selected_idx=idx,
            on_confirm=_on_confirm,
            option_descriptions=_turn_time_descriptions(self.app.state.locale),
            locale=self.app.state.locale,
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
                from silicon_pantheon.client.locale.scenario import localize_scenario
                desc["scenario_slug"] = scenario_name
                try:
                    self.app.state.scenario_description = localize_scenario(
                        desc, self.app.state.locale
                    )
                except Exception:
                    log.exception(
                        "load_preview: localize_scenario crashed for %s locale=%s",
                        scenario_name, self.app.state.locale,
                    )
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
        cid = self.app.client.connection_id if self.app.client else "?"
        log.debug("refresh_state: calling get_room_state cid=%s", cid)
        try:
            r = await self.app.client.call("get_room_state")
        except Exception as e:
            log.exception(
                "refresh_state: get_room_state EXCEPTION cid=%s type=%s",
                cid, type(e).__name__,
            )
            self.app.state.error_message = f"get_room_state failed: {e}"
            return None
        if not r.get("ok"):
            err_msg = (r.get("error") or {}).get(
                "message", "get_room_state rejected"
            )
            log.warning(
                "refresh_state: get_room_state rejected cid=%s err=%s",
                cid, err_msg,
            )
            # If we got kicked or the room vanished, go back to lobby.
            if "not_in_room" in err_msg or "not seated" in err_msg or "in_lobby" in err_msg:
                log.info("refresh_state: kicked or room gone — returning to lobby")
                self.app.state.room_id = None
                self.app.state.slot = None
                from silicon_pantheon.client.tui.screens.lobby import LobbyScreen
                return LobbyScreen(self.app)
            self.app.state.error_message = err_msg
            return None
        self.app.state.error_message = ""
        room = r.get("room", {})
        status = room.get("status")
        prior_scenario = (self.app.state.last_room_state or {}).get("scenario")
        self.app.state.last_room_state = room
        log.debug(
            "refresh_state: status=%s seats=%s",
            status,
            {k: {"ready": v.get("ready"), "occupied": v.get("occupied")}
             for k, v in (room.get("seats") or {}).items()},
        )
        if status == "in_game":
            from silicon_pantheon.client.tui.screens.game import GameScreen

            log.info("refresh_state: transitioning to GameScreen")
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
            self.app.state.error_message = (r.get("error") or {}).get(
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
        log.info(
            "toggle_ready: currently=%s → requesting=%s cid=%s slot=%s status=%s",
            currently_ready, not currently_ready,
            self.app.client.connection_id if self.app.client else "?",
            slot, rs.get("status"),
        )
        try:
            r = await self.app.client.call(
                "set_ready", ready=not currently_ready
            )
        except Exception as e:
            log.exception(
                "toggle_ready: set_ready EXCEPTION cid=%s",
                self.app.client.connection_id if self.app.client else "?",
            )
            self.app.state.error_message = f"set_ready failed: {e}"
            return
        log.info("toggle_ready: set_ready ok=%s", r.get("ok"))
        if not r.get("ok"):
            self.app.state.error_message = (r.get("error") or {}).get(
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
