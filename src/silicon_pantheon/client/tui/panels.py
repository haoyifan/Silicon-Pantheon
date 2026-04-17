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
    from silicon_pantheon.client.tui.app import Screen, TUIApp


FOCUSED_BORDER_STYLE = "bright_yellow"
IDLE_BORDER_STYLE = "bright_black"


def estimate_panel_height(console_height: int, ratio: float, border_lines: int = 2) -> int:
    """Estimate visible rows for a panel that occupies `ratio` of the terminal."""
    return max(1, int((console_height - 2) * ratio) - border_lines)


def border_style(focused: bool) -> str:
    return FOCUSED_BORDER_STYLE if focused else IDLE_BORDER_STYLE


def apply_vim_scroll(
    key: str,
    *,
    current: int,
    step_forward: int = 1,
    step_backward: int = 1,
    page_size: int = 12,
    bottom: int = 10**9,
    gg_state: list[bool] | None = None,
) -> int | None:
    """Map a key to an updated scroll offset for a read-only panel.

    Returns the new offset when the key was a scroll key (caller
    should assign it back), or None when the key wasn't recognized
    — caller should fall through to its own handling.

    Scroll offset semantics are panel-dependent: most panels treat
    0 as "top of content" and grow forward as you read further
    down. ReasoningPanel inverts (0 = tail / newest, growing means
    "back in time"), so it should pass step_backward+step_forward
    swapped — or just reinterpret up/down to suit its semantics.

    Shortcuts covered (only relevant when the panel has focus and
    the panel takes no text input):
      - j / ↓ / down         → step_forward
      - k / ↑ / up           → step_backward
      - ctrl-d               → half page forward
      - ctrl-u               → half page backward
      - ctrl-f / pgdown      → full page forward
      - ctrl-b / pgup        → full page backward
      - shift-g / end        → bottom
      - home                 → top
      - g (when `gg_state` is a primed [True]) → top
        `gg_state` is a single-element list used as a one-shot
        latch: on first 'g' the caller should set it to [True] and
        call us again; on the second 'g' we jump to top and reset
        it. Pass None to disable gg; in that case single 'g' is
        ignored.

    `bottom` caps the maximum offset — readers don't need to pass
    the real max if they'd prefer to clamp themselves.
    """
    # Clear the gg latch eagerly for ANY non-g key. The recognized
    # scroll keys below all return early so we'd never reach a tail
    # check; do it up front so the latch behaves like vim's.
    if key != "g" and gg_state is not None and gg_state[0]:
        gg_state[0] = False
    half = max(1, page_size // 2)
    if key in ("down", "j"):
        return min(bottom, current + step_forward)
    if key in ("up", "k"):
        return max(0, current - step_backward)
    if key == "ctrl-d":
        return min(bottom, current + half)
    if key == "ctrl-u":
        return max(0, current - half)
    if key in ("ctrl-f", "pgdown"):
        return min(bottom, current + page_size)
    if key in ("ctrl-b", "pgup"):
        return max(0, current - page_size)
    if key in ("shift-g", "end"):
        return bottom
    if key == "home":
        return 0
    if key == "g":
        if gg_state is None:
            return None
        if gg_state[0]:
            # Second 'g': reset latch and jump to top.
            gg_state[0] = False
            return 0
        # First 'g': latch and stay put.
        gg_state[0] = True
        return current
    # Any non-g key clears the gg latch — standard vim behaviour.
    if gg_state is not None and gg_state[0]:
        gg_state[0] = False
    return None


def wrap_rows_to_width(rows: list, inner_width: int) -> list:
    """Flatten scroll rows so each entry is one visible display line.

    Panels with per-line scroll offsets were buggy because a row like
    `Text(long_paragraph)` is ONE entry in the rows list but renders
    into many wrapped display rows; scrolling by 1 jumped the whole
    block at once.

    Pass the rendered `rows` list through this helper before applying
    the scroll offset. Text rows are split on explicit '\\n' and
    textwrap-wrapped to `inner_width`; non-Text renderables pass
    through as-is (scrolling one Table or Group is still atomic —
    convert those to Text rows in the panel if you need finer
    control).

    `inner_width` should approximate the panel's inner content width
    (console width × panel layout fraction, minus border + padding).
    Too narrow wraps more aggressively; too wide under-counts rows.
    """
    import textwrap

    from rich.text import Text

    out: list = []
    for row in rows:
        if not isinstance(row, Text):
            out.append(row)
            continue
        plain = row.plain
        style = row.style or None
        if not plain:
            out.append(Text("", style=style))
            continue
        for logical_line in plain.split("\n"):
            if not logical_line.strip():
                out.append(Text("", style=style))
                continue
            wrapped = textwrap.wrap(
                logical_line,
                width=inner_width,
                break_long_words=True,
                break_on_hyphens=False,
                drop_whitespace=False,
            ) or [logical_line]
            for w in wrapped:
                out.append(Text(w, style=style))
    return out


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
