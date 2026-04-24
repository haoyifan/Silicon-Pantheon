"""Shared TUI widgets — Dropdown, ConfirmModal, UnitCard.

These are reusable modal / card components used by both the room and
game screens.  Extracted from ``screens.room`` to avoid circular
imports and keep the room module focused on its own layout.
"""

from __future__ import annotations

import time as _time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from rich.align import Align
from rich.console import Group, RenderableType
from rich.panel import Panel as RichPanel
from rich.table import Table
from rich.text import Text

from silicon_pantheon.client.locale import t
from silicon_pantheon.client.tui.panels import border_style


@dataclass
class Dropdown:
    """Modal single-select list with an inline explanation box.

    When the caller supplies `option_descriptions`, the currently-
    highlighted option's description is rendered beneath the list so
    the player can read what 'classic' fog actually means before
    committing.

    If a description is longer than the visible area, PageUp/PageDown
    (or u/d) scrolls the description panel.
    """

    title: str
    options: list[str]
    selected_idx: int
    on_confirm: Callable[[str], Awaitable[None]]
    # Optional {option_value: markdown-free explanation}. Missing keys
    # render no description panel — the list stays minimal for truly
    # self-describing options (e.g. team colors).
    option_descriptions: dict[str, str] | None = None
    locale: str = "en"
    # Description scroll offset (lines from top).
    _desc_scroll: int = 0
    # Focus mode: "list" = j/k selects options, "desc" = j/k scrolls
    # description. Tab cycles between them when descriptions exist.
    _focus: str = "list"

    def render(self) -> RenderableType:
        lines: list[Text] = []
        for i, opt in enumerate(self.options):
            marker = "➤ " if i == self.selected_idx else "  "
            style = "bold yellow" if i == self.selected_idx else "white"
            lines.append(Text(f"{marker}{opt}", style=style))
        list_border = "bright_cyan" if self._focus == "list" else "dim"
        list_panel = RichPanel(
            Group(*lines), border_style=list_border, padding=(0, 1),
        )
        has_desc = bool(self.option_descriptions)
        footer_key = (
            "room_modal.dropdown_footer_tab" if has_desc
            else "room_modal.dropdown_footer"
        )
        footer = Text(
            t(footer_key, self.locale), style="dim"
        )
        body_parts: list[RenderableType] = [list_panel]
        desc = (self.option_descriptions or {}).get(
            self.options[self.selected_idx] if self.options else ""
        )
        if desc:
            # Show a scrollable window of the description. Split by
            # lines and apply the scroll offset.
            desc_lines = desc.split("\n")
            total_lines = len(desc_lines)
            max_visible = 12
            start = min(self._desc_scroll, max(0, total_lines - max_visible))
            end = min(start + max_visible, total_lines)
            visible = "\n".join(desc_lines[start:end])
            scroll_hint = ""
            if total_lines > max_visible:
                scroll_hint = f" [{start + 1}-{end}/{total_lines}]"
            desc_border = "bright_cyan" if self._focus == "desc" else "dim"
            body_parts.append(
                RichPanel(
                    Text(visible, style="white", no_wrap=False, overflow="fold"),
                    title=self.options[self.selected_idx] + scroll_hint,
                    border_style=desc_border,
                    padding=(0, 1),
                )
            )
        body_parts.append(Text(""))
        body_parts.append(footer)
        # The dropdown renders as a full-screen Layout so the panel
        # never grows past the terminal height. Rich clips overflow
        # inside the layout slot, and the description scroll handles
        # content that doesn't fit.
        from rich.layout import Layout

        root = Layout()
        root.split_column(
            Layout(name="pad_top", size=2),
            Layout(name="content"),
            Layout(name="pad_bot", size=1),
        )
        root["pad_top"].update(Text(""))
        root["pad_bot"].update(Text(""))
        inner = Align.center(
            RichPanel(
                Group(*body_parts),
                title=self.title,
                border_style="yellow",
                padding=(1, 3),
                width=60,
            ),
        )
        root["content"].update(inner)
        return root

    async def handle_key(self, key: str) -> bool:
        if key in ("esc", "q"):
            return True
        # Tab cycles focus between option list and description content
        # (when descriptions exist). Without descriptions, Tab closes.
        if key == "\t":
            if self.option_descriptions:
                self._focus = "desc" if self._focus == "list" else "list"
                return False
            return True  # no descriptions → close
        if key == "enter":
            chosen = self.options[self.selected_idx]
            await self.on_confirm(chosen)
            return True

        # Route j/k/arrows based on focus mode.
        if self._focus == "list":
            if key in ("up", "k"):
                self.selected_idx = (self.selected_idx - 1) % len(self.options)
                self._desc_scroll = 0
                return False
            if key in ("down", "j"):
                self.selected_idx = (self.selected_idx + 1) % len(self.options)
                self._desc_scroll = 0
                return False
        else:  # focus == "desc"
            if key in ("down", "j"):
                self._desc_scroll += 1
                return False
            if key in ("up", "k"):
                self._desc_scroll = max(0, self._desc_scroll - 1)
                return False
            if key in ("ctrl-d", "pgdown"):
                self._desc_scroll += 6
                return False
            if key in ("ctrl-u", "pgup"):
                self._desc_scroll = max(0, self._desc_scroll - 6)
                return False
        return False


