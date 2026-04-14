"""Panel framework: a screen is a focused list of panels.

Each panel renders into one slot of a Rich Layout and decides what to
do with the keys that arrive while it's focused. The screen owns the
focus state and routes keys: Tab / Shift-Tab cycle focus across
focusable panels; everything else goes to the focused panel.

A focused panel paints its border in `FOCUSED_BORDER_STYLE`; other
panels use `IDLE_BORDER_STYLE`. Panels that can't take focus
(static-info ones) say so by overriding `can_focus()`.

Why a base class instead of duck typing: the screen uses .title and
.can_focus() at every render and key dispatch, and the explicit base
catches typos at import time instead of at first keystroke.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from rich.console import RenderableType

if TYPE_CHECKING:
    from clash_of_odin.client.tui.app import Screen, TUIApp


FOCUSED_BORDER_STYLE = "bright_yellow"
IDLE_BORDER_STYLE = "bright_black"


def border_style(focused: bool) -> str:
    return FOCUSED_BORDER_STYLE if focused else IDLE_BORDER_STYLE


class Panel(ABC):
    """One framed region of a screen."""

    title: str = ""

    def can_focus(self) -> bool:
        """If False, Tab skips this panel and arrows never reach it."""
        return True

    def key_hints(self) -> str:
        """Short inline string shown in the footer when this panel is
        focused. Describes what the focused-panel keystrokes do."""
        return ""

    @abstractmethod
    def render(self, focused: bool) -> RenderableType:
        """Return the renderable that fills this panel's region."""

    async def handle_key(self, key: str) -> "Screen | None":
        """Handle a keystroke while this panel is focused. Return a
        Screen to transition to, else None."""
        return None

    async def on_app_tick(self, app: "TUIApp") -> None:
        """Hook for periodic refresh work (e.g. scrolling animations).
        Default no-op."""
        return None
