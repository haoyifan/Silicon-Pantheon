"""Lobby screen — rooms list, host / join / refresh / quit.

Minimal stub for the 1d.2 login commit; full logic lands in 1d.3.
"""

from __future__ import annotations

from rich.align import Align
from rich.console import RenderableType
from rich.panel import Panel
from rich.text import Text

from clash_of_robots.client.tui.app import Screen, TUIApp


class LobbyScreen(Screen):
    def __init__(self, app: TUIApp):
        self.app = app

    def render(self) -> RenderableType:
        body = Text("Lobby (full implementation in 1d.3)", style="dim italic")
        footer = Text(
            f"\nSigned in as {self.app.state.display_name} ({self.app.state.kind}). Press q to quit.",
            style="dim",
        )
        return Align.center(
            Panel(Text.assemble(body, footer), title="lobby", border_style="green"),
            vertical="middle",
        )

    async def handle_key(self, key: str) -> Screen | None:
        if key == "q":
            self.app.exit()
        return None