@dataclass
class ConfirmModal:
    """Yes/No confirmation overlay. Esc cancels. Enter invokes
    `on_confirm` on the currently-highlighted option."""

    prompt: str
    on_confirm: Callable[[bool], Awaitable[None]]
    selected_yes: bool = False  # default: No, so accidental Enter cancels
    locale: str = "en"

    def render(self) -> RenderableType:
        # Yes is the destructive option (leave / concede / quit), so
        # red genuinely conveys "this is the dangerous side". Not a
        # team-color collision in this context — confirm modals never
        # render team status alongside.
        yes = Text(
            "[Yes]",
            style="bold red" if self.selected_yes else "dim",
        )
        no = Text(
            "[No]",
            style="bold green" if not self.selected_yes else "dim",
        )
        row = Text()
        row.append(yes)
        row.append("    ")
        row.append(no)
        body = Group(
            Text(self.prompt, style="white"),
            Text(""),
            Align.center(row),
            Text(""),
            Text(t("room_modal.confirm_footer", self.locale), style="dim"),
        )
        return Align.center(
            RichPanel(body, title=t("room_status.confirm", self.locale), border_style="yellow", padding=(1, 3)),
            vertical="middle",
        )

    async def handle_key(self, key: str) -> bool:
        """True = close modal. on_confirm already fired if chosen.

        Accepts every reasonable hotkey a user might try so the
        modal never looks frozen:
          - y / Y / enter  → confirm yes
          - n / N / esc    → cancel (close without firing on_confirm)
          - ← / → / h / l / j / k → move selection between Yes/No
          - \\t (Tab) / q  → cancel, same as esc. Tab specifically was
            the recurring footgun — users hit Tab to cycle panels,
            the modal ate the key with no visual feedback, and the
            TUI appeared stuck.
        """
        if key in ("esc", "\t", "q", "n", "N"):
            return True
        if key in ("y", "Y"):
            await self.on_confirm(True)
            return True
        if key in ("left", "h", "j"):
            self.selected_yes = True
            return False
        if key in ("right", "l", "k"):
            self.selected_yes = False
            return False
        if key == "enter":
            await self.on_confirm(self.selected_yes)
            return True
        return False


