"""Lobby screen — list rooms, create / join / preview / refresh / quit.

Polls `list_rooms` every POLL_INTERVAL_S via the ticker. `n` creates
a new room with sensible defaults (a lightweight CreateRoomScreen
with full config UI can come later); `enter` joins the highlighted
row; `p` shows the preview; `r` refreshes immediately.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from rich.align import Align
from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from silicon_pantheon.client.locale import t
from silicon_pantheon.client.tui.app import POLL_INTERVAL_S, Screen, TUIApp


class LobbyScreen(Screen):
    def __init__(self, app: TUIApp):
        self.app = app
        self._selected = 0
        self._last_poll = 0.0
        self._tutorial = None  # TutorialOverlay | None
        self._confirm = None  # ConfirmModal | None

    async def on_enter(self, app: TUIApp) -> None:
        # Immediate refresh on entry so the table is populated before the
        # first tick.
        await self._refresh_rooms()
        await self._refresh_leaderboard()
        # Scenario cache is populated at login via get_scenario_bundle.
        # Tutorial: show on first visit or when replay is requested.
        self._maybe_start_tutorial()

    def render(self) -> RenderableType:
        lc = self.app.state.locale
        rooms = self.app.state.last_rooms

        header = Text(f"{t('lobby_title', lc)} — {self.app.state.display_name}", style="bold yellow")
        subtitle = Text(f"{len(rooms)} {t('lobby_table.room', lc)} {t('lobby_table.open', lc)}", style="dim")

        table = Table(expand=True, show_lines=False, header_style="bold")
        table.add_column(" ", width=2)
        table.add_column(t("lobby_table.room_id", lc), width=8, no_wrap=True)
        table.add_column(t("lobby_table.host", lc), overflow="fold")
        table.add_column(t("lobby_table.joiner", lc), overflow="fold")
        table.add_column(t("lobby_table.scenario", lc), overflow="fold")
        table.add_column(t("lobby_table.col_status", lc))

        if not rooms:
            table.add_row("", "", t("lobby_table.no_rooms", lc), "", "", "")
        else:
            for i, r in enumerate(rooms):
                marker = "➤" if i == self._selected else " "
                seats = r.get("seats", {})
                # Room ID: show short prefix for easy identification
                room_id = r.get("room_id", "")
                room_id_short = room_id[:8] if room_id else "—"
                # Scenario: prefer localized name from cache
                scenario_raw = r.get("scenario", "")
                cached = self.app.state.scenario_cache.get(scenario_raw)
                if cached:
                    scenario_display = cached.get("name") or scenario_raw
                else:
                    scenario_display = scenario_raw.replace("_", " ").lstrip("0123456789_")
                # Host and joiner as separate columns
                host_name = r.get("host_name", "")
                joiner_player = seats.get("b", {}).get("player") or {}
                joiner_name = joiner_player.get("display_name", "") if joiner_player else ""
                # Status with color coding
                status_raw = r.get("status", "unknown")
                status_text = t(f"lobby_val.status_{status_raw}", lc)
                _status_colors = {
                    "waiting_for_players": "green",
                    "waiting_ready": "yellow",
                    "counting_down": "yellow",
                    "in_game": "red",
                    "finished": "dim",
                }
                status_style = _status_colors.get(status_raw, "white")
                status_display = Text(status_text, style=status_style)
                table.add_row(
                    marker,
                    room_id_short,
                    host_name,
                    joiner_name or "—",
                    scenario_display,
                    status_display,
                    style="bold" if i == self._selected else None,
                )

        keys = Text(t("lobby_table.footer", lc), style="dim")

        status = Text("")
        if self.app.state.error_message:
            status.append(self.app.state.error_message, style="red")
        elif self.app.state.status_message:
            status.append(self.app.state.status_message, style="green")

        body = Group(header, subtitle, Text(""), table, Text(""), keys, status)
        room_panel = Panel(body, border_style="green", title=t("lobby_title", lc))
        lb_panel = self._render_leaderboard(lc)

        from rich.layout import Layout

        main = Layout()
        main.split_row(
            Layout(name="rooms", ratio=2),
            Layout(name="leaderboard", ratio=1),
        )
        main["rooms"].update(room_panel)
        main["leaderboard"].update(lb_panel)

        if self._confirm is not None:
            return self._confirm.render()
        if self._tutorial is not None and not self._tutorial.is_done:
            root = Layout()
            root.split_column(
                Layout(name="bg", ratio=1),
                Layout(name="tutorial", size=14),
            )
            root["bg"].update(main)
            root["tutorial"].update(self._tutorial.render())
            return root
        return main

    async def tick(self) -> None:
        import time

        now = time.time()
        if now - self._last_poll >= POLL_INTERVAL_S:
            await self._refresh_rooms()
            await self._refresh_leaderboard()

    async def handle_key(self, key: str) -> Screen | None:
        # Confirmation modal wins over everything.
        if self._confirm is not None:
            close = await self._confirm.handle_key(key)
            if close:
                self._confirm = None
            return None

        # Tutorial overlay intercepts all keys while active.
        if self._tutorial is not None and not self._tutorial.is_done:
            self._tutorial.handle_key(key)
            if self._tutorial.is_done:
                self._tutorial = None
            return None

        rooms = self.app.state.last_rooms
        if key == "q":
            from silicon_pantheon.client.tui.widgets import ConfirmModal
            async def _quit(yes: bool) -> None:
                if yes:
                    self.app.exit()
            self._confirm = ConfirmModal(
                prompt=t("lobby_quit.confirm", self.app.state.locale),
                on_confirm=_quit,
                locale=self.app.state.locale,
            )
            return None
        if key in ("down", "j"):
            if rooms:
                self._selected = (self._selected + 1) % len(rooms)
            return None
        if key in ("up", "k"):
            if rooms:
                self._selected = (self._selected - 1) % len(rooms)
            return None
        if key == "r":
            self.app.state.status_message = t("lobby_actions.refreshing", self.app.state.locale)
            await self._refresh_rooms()
            await self._refresh_leaderboard()
            self.app.state.status_message = ""
            return None
        if key == "n":
            return await self._create_room()
        if key == "enter":
            return await self._join_selected()
        if key == "p":
            return await self._preview_selected()
        if key == "t":
            self._start_tutorial()
            return None
        if key == "w":
            from silicon_pantheon.client.tui.screens.replay_picker import (
                ReplayPickerScreen,
            )
            return ReplayPickerScreen(self.app)
        return None

    # ---- actions ----

    async def _refresh_rooms(self) -> None:
        import time

        self._last_poll = time.time()
        if self.app.client is None:
            return
        try:
            r = await self.app.client.call("list_rooms")
        except Exception as e:
            self.app.state.error_message = f"list_rooms failed: {e}"
            return
        if not r.get("ok"):
            err_msg = (r.get("error") or {}).get("message", "list_rooms rejected")
            self.app.state.error_message = err_msg
            # If the server says "set_player_metadata first", the
            # connection's metadata was lost (heartbeat sweeper evicted
            # the session while the client sat on post-match). Auto-
            # recover by re-sending metadata and retrying.
            if "set_player_metadata" in err_msg:
                import logging as _logging
                _log = _logging.getLogger("silicon.tui.lobby")
                _log.warning(
                    "lobby rejected with 'set_player_metadata first' — "
                    "re-sending metadata to recover. cid=%s",
                    self.app.client.connection_id if self.app.client else "?",
                )
                try:
                    from silicon_pantheon.shared.protocol import PROTOCOL_VERSION
                    rr = await self.app.client.call(
                        "set_player_metadata",
                        display_name=self.app.state.display_name,
                        kind=self.app.state.kind,
                        provider=self.app.state.provider,
                        model=self.app.state.model,
                        client_protocol_version=PROTOCOL_VERSION,
                    )
                    if rr.get("ok"):
                        _log.info("metadata re-sent successfully — retrying list_rooms")
                        self.app.state.error_message = ""
                        # Retry the original call.
                        r2 = await self.app.client.call("list_rooms")
                        if r2.get("ok"):
                            self.app.state.last_rooms = r2.get("rooms", [])
                            if self._selected >= len(self.app.state.last_rooms):
                                self._selected = max(0, len(self.app.state.last_rooms) - 1)
                            return
                except Exception as e2:
                    _log.exception("metadata re-send failed: %s", e2)
            return
        self.app.state.error_message = ""
        self.app.state.last_rooms = r.get("rooms", [])
        if self._selected >= len(self.app.state.last_rooms):
            self._selected = max(0, len(self.app.state.last_rooms) - 1)

    def _ensure_tutorial_state(self) -> None:
        if self.app.state.tutorial_state is None:
            from silicon_pantheon.client.tui.tutorial import load_tutorial_state
            self.app.state.tutorial_state = load_tutorial_state()

    def _maybe_start_tutorial(self) -> None:
        self._ensure_tutorial_state()
        ts = self.app.state.tutorial_state
        if not ts.is_stage_done("lobby"):
            self._start_tutorial()

    def _start_tutorial(self) -> None:
        self._ensure_tutorial_state()
        from silicon_pantheon.client.tui.tutorial import (
            LOBBY_STEPS,
            TutorialOverlay,
        )
        ts = self.app.state.tutorial_state
        # Reset ALL stages so room and game tutorials also replay
        # when the user enters those screens next.
        ts.reset_all()

        def _on_done():
            ts.mark_done("lobby")

        self._tutorial = TutorialOverlay(
            steps=LOBBY_STEPS,
            stage="lobby",
            locale=self.app.state.locale,
            on_complete=_on_done,
        )

    async def _refresh_leaderboard(self) -> None:
        if self.app.client is None:
            return
        try:
            r = await self.app.client.call("get_leaderboard")
        except Exception:
            return
        if r.get("ok"):
            result = r.get("result") or r
            self.app.state.last_leaderboard = result.get("leaderboard", [])

    def _render_leaderboard(self, lc: str) -> RenderableType:
        lb = self.app.state.last_leaderboard

        tbl = Table(expand=True, show_lines=True, header_style="bold", padding=(0, 1))
        tbl.add_column(t("leaderboard.col_model", lc), overflow="fold", no_wrap=False)
        tbl.add_column(t("leaderboard.col_games", lc), justify="right")
        tbl.add_column(t("leaderboard.col_wins", lc), justify="right")
        tbl.add_column(t("leaderboard.col_win_pct", lc), justify="right")
        tbl.add_column(t("leaderboard.col_losses", lc), justify="right")
        tbl.add_column(t("leaderboard.col_draws", lc), justify="right")
        tbl.add_column(t("leaderboard.col_avg_think", lc), justify="right")

        if not lb:
            tbl.add_row(t("leaderboard.no_data", lc), "", "", "", "", "", "")
        else:
            for entry in lb:
                games = entry.get("games", 0)
                wins = entry.get("wins", 0)
                win_pct = f"{wins / games * 100:.0f}%" if games else "—"
                model = entry.get("model", "?")
                tbl.add_row(
                    model,
                    str(games),
                    str(wins),
                    win_pct,
                    str(entry.get("losses", 0)),
                    str(entry.get("draws", 0)),
                    f"{entry.get('avg_think_time_s', 0):.0f}s",
                )

        total = sum(e.get("games", 0) for e in lb) // 2 if lb else 0
        subtitle = Text(
            f"{total} {t('leaderboard.total_games', lc)}",
            style="dim",
        )
        body = Group(subtitle, Text(""), tbl)
        return Panel(body, border_style="cyan", title=t("leaderboard.title", lc))

    async def _create_room(self) -> Screen | None:
        if self.app.client is None:
            return None
        try:
            r = await self.app.client.call(
                "create_room",
                scenario="01_tiny_skirmish",
                team_assignment="fixed",
                host_team="blue",
                fog_of_war="none",
            )
        except Exception as e:
            self.app.state.error_message = f"create_room failed: {e}"
            return None
        if not r.get("ok"):
            self.app.state.error_message = (r.get("error") or {}).get("message", "create_room rejected")
            return None
        self.app.state.room_id = r.get("room_id")
        self.app.state.slot = r.get("slot")
        from silicon_pantheon.client.tui.screens.room import RoomScreen

        return RoomScreen(self.app)

    async def _join_selected(self) -> Screen | None:
        rooms = self.app.state.last_rooms
        if not rooms or self.app.client is None:
            return None
        room_id = rooms[self._selected].get("room_id")
        if not room_id:
            return None
        try:
            r = await self.app.client.call("join_room", room_id=room_id)
        except Exception as e:
            self.app.state.error_message = f"join_room failed: {e}"
            return None
        if not r.get("ok"):
            self.app.state.error_message = (r.get("error") or {}).get("message", "join_room rejected")
            return None
        self.app.state.room_id = room_id
        self.app.state.slot = r.get("slot")
        from silicon_pantheon.client.tui.screens.room import RoomScreen

        return RoomScreen(self.app)

    async def _preview_selected(self) -> Screen | None:
        rooms = self.app.state.last_rooms
        if not rooms or self.app.client is None:
            return None
        room = rooms[self._selected]
        room_id = room.get("room_id")
        if not room_id:
            return None
        scenario = room.get("scenario", "")
        # Fetch the full scenario description for the preview.
        desc: dict | None = None
        if scenario:
            try:
                r = await self.app.client.call("describe_scenario", name=scenario)
                if r.get("ok"):
                    from silicon_pantheon.client.locale.scenario import localize_scenario
                    r["scenario_slug"] = scenario
                    desc = localize_scenario(r, self.app.state.locale)
            except Exception:
                pass
        from silicon_pantheon.client.tui.screens.room_preview import RoomPreviewScreen
        return RoomPreviewScreen(self.app, room_data=room, scenario_desc=desc)
