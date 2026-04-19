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

from rich import box
from rich.align import Align
from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from silicon_pantheon.client.locale import t
from silicon_pantheon.client.tui.app import POLL_INTERVAL_S, Screen, TUIApp

# Viewport sizes. The ranking board sits as a compact scoreboard
# widget at the top (centered, ~50% width, fixed-height box).
# Rooms panel gets full width + the remaining vertical space.
#
# LEADERBOARD_VISIBLE_ROWS must match what actually renders inside
# RANKING_BAND_HEIGHT, otherwise Rich silently truncates rows at
# the bottom and the cursor vanishes when it scrolls there. Budget:
#   band height = 2 (panel borders) + 1 (subtitle) + 1 (hint)
#                 + 2 (disclaimer, wraps) + table height
#   table height = 2*N + 3  (box top/bottom + header + separators
#                             between the N data rows)
#   → N = (RANKING_BAND_HEIGHT - 9) / 2
ROOMS_VISIBLE_ROWS = 12
RANKING_BAND_HEIGHT = 18
LEADERBOARD_VISIBLE_ROWS = 4


class LobbyScreen(Screen):
    def __init__(self, app: TUIApp):
        self.app = app
        self._selected = 0
        self._last_poll = 0.0
        self._tutorial = None  # TutorialOverlay | None
        self._confirm = None  # ConfirmModal | None

    # View state is stored on SharedState so model-details round-trips
    # preserve which panel the user was focused on + which row was
    # selected. Bare attribute access would re-init per LobbyScreen
    # construction, wiping the selection.
    @property
    def _active_view(self) -> str:
        return self.app.state.lobby_active_view

    @_active_view.setter
    def _active_view(self, value: str) -> None:
        self.app.state.lobby_active_view = value

    @property
    def _ranking_selected(self) -> int:
        return self.app.state.lobby_ranking_selected

    @_ranking_selected.setter
    def _ranking_selected(self, value: int) -> None:
        self.app.state.lobby_ranking_selected = value

    @property
    def _rooms_scroll(self) -> int:
        return self.app.state.lobby_rooms_scroll

    @_rooms_scroll.setter
    def _rooms_scroll(self, value: int) -> None:
        self.app.state.lobby_rooms_scroll = value

    @property
    def _ranking_scroll(self) -> int:
        return self.app.state.lobby_ranking_scroll

    @_ranking_scroll.setter
    def _ranking_scroll(self, value: int) -> None:
        self.app.state.lobby_ranking_scroll = value

    @staticmethod
    def _slice_for_viewport(
        items: list, selected: int, scroll: int, size: int
    ) -> tuple[list, int]:
        """Return the visible window and the (possibly adjusted) scroll.

        Keeps the selected row inside [scroll, scroll+size) — classic
        cursor-follow windowing. When the full list fits, scroll stays
        at 0 so short lists don't look like they're mid-scroll.
        """
        total = len(items)
        if total <= size:
            return items, 0
        if selected < scroll:
            scroll = selected
        elif selected >= scroll + size:
            scroll = selected - size + 1
        scroll = max(0, min(scroll, max(0, total - size)))
        return items[scroll:scroll + size], scroll

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
        view_bar = self._render_view_bar(lc)

        table = Table(expand=True, show_lines=False, header_style="bold")
        table.add_column(" ", width=2)
        table.add_column(t("lobby_table.room_id", lc), width=8, no_wrap=True)
        table.add_column(t("lobby_table.host", lc), overflow="fold")
        table.add_column(t("lobby_table.joiner", lc), overflow="fold")
        table.add_column(t("lobby_table.scenario", lc), overflow="ellipsis", max_width=36, no_wrap=True)
        table.add_column(t("lobby_table.col_status", lc))

        if not rooms:
            table.add_row("", "", t("lobby_table.no_rooms", lc), "", "", "")
        else:
            visible_rooms, self._rooms_scroll = self._slice_for_viewport(
                rooms, self._selected, self._rooms_scroll, ROOMS_VISIBLE_ROWS
            )
            for offset, r in enumerate(visible_rooms):
                i = offset + self._rooms_scroll
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

        # More-rows indicator so the user knows the list is windowed.
        rooms_more = ""
        if len(rooms) > ROOMS_VISIBLE_ROWS:
            shown = min(ROOMS_VISIBLE_ROWS, len(rooms) - self._rooms_scroll)
            rooms_more = f"[{self._rooms_scroll + 1}–{self._rooms_scroll + shown} of {len(rooms)}]"

        rooms_border = "green" if self._active_view == "rooms" else "dim"
        ranking_border = "cyan" if self._active_view == "ranking" else "dim"
        subtitle_line = Text(
            f"{len(rooms)} {t('lobby_table.room', lc)} {t('lobby_table.open', lc)}  {rooms_more}",
            style="dim",
        )
        body = Group(header, subtitle_line, view_bar, Text(""), table, Text(""), keys, status)
        room_panel = Panel(body, border_style=rooms_border, title=t("lobby_title", lc))
        lb_panel = self._render_leaderboard(lc, border_style=ranking_border)

        from rich.layout import Layout

        # Ranking sits as a compact centered scoreboard at the TOP
        # (fixed short band), rooms below gets full width and the
        # rest of the vertical space. The side-spacers recede to dim
        # so the scoreboard reads as a focal widget.
        main = Layout()
        main.split_column(
            Layout(name="ranking_band", size=RANKING_BAND_HEIGHT),
            Layout(name="rooms", ratio=1),
        )
        main["ranking_band"].split_row(
            Layout(name="lb_spacer_l", ratio=1),
            Layout(name="leaderboard", ratio=2),
            Layout(name="lb_spacer_r", ratio=1),
        )
        main["ranking_band"]["leaderboard"].update(lb_panel)
        main["ranking_band"]["lb_spacer_l"].update(Text(""))
        main["ranking_band"]["lb_spacer_r"].update(Text(""))
        main["rooms"].update(room_panel)

        if self._confirm is not None:
            return self._confirm.render()
        if self._tutorial is not None and not self._tutorial.is_done:
            # With rooms + leaderboard stacked vertically, a 14-row
            # tutorial stripe at the bottom would squeeze both panels.
            # Switch to the inline (non-centered) tutorial render and
            # shrink the stripe — render_inline fills its allotted
            # box efficiently instead of Align.center-ing inside it.
            root = Layout()
            root.split_column(
                Layout(name="bg", ratio=1),
                Layout(name="tutorial", size=10),
            )
            root["bg"].update(main)
            root["tutorial"].update(self._tutorial.render_inline())
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
        lb = self.app.state.last_leaderboard
        if key in ("\t", "tab"):
            self._active_view = "ranking" if self._active_view == "rooms" else "rooms"
            return None
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
            if self._active_view == "ranking":
                if lb:
                    self._ranking_selected = (self._ranking_selected + 1) % len(lb)
            elif rooms:
                self._selected = (self._selected + 1) % len(rooms)
            return None
        if key in ("up", "k"):
            if self._active_view == "ranking":
                if lb:
                    self._ranking_selected = (self._ranking_selected - 1) % len(lb)
            elif rooms:
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
            if self._active_view == "ranking":
                return await self._open_model_details()
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

    def _render_view_bar(self, lc: str) -> Text:
        """Header tabs showing which sub-view (rooms/ranking) is active."""
        rooms_label = t("lobby_views.rooms", lc)
        ranking_label = t("lobby_views.ranking", lc)
        hint = t("lobby_views.tab_hint", lc)
        bar = Text("")
        if self._active_view == "rooms":
            bar.append(f"[ {rooms_label} ]", style="bold green")
            bar.append(f"  {ranking_label}  ", style="dim")
        else:
            bar.append(f"  {rooms_label}  ", style="dim")
            bar.append(f"[ {ranking_label} ]", style="bold cyan")
        bar.append(f"   {hint}", style="dim italic")
        return bar

    def _render_leaderboard(self, lc: str, border_style: str = "cyan") -> RenderableType:
        lb = self.app.state.last_leaderboard
        # Keep the selected index in range if the table shrank between refreshes.
        if lb and self._ranking_selected >= len(lb):
            self._ranking_selected = len(lb) - 1

        # Compact "scoreboard" style: heavy double-edge box, rank column
        # with medals, trimmed stats. The full drill-down is one Enter
        # away, so this view only needs the headline numbers.
        tbl = Table(
            expand=True,
            show_lines=True,
            header_style="bold yellow",
            box=box.DOUBLE_EDGE,
            padding=(0, 1),
        )
        tbl.add_column("#", justify="right", width=3, no_wrap=True)
        tbl.add_column(
            t("leaderboard.col_model", lc),
            overflow="ellipsis",
            no_wrap=True,
        )
        tbl.add_column(t("leaderboard.col_games", lc), justify="right", width=5)
        tbl.add_column("W-L-D", justify="center", width=8, no_wrap=True)
        tbl.add_column(t("leaderboard.col_win_pct", lc), justify="right", width=5)

        focused = self._active_view == "ranking"
        medals = {0: "🥇", 1: "🥈", 2: "🥉"}

        if not lb:
            tbl.add_row("", t("leaderboard.no_data", lc), "", "", "")
        else:
            visible, self._ranking_scroll = self._slice_for_viewport(
                lb, self._ranking_selected, self._ranking_scroll, LEADERBOARD_VISIBLE_ROWS
            )
            for offset, entry in enumerate(visible):
                i = offset + self._ranking_scroll
                games = entry.get("games", 0)
                wins = entry.get("wins", 0)
                losses = entry.get("losses", 0)
                draws = entry.get("draws", 0)
                win_pct = f"{wins / games * 100:.0f}%" if games else "—"
                model = entry.get("model", "?")
                is_sel = focused and i == self._ranking_selected
                rank_cell = medals.get(i, f"#{i + 1}")
                tbl.add_row(
                    rank_cell,
                    ("➤ " if is_sel else "") + model,
                    str(games),
                    f"{wins}-{losses}-{draws}",
                    win_pct,
                    style="bold reverse" if is_sel else None,
                )

        total = sum(e.get("games", 0) for e in lb) // 2 if lb else 0
        lb_more = ""
        if lb and len(lb) > LEADERBOARD_VISIBLE_ROWS:
            shown = min(LEADERBOARD_VISIBLE_ROWS, len(lb) - self._ranking_scroll)
            lb_more = f"  [{self._ranking_scroll + 1}–{self._ranking_scroll + shown}/{len(lb)}]"
        subtitle = Text(
            f"{total} {t('leaderboard.total_games', lc)}{lb_more}",
            style="dim",
            justify="center",
        )
        hint_text = (
            t("leaderboard.hint_active", lc)
            if focused
            else t("leaderboard.hint_inactive", lc)
        )
        hint = Text(hint_text, style="dim italic", justify="center")
        disclaimer = Text(
            t("leaderboard.disclaimer", lc),
            style="dim italic",
            justify="center",
        )
        body = Group(tbl, subtitle, hint, disclaimer)
        return Panel(
            body,
            border_style=border_style,
            title=f"🏆 {t('leaderboard.title', lc)}",
            box=box.DOUBLE,
            padding=(0, 1),
        )

    async def _open_model_details(self) -> Screen | None:
        lb = self.app.state.last_leaderboard
        if not lb:
            return None
        idx = min(self._ranking_selected, len(lb) - 1)
        entry = lb[idx]
        model = entry.get("model") or ""
        provider = entry.get("provider") or ""
        if not model:
            return None
        from silicon_pantheon.client.tui.screens.model_details import ModelDetailsScreen
        return ModelDetailsScreen(self.app, model=model, provider=provider)

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