@dataclass
class AlertModal:
    """Single-button informational overlay used for eviction notices.

    Looks like ConfirmModal but has only an OK button — the user
    can't refuse, the alert just acknowledges what happened (lost
    network, kicked from game, model provider down) before the app
    auto-routes them somewhere safe.

    on_dismiss fires exactly once when the user dismisses (Enter /
    space / Esc / q). The TUI dispatcher is responsible for staging
    the post-dismiss screen transition; this widget itself is pure
    UI.
    """

    title: str
    body: str
    on_dismiss: Callable[[], Awaitable[None]] | None = None
    locale: str = "en"
    # Severity tints the border / title. "warning" (yellow) is the
    # default — eviction is annoying but recoverable. "error" (red)
    # is reserved for non-recoverable cases (login required); the
    # color signals "you actually need to act on this", not "the bot
    # made a tactical mistake".
    severity: str = "warning"

    def render(self) -> RenderableType:
        border = {
            "info": "cyan",
            "warning": "yellow",
            "error": "red",
        }.get(self.severity, "yellow")
        ok_button = Text("[ OK ]", style="bold green")
        body = Group(
            Text(self.body, style="white", no_wrap=False, overflow="fold"),
            Text(""),
            Align.center(ok_button),
            Text(""),
            Text(
                "Press Enter / Space / Esc to continue",
                style="dim",
            ),
        )
        return Align.center(
            RichPanel(
                body,
                title=self.title,
                border_style=border,
                padding=(1, 3),
                width=64,
            ),
            vertical="middle",
        )

    async def handle_key(self, key: str) -> bool:
        """True = close modal. on_dismiss fires once on close.

        Every reasonable acknowledgement key dismisses — the user
        should never feel stuck inside the alert. Tab / q / Esc /
        Enter / Space all work; selection arrows are intentionally
        ignored because there's nothing to select.
        """
        if key in ("enter", "esc", "q", " ", "space", "\t", "y", "Y", "n", "N"):
            cb = self.on_dismiss
            self.on_dismiss = None  # one-shot
            if cb is not None:
                await cb()
            return True
        return False


ART_FRAME_SECONDS = 2.0


