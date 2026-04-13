"""Room screen stub — full implementation in 1d.4."""

from __future__ import annotations

from rich.align import Align
from rich.console import RenderableType
from rich.panel import Panel
from rich.text import Text

from clash_of_robots.client.tui.app import Screen, TUIApp


class RoomScreen(Screen):
    def __init__(self, app: TUIApp):
        self.app = app

    def render(self) -> RenderableType:
        body = Text(
            f"Room {self.app.state.room_id} — slot {self.app.state.slot}\n"
            "(full implementation in 1d.4)",
            style="dim italic",
        )
        footer = Text("\n[q] quit", style="dim")
        return Align.center(
            Panel(Text.assemble(body, footer), title="room", border_style="yellow"),
            vertical="middle",
        )

    async def handle_key(self, key: str) -> Screen | None:
        if key == "q":
            self.app.exit()
        return None
