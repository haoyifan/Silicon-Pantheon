"""Lobby screen — list rooms, create / join / preview / refresh / quit.

Polls `list_rooms` every POLL_INTERVAL_S via the ticker. `n` creates
a new room with sensible defaults (a lightweight CreateRoomScreen
with full config UI can come later); `enter` joins the highlighted
row; `p` shows the preview; `r` refreshes immediately.
"""

from __future__ import annotations

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

    async def on_enter(self, app: TUIApp) -> None:
        # Immediate refresh on entry so the table is populated before the
        # first tick.
        await self._refresh_rooms()

    def render(self) -> RenderableType:
        lc = self.app.state.locale
        rooms = self.app.state.last_rooms

        header = Text(f"{t('lobby_title', lc)} — {self.app.state.display_name} ({self.app.state.kind})", style="bold yellow")
        subtitle = Text(f"{len(rooms)} {t('lobby_table.room', lc)}(s) {t('lobby_table.open', lc)}", style="dim")

        table = Table(expand=True, show_lines=False, header_style="bold")
        table.add_column(" ", width=2)
        table.add_column(t("lobby_table.room", lc), overflow="fold")
        table.add_column(t("lobby_table.host", lc), overflow="fold")
        table.add_column(t("lobby_table.scenario", lc), overflow="fold")
        table.add_column(t("lobby_table.teams", lc))
        table.add_column(t("lobby_table.fog", lc))
        table.add_column(t("lobby_table.seats", lc))
        table.add_column(t("lobby_table.col_status", lc))

        if not rooms:
            table.add_row("", t("lobby_table.no_rooms", lc), "", "", "", "", "", "")
        else:
            for i, r in enumerate(rooms):
                marker = "➤" if i == self._selected else " "
                seats = r.get("seats", {})
                occ = sum(1 for s in seats.values() if s.get("occupied"))
                table.add_row(
                    marker,
                    r.get("room_id", "")[:10],
                    r.get("host_name", ""),
                    r.get("scenario", ""),
                    r.get("team_assignment", ""),
                    r.get("fog_of_war", ""),
                    f"{occ}/2",
                    r.get("status", ""),
                    style="bold" if i == self._selected else None,
                )

        keys = Text(t("lobby_table.footer", lc), style="dim")

        status = Text("")
        if self.app.state.error_message:
            status.append(self.app.state.error_message, style="red")
        elif self.app.state.status_message:
            status.append(self.app.state.status_message, style="green")

        body = Group(header, subtitle, Text(""), table, Text(""), keys, status)
        return Panel(Align.center(body, vertical="top"), border_style="green", title=t("lobby_title", lc))

    async def tick(self) -> None:
        import time

        now = time.time()
        if now - self._last_poll >= POLL_INTERVAL_S:
            await self._refresh_rooms()

    async def handle_key(self, key: str) -> Screen | None:
        rooms = self.app.state.last_rooms
        if key == "q":
            self.app.exit()
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
            self.app.state.status_message = ""
            return None
        if key == "n":
            return await self._create_room()
        if key == "enter":
            return await self._join_selected()
        if key == "p":
            return await self._preview_selected()
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
        room_id = rooms[self._selected].get("room_id")
        if not room_id:
            return None
        try:
            r = await self.app.client.call("preview_room", room_id=room_id)
        except Exception as e:
            self.app.state.error_message = f"preview_room failed: {e}"
            return None
        if not r.get("ok"):
            self.app.state.error_message = (r.get("error") or {}).get("message", "preview rejected")
            return None
        # Stash preview on state for a (future) preview screen; for now
        # render a concise banner on the lobby.
        room = r.get("room", {})
        preview = room.get("scenario_preview", {})
        self.app.state.status_message = (
            f"preview {room_id[:8]}: {preview.get('width','?')}x{preview.get('height','?')} "
            f"units={len(preview.get('units', []))}"
        )
        return None