@dataclass
class UnitCard:
    """Read-only card showing a unit's description / stats / tags /
    abilities / inventory.

    Holds an ordered list of units the player can browse with
    h/left and l/right while the card is open — the unit_classes
    lookup lets us repaint stats and ASCII art when the highlighted
    unit changes class. The owning MapPanel snaps its cursor to the
    card's currently-displayed unit when the card dismisses, so
    closing always lands you back on the unit you were inspecting."""

    units: list[dict[str, Any]]
    index: int
    unit_classes: dict[str, Any] | None = None
    locale: str = "en"
    _opened_at: float | None = None

    @property
    def unit(self) -> dict[str, Any]:
        return self.units[self.index]

    @property
    def class_spec(self) -> dict[str, Any] | None:
        if self.unit_classes is None:
            return None
        return self.unit_classes.get(self.unit.get("class"))

    def _stat(self, key: str, default: str = "?") -> str:
        """Prefer the unit's live value, fall back to class_spec, then
        to the placeholder."""
        u_val = self.unit.get(key)
        if u_val is not None and u_val != "":
            return str(u_val)
        if self.class_spec is not None:
            spec_val = self.class_spec.get(key)
            if spec_val is not None:
                return str(spec_val)
        return default

    def render(self) -> RenderableType:
        u = self.unit
        spec = self.class_spec or {}
        owner = u.get("owner", "?")
        team_color = "cyan" if owner == "blue" else "red"
        display = (
            u.get("display_name")
            or spec.get("display_name")
            or u.get("class")
            or u.get("id", "?")
        )
        title = f"{display} ({owner})"
        frames = u.get("art_frames") or spec.get("art_frames") or []
        text_body = self._render_text_body(team_color)
        if not frames:
            return RichPanel(
                text_body,
                title=title,
                border_style=team_color,
                padding=(0, 2),
            )
        # Two-column layout: text on the left, animated portrait on
        # the right. The portrait column auto-sizes to its widest
        # frame so descriptions on the left always have predictable
        # space and never get clipped by the art.
        if self._opened_at is None:
            self._opened_at = _time.monotonic()
        elapsed = _time.monotonic() - self._opened_at
        idx = int(elapsed / ART_FRAME_SECONDS) % len(frames)
        frame = frames[idx]
        art_width = max(
            (len(line) for f in frames for line in f.split("\n")),
            default=0,
        )
        # Add a small gutter so art doesn't kiss the right border.
        art_col_width = art_width + 2
        grid = Table.grid(expand=True, padding=(0, 1))
        grid.add_column(ratio=1)
        grid.add_column(no_wrap=True, width=art_col_width)
        grid.add_row(text_body, Text(frame, style=team_color))
        return RichPanel(
            grid,
            title=title,
            border_style=team_color,
            padding=(0, 2),
        )

    def _render_text_body(self, team_color: str) -> RenderableType:
        u = self.unit
        spec = self.class_spec or {}
        rows: list[RenderableType] = []
        desc = spec.get("description") or u.get("description") or ""
        if desc:
            rows.append(Text(desc, style="italic"))
            rows.append(Text(""))

        stats = Table.grid(padding=(0, 2))
        stats.add_column(style="dim")
        stats.add_column()
        hp_now = u.get("hp")
        hp_max = self._stat("hp_max")
        _lc = self.locale
        stats.add_row(
            t("stat.hp", _lc),
            f"{hp_now if hp_now is not None else hp_max} / {hp_max}",
        )
        stats.add_row(t("stat.atk", _lc), self._stat("atk"))
        def_val = u.get("def")
        if def_val is None:
            def_val = spec.get("defense") or spec.get("def") or "?"
        stats.add_row(t("stat.def", _lc), str(def_val))
        stats.add_row(t("stat.res", _lc), self._stat("res"))
        stats.add_row(t("stat.spd", _lc), self._stat("spd"))
        stats.add_row(t("stat.move", _lc), self._stat("move"))
        rng = u.get("rng") or [
            spec.get("rng_min", self._stat("rng_min")),
            spec.get("rng_max", self._stat("rng_max")),
        ]
        stats.add_row(t("stat.range", _lc), f"{rng[0]}–{rng[1]}")
        if u.get("is_magic") or spec.get("is_magic"):
            stats.add_row(t("stat.type", _lc), t("stat.magic", _lc))
        if u.get("can_heal") or spec.get("can_heal"):
            stats.add_row(t("stat.can_heal", _lc), t("stat.yes", _lc))
        rows.append(stats)

        tags = u.get("tags") or spec.get("tags") or []
        if tags:
            rows.append(Text(""))
            rows.append(Text(f"{t('section.tags', self.locale)}: " + ", ".join(tags), style="dim"))

        abilities = u.get("abilities") or spec.get("abilities") or []
        if abilities:
            rows.append(Text(""))
            rows.append(Text(f"{t('section.abilities', self.locale)}: " + ", ".join(abilities)))

        inv = u.get("default_inventory") or spec.get("default_inventory") or []
        if inv:
            rows.append(Text(""))
            rows.append(Text(f"{t('section.inventory', self.locale)}: " + ", ".join(inv)))

        rows.append(Text(""))
        if len(self.units) > 1:
            rows.append(
                Text(t("unit_card.nav_multi", self.locale), style="dim")
            )
        else:
            rows.append(Text(t("unit_card.nav_single", self.locale), style="dim"))
        return Group(*rows)

    def navigate(self, step: int) -> None:
        """Move the highlighted unit by `step` (wraps). Resets the
        animation clock so the new portrait starts at frame 0."""
        if not self.units:
            return
        self.index = (self.index + step) % len(self.units)
        self._opened_at = None

    async def handle_key(self, key: str) -> bool:
        # Tab closes the card too — otherwise users pressing Tab to
        # leave the unit card feel stuck.
        return key in ("esc", "enter", "q", "\t")
