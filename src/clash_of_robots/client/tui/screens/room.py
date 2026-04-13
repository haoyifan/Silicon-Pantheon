"""Room screen — preview the scenario, toggle ready, wait for auto-start.

Polls get_room_state every POLL_INTERVAL_S. When the server promotes
the room to IN_GAME (countdown complete, session created), the ticker
transitions to GameScreen.
"""

from __future__ import annotations

import logging
from typing import Any

from rich.align import Align
from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from clash_of_robots.client.tui.app import POLL_INTERVAL_S, Screen, TUIApp

log = logging.getLogger("clash.tui.room")


class RoomScreen(Screen):
    def __init__(self, app: TUIApp):
        self.app = app
        self._preview_loaded = False
        self._last_poll = 0.0
        self._scenario_preview: dict[str, Any] | None = None
        self._ready_local = False

    async def on_enter(self, app: TUIApp) -> None:
        await self._load_preview()
        await self._refresh_state()

    def render(self) -> RenderableType:
        rs = self.app.state.last_room_state or {}
        room_id = rs.get("room_id") or self.app.state.room_id or "?"
        status = rs.get("status", "?")

        header = Text(
            f"Room {room_id[:10]} — {rs.get('scenario', '?')}   "
            f"fog={rs.get('fog_of_war', '?')}   teams={rs.get('team_assignment', '?')}",
            style="bold cyan",
        )

        # Seats table.
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

        # Countdown / status banner.
        banner = Text("")
        countdown = rs.get("autostart_in_s")
        if countdown is not None:
            banner = Text(
                f"Match auto-starts in {countdown:.1f}s …", style="yellow bold"
            )
        elif status == "waiting_for_players":
            banner = Text("waiting for opponent…", style="dim")
        elif status == "waiting_ready":
            banner = Text("both players present — press r to ready", style="cyan")
        elif status == "in_game":
            banner = Text("starting match…", style="green bold")

        # Scenario preview box.
        preview = self._render_preview() if self._scenario_preview else Text(
            "(loading preview…)", style="dim italic"
        )

        keys = Text(
            "r toggle ready   l leave room   q quit",
            style="dim",
        )

        status_line = Text("")
        if self.app.state.error_message:
            status_line.append(self.app.state.error_message, style="red")

        body = Group(
            header,
            Text(""),
            seat_table,
            Text(""),
            banner,
            Text(""),
            Panel(preview, title="scenario preview", border_style="dim"),
            Text(""),
            keys,
            status_line,
        )
        return Align.center(
            Panel(body, title="room", border_style="yellow"), vertical="top"
        )

    def _render_preview(self) -> RenderableType:
        p = self._scenario_preview or {}
        w = int(p.get("width", 0))
        h = int(p.get("height", 0))
        units = p.get("units", [])
        forts = p.get("forts", [])
        # Build an ascii grid: '.'=plain, '*'=fort, letters for units.
        grid = [["." for _ in range(w)] for _ in range(h)]
        for f in forts:
            pos = f.get("pos") or {}
            x, y = int(pos.get("x", -1)), int(pos.get("y", -1))
            if 0 <= x < w and 0 <= y < h:
                grid[y][x] = "*"
        glyph_for = {"knight": "K", "archer": "A", "cavalry": "C", "mage": "M"}
        for u in units:
            pos = u.get("pos") or {}
            x, y = int(pos.get("x", -1)), int(pos.get("y", -1))
            if 0 <= x < w and 0 <= y < h:
                glyph = glyph_for.get(u.get("class", ""), "?")
                if u.get("owner") == "red":
                    glyph = glyph.lower()
                grid[y][x] = glyph
        text = Text()
        text.append("   " + " ".join(f"{x:>2}" for x in range(w)) + "\n", style="dim")
        for y in range(h):
            text.append(f"{y:>2} ", style="dim")
            for x in range(w):
                g = grid[y][x]
                if g.isupper() and g.isalpha():
                    text.append(f" {g}", style="bold cyan")
                elif g.islower() and g.isalpha():
                    text.append(f" {g}", style="bold red")
                elif g == "*":
                    text.append(f" *", style="yellow")
                else:
                    text.append(" .", style="dim")
                text.append(" ")
            text.append("\n")
        text.append(f"\n{w}x{h} board · {len(units)} units · {len(forts)} forts")
        return text

    async def tick(self) -> None:
        import time

        now = time.time()
        if now - self._last_poll >= POLL_INTERVAL_S:
            await self._refresh_state()

    async def handle_key(self, key: str) -> Screen | None:
        if key == "q":
            self.app.exit()
            return None
        if key == "l":
            return await self._leave()
        if key == "r":
            return await self._toggle_ready()
        return None

    # ---- actions ----

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

    async def _refresh_state(self) -> Screen | None:
        import time

        self._last_poll = time.time()
        if self.app.client is None:
            return None
        try:
            r = await self.app.client.call("get_room_state")
        except Exception as e:
            self.app.state.error_message = f"get_room_state failed: {e}"
            return None
        if not r.get("ok"):
            # A room that vanished is fatal for this screen.
            self.app.state.error_message = r.get("error", {}).get(
                "message", "get_room_state rejected"
            )
            log.warning("get_room_state rejected: %s", r)
            return None
        self.app.state.error_message = ""
        room = r.get("room", {})
        self.app.state.last_room_state = room
        log.info(
            "poll room=%s status=%s autostart_in_s=%s seats=%s",
            room.get("room_id"),
            room.get("status"),
            room.get("autostart_in_s"),
            {k: v.get("ready") for k, v in (room.get("seats") or {}).items()},
        )
        # Did the match just start? Transition to GameScreen.
        if room.get("status") == "in_game":
            from clash_of_robots.client.tui.screens.game import GameScreen

            next_screen = GameScreen(self.app)
            await self.app.transition(next_screen)
            return next_screen
        return None

    async def _toggle_ready(self) -> Screen | None:
        if self.app.client is None:
            return None
        self._ready_local = not self._ready_local
        log.info("toggle_ready: sending ready=%s", self._ready_local)
        try:
            r = await self.app.client.call("set_ready", ready=self._ready_local)
        except Exception as e:
            log.exception("set_ready raised")
            self.app.state.error_message = f"set_ready failed: {e}"
            return None
        log.info("toggle_ready: response=%s", r)
        if not r.get("ok"):
            self._ready_local = not self._ready_local  # revert on failure
            self.app.state.error_message = r.get("error", {}).get(
                "message", "set_ready rejected"
            )
        else:
            self.app.state.error_message = ""
        await self._refresh_state()
        return None

    async def _leave(self) -> Screen | None:
        if self.app.client is None:
            return None
        try:
            await self.app.client.call("leave_room")
        except Exception:
            pass
        self.app.state.room_id = None
        self.app.state.slot = None
        from clash_of_robots.client.tui.screens.lobby import LobbyScreen

        return LobbyScreen(self.app)
