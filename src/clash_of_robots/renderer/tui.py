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
from .sidebar import (
    render_header,
    render_last_action,
    render_thoughts_panel,
    render_units_table,
)


class TUIRenderer:
    def __init__(self, session: Session, thoughts_height: int | None = None):
        self.session = session
        self.console = Console()
        self._live: Live | None = None
        self._tty = sys.stdout.isatty()
        self.thoughts_height = thoughts_height

    def _frame(self):
        state = self.session.state
        thoughts_kwargs = {}
        if self.thoughts_height is not None:
            thoughts_kwargs["height"] = self.thoughts_height
        return Group(
            render_header(state),
            Panel(render_board(state), title="Board", border_style="dim"),
            render_units_table(state),
            render_last_action(state),
            render_thoughts_panel(self.session, **thoughts_kwargs),
        )

    def start(self) -> None:
        if self._tty:
            # screen=True renders to the terminal's alternate screen buffer,
            # which eliminates the bottom-row flicker that the primary-buffer
            # cursor-move/clear sequence produces on tall frames. Downside:
            # when Live exits, the terminal restores the pre-match state and
            # the final board disappears — we handle that in stop() by
            # re-printing the last frame to the primary screen.
            self._live = Live(
                self._frame(), console=self.console, refresh_per_second=10, screen=True
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
            # Capture the final frame before exiting Live (which restores the
            # primary screen buffer and wipes everything we drew).
            final_frame = self._frame()
            self._live.__exit__(None, None, None)
            self._live = None
            self.console.print(final_frame)
