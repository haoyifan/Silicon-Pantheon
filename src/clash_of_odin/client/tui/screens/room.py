"""Room screen — button-driven UI with dropdown modals.

Layout
------

  header ......................................  scenario / fog / teams
  seats table .................................  who's occupying each slot
  status banner ...............................  countdown / waiting / starting
  scenario preview panel ......................  mini ascii board
  actions panel ...............................  highlighted list of buttons

Navigation
----------

The action panel shows one button per row with a ➤ marker on the
currently focused one. Tab / ↓ / j moves down; Shift-Tab / ↑ / k moves
up; Enter activates the focused button; Esc does nothing when no
modal is open.

Buttons that need a choice (Scenario, Fog, Teams, Host Team) open a
full-screen dropdown modal when activated. The modal is navigated the
same way — arrows or j/k, Enter confirms, Esc cancels. Host-only
buttons are hidden for non-host players (slot B).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from rich.align import Align
from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from clash_of_odin.client.tui.app import POLL_INTERVAL_S, Screen, TUIApp

log = logging.getLogger("clash.tui.room")


def _unit_cell_style(u: dict[str, Any]) -> tuple[str, str]:
    """Pick the (glyph, Rich style) for a unit cell in any board view.

    Honors scenario-provided `glyph` / `color` from the unit_classes
    block. Falls back to the first letter of the class name (uppercase
    for blue, lowercase for red) so legacy scenarios and any class
    that omits glyph/color still render something visible.
    """
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


@dataclass
class Button:
    label: str
    action: str  # identifier dispatched to _run_action
    enabled: bool = True
    # For value-display buttons (Scenario / Fog / Teams / Host Team),
    # `value` is displayed to the right of the label.
    value: str | None = None


@dataclass
class Dropdown:
    """Modal single-select list. Enter confirms, Esc cancels."""

    title: str
    options: list[str]
    selected_idx: int
    on_confirm: Callable[[str], Awaitable[None]]

    def render(self) -> RenderableType:
        lines: list[Text] = []
        for i, opt in enumerate(self.options):
            marker = "➤ " if i == self.selected_idx else "  "
            style = "bold cyan" if i == self.selected_idx else "white"
            lines.append(Text(f"{marker}{opt}", style=style))
        footer = Text(
            "\n↑/k up   ↓/j down   Enter select   Esc cancel",
            style="dim",
        )
        body = Group(*(lines + [footer]))
        return Align.center(
            Panel(body, title=self.title, border_style="cyan", padding=(1, 3)),
            vertical="middle",
        )

    async def handle_key(self, key: str) -> bool:
        """Return True if the modal should close after this keypress."""
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


_FOG_OPTIONS = ("none", "classic", "line_of_sight")
_TEAM_MODE_OPTIONS = ("fixed", "random")
_HOST_TEAM_OPTIONS = ("blue", "red")


class RoomScreen(Screen):
    def __init__(self, app: TUIApp):
        self.app = app
        self._last_poll = 0.0
        self._scenario_preview: dict[str, Any] | None = None
        # Button navigation state.
        self._focus = 0
        self._modal: Dropdown | None = None
        # Cache scenarios list; fetched once on entry.
        self._scenarios: list[str] = []

    async def on_enter(self, app: TUIApp) -> None:
        await self._load_preview()
        await self._refresh_state()
        await self._load_scenarios()

    # ---- render ----

    def render(self) -> RenderableType:
        # Modal overlays the whole screen when active.
        if self._modal is not None:
            return self._modal.render()
        return self._render_main()

    def _render_main(self) -> RenderableType:
        rs = self.app.state.last_room_state or {}
        room_id = rs.get("room_id") or self.app.state.room_id or "?"

        header = Text(
            f"Room {room_id[:10]} — {rs.get('scenario', '?')}   "
            f"fog={rs.get('fog_of_war', '?')}   teams={rs.get('team_assignment', '?')}",
            style="bold cyan",
        )

        seats = rs.get("seats", {})
        seat_table = Table(expand=False, show_header=True, header_style="bold")
        seat_table.add_column("Slot")
        seat_table.add_column("Player")
        seat_table.add_column("Ready")
        for slot_id in ("a", "b"):
            seat = seats.get(slot_id, {})
            player = seat.get("player") or {}
            seat_table.add_row(
                slot_id,
                player.get("display_name", "(empty)"),
                "✓" if seat.get("ready") else "…",
            )

        banner = Text("")
        countdown = rs.get("autostart_in_s")
        status = rs.get("status", "?")
        if countdown is not None:
            banner = Text(
                f"Match auto-starts in {countdown:.1f}s …", style="yellow bold"
            )
        elif status == "waiting_for_players":
            banner = Text("waiting for opponent…", style="dim")
        elif status == "waiting_ready":
            banner = Text(
                "both players present — select Toggle Ready and press Enter",
                style="cyan",
            )
        elif status == "in_game":
            banner = Text("starting match…", style="green bold")

        preview = (
            self._render_preview()
            if self._scenario_preview
            else Text("(loading preview…)", style="dim italic")
        )

        actions_panel = self._render_actions(rs)
        error_line = Text("")
        if self.app.state.error_message:
            error_line.append(self.app.state.error_message, style="red")

        body = Group(
            header,
            Text(""),
            seat_table,
            Text(""),
            banner,
            Text(""),
            Panel(preview, title="scenario preview", border_style="dim"),
            Text(""),
            actions_panel,
            error_line,
        )
        return Align.center(
            Panel(body, title="room", border_style="yellow"), vertical="top"
        )

    def _render_actions(self, rs: dict[str, Any]) -> RenderableType:
        """Vertical button list with a focus marker on the highlighted row."""
        buttons = self._build_buttons(rs)
        lines: list[Text] = []
        for i, btn in enumerate(buttons):
            is_focused = i == self._focus
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
        lines.append(
            Text(
                "\n↑/k up   ↓/j (or Tab) down   Enter activate   q quit",
                style="dim",
            )
        )
        return Panel(
            Group(*lines), title="actions", border_style="bright_black"
        )

    def _build_buttons(self, rs: dict[str, Any]) -> list[Button]:
        """Return the buttons visible in the current state.

        Host-only controls are hidden for slot B. Buttons whose action
        doesn't apply in the current state are kept but disabled so
        the layout doesn't shift around as readiness toggles.
        """
        is_host = self.app.state.slot == "a"
        editable = rs.get("status") in (
            "waiting_for_players",
            "waiting_ready",
        )
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
                        enabled=editable and bool(self._scenarios),
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

    def _render_preview(self) -> RenderableType:
        p = self._scenario_preview or {}
        w = int(p.get("width", 0))
        h = int(p.get("height", 0))
        units = p.get("units", [])
        forts = p.get("forts", [])
        # cell -> (glyph, style). Forts pre-painted; units overwrite.
        styled: dict[tuple[int, int], tuple[str, str]] = {}
        for f in forts:
            pos = f.get("pos") or {}
            x, y = int(pos.get("x", -1)), int(pos.get("y", -1))
            if 0 <= x < w and 0 <= y < h:
                styled[(x, y)] = ("*", "yellow")
        for u in units:
            pos = u.get("pos") or {}
            x, y = int(pos.get("x", -1)), int(pos.get("y", -1))
            if 0 <= x < w and 0 <= y < h:
                glyph, style = _unit_cell_style(u)
                styled[(x, y)] = (glyph, style)
        text = Text()
        text.append("   " + " ".join(f"{x:>2}" for x in range(w)) + "\n", style="dim")
        for y in range(h):
            text.append(f"{y:>2} ", style="dim")
            for x in range(w):
                cell = styled.get((x, y))
                if cell is None:
                    text.append(" .", style="dim")
                else:
                    g, st = cell
                    text.append(f" {g}", style=st)
                text.append(" ")
            text.append("\n")
        text.append(f"\n{w}x{h} board · {len(units)} units · {len(forts)} forts")
        return text

    # ---- input ----

    async def handle_key(self, key: str) -> Screen | None:
        if self._modal is not None:
            close = await self._modal.handle_key(key)
            if close:
                self._modal = None
                await self._refresh_state()
            return None

        if key == "q":
            self.app.exit()
            return None
        buttons = self._build_buttons(self.app.state.last_room_state or {})
        if not buttons:
            return None
        if key in ("down", "j") or key == "\t":
            self._focus = (self._focus + 1) % len(buttons)
            return None
        if key in ("up", "k"):
            self._focus = (self._focus - 1) % len(buttons)
            return None
        if key == "enter":
            btn = buttons[self._focus]
            if not btn.enabled:
                return None
            return await self._run_action(btn.action)
        return None

    async def _run_action(self, action: str) -> Screen | None:
        if action == "toggle_ready":
            await self._toggle_ready()
            return None
        if action == "leave":
            return await self._leave()
        if action == "quit":
            self.app.exit()
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

    def _open_scenario_modal(self) -> None:
        current = (self.app.state.last_room_state or {}).get("scenario")
        options = list(self._scenarios) or [current or "01_tiny_skirmish"]
        idx = options.index(current) if current in options else 0
        self._modal = Dropdown(
            title="Change Scenario",
            options=options,
            selected_idx=idx,
            on_confirm=lambda v: self._apply_config({"scenario": v}),
        )

    def _open_fog_modal(self) -> None:
        current = (self.app.state.last_room_state or {}).get("fog_of_war", "none")
        idx = _FOG_OPTIONS.index(current) if current in _FOG_OPTIONS else 0
        self._modal = Dropdown(
            title="Change Fog of War",
            options=list(_FOG_OPTIONS),
            selected_idx=idx,
            on_confirm=lambda v: self._apply_config({"fog_of_war": v}),
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
        self._modal = Dropdown(
            title="Change Team Assignment",
            options=list(_TEAM_MODE_OPTIONS),
            selected_idx=idx,
            on_confirm=lambda v: self._apply_config({"team_assignment": v}),
        )

    def _open_host_team_modal(self) -> None:
        current = (self.app.state.last_room_state or {}).get("host_team", "blue")
        idx = (
            _HOST_TEAM_OPTIONS.index(current)
            if current in _HOST_TEAM_OPTIONS
            else 0
        )
        self._modal = Dropdown(
            title="Change Host Team",
            options=list(_HOST_TEAM_OPTIONS),
            selected_idx=idx,
            on_confirm=lambda v: self._apply_config({"host_team": v}),
        )

    # ---- server interactions ----

    async def tick(self) -> None:
        import time as _time

        now = _time.time()
        if now - self._last_poll >= POLL_INTERVAL_S:
            await self._refresh_state()

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
        if r.get("ok"):
            self._scenario_preview = (r.get("room") or {}).get("scenario_preview", {})
        # Also cache the full scenario bundle so the game screen can
        # render unit/terrain legends without a second roundtrip.
        scenario_name = (
            (self.app.state.last_room_state or {}).get("scenario")
            if self.app.state.last_room_state
            else None
        )
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
            self._scenarios = r.get("scenarios", [])

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
        log.info(
            "poll room=%s status=%s autostart_in_s=%s seats=%s",
            room.get("room_id"),
            room.get("status"),
            room.get("autostart_in_s"),
            {k: v.get("ready") for k, v in (room.get("seats") or {}).items()},
        )
        # Clamp focus so it stays on a valid button.
        button_count = len(self._build_buttons(room))
        if self._focus >= button_count:
            self._focus = max(0, button_count - 1)
        if room.get("status") == "in_game":
            log.info("room status is in_game; transitioning to GameScreen")
            from clash_of_odin.client.tui.screens.game import GameScreen

            next_screen = GameScreen(self.app)
            log.info("calling app.transition(GameScreen)")
            await self.app.transition(next_screen)
            log.info("app.transition(GameScreen) returned")
            return next_screen
        # If the host changed the scenario since our last refresh,
        # reload the mini-map preview so the ASCII board matches.
        if prior_scenario and room.get("scenario") != prior_scenario:
            await self._load_preview()
        return None

    async def _apply_config(self, fields: dict[str, Any]) -> None:
        """Send update_room_config with the given fields and refresh."""
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
        my_slot = self.app.state.slot
        current = (rs.get("seats") or {}).get(my_slot, {}).get("ready", False)
        log.info("toggle_ready: sending ready=%s", not current)
        try:
            r = await self.app.client.call("set_ready", ready=(not current))
        except Exception as e:
            log.exception("set_ready raised")
            self.app.state.error_message = f"set_ready failed: {e}"
            return
        log.info("toggle_ready: response=%s", r)
        if not r.get("ok"):
            self.app.state.error_message = r.get("error", {}).get(
                "message", "set_ready rejected"
            )
        else:
            self.app.state.error_message = ""
        await self._refresh_state()

    async def _leave(self) -> Screen | None:
        if self.app.client is None:
            return None
        try:
            await self.app.client.call("leave_room")
        except Exception:
            pass
        self.app.state.room_id = None
        self.app.state.slot = None
        from clash_of_odin.client.tui.screens.lobby import LobbyScreen

        return LobbyScreen(self.app)
