"""Terminal UI: live-updating board + sidebar.

Uses rich.Live if stdout is a TTY; otherwise prints frames as plain text so
the renderer still works under `pytest -s` or piped output.
"""

from __future__ import annotations

import sys

from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel

from clash_of_robots.server.session import Session

from .board_view import render_board
from .sidebar import render_header, render_last_action, render_units_table


class TUIRenderer:
    def __init__(self, session: Session):
        self.session = session
        self.console = Console()
        self._live: Live | None = None
        self._tty = sys.stdout.isatty()

    def _frame(self):
        state = self.session.state
        return Group(
            render_header(state),
            Panel(render_board(state), title="Board", border_style="dim"),
            render_units_table(state),
            render_last_action(state),
        )

    def start(self) -> None:
        if self._tty:
            self._live = Live(
                self._frame(), console=self.console, refresh_per_second=10, screen=False
            )
            self._live.__enter__()
        else:
            self.console.print(self._frame())
            self.console.print("-" * 40)

    def refresh(self) -> None:
        if self._live is not None:
            self._live.update(self._frame())
        else:
            self.console.print(self._frame())
            self.console.print("-" * 40)

    def stop(self) -> None:
        if self._live is not None:
            self._live.__exit__(None, None, None)
            self._live = None
