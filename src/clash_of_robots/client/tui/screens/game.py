"""Game screen stub — full implementation in 1d.5."""

from __future__ import annotations

from rich.align import Align
from rich.console import RenderableType
from rich.panel import Panel
from rich.text import Text

from clash_of_robots.client.tui.app import Screen, TUIApp


class GameScreen(Screen):
    def __init__(self, app: TUIApp):
        self.app = app

    def render(self) -> RenderableType:
        return Align.center(
            Panel(
                Text(
                    "In-game screen — full implementation in 1d.5\n\n"
                    "[q] quit",
                    style="dim italic",
                ),
                title="game",
                border_style="red",
            ),
            vertical="middle",
        )

    async def handle_key(self, key: str) -> Screen | None:
        if key == "q":
            self.app.exit()
        return None
