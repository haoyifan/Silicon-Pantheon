"""In-game screen — four-panel grid.

    ┌────────────┬─────────┐
    │   Map      │ Player  │
    │            │         │
    ├────────────┼─────────┤
    │ Reasoning  │ Coach   │
    └────────────┴─────────┘

Tab cycles focus across panels. Arrows / j-k / Enter dispatch to the
focused panel only.

  - Map (focused): tile cursor with ←↑↓→ / h j k l. Enter on a unit
    opens its UnitCard with description / stats / tags / abilities.
  - Player (focused): scrollable roster of both teams — HP, class,
    dead units rendered strikethrough.
  - Reasoning (focused): up/down scroll the agent-thought log.
  - Coach (focused): type freely — Enter sends, Esc clears buffer.

There's no Actions panel during gameplay: end-turn and concede are
agent-driven via MCP tools, and `q` in the footer is the only
player-side command (opens a confirm before quitting).
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from typing import Any

from rich.align import Align
from rich.console import Group, RenderableType
from rich.layout import Layout
from rich.panel import Panel as RichPanel
from rich.table import Table
from rich.text import Text

from silicon_pantheon.client.tui.app import POLL_INTERVAL_S, Screen, TUIApp
from silicon_pantheon.client.tui.panels import Panel, border_style
from silicon_pantheon.client.tui.screens.room import (
    ConfirmModal,
    UnitCard,
    _describe_win_condition,
    _terrain_effect_summary,
    _unit_cell_style,
    _unit_display_name,
)

log = logging.getLogger("silicon.tui.game")


# ---- panel: Player (turn / team / agent status) ----


def _status_style(status: str) -> str:
    """Color the status cell by how "done" the unit is this turn:
    ready (still to act) = green, moved (partial) = yellow, done
    (spent) = dim."""
    if status == "ready":
        return "green"
    if status == "moved":
        return "yellow"
    if status == "done":
        return "dim"
    return "white"


def _hp_style(hp: int | str, hp_max: int | str) -> str:
    """Color the HP cell by remaining ratio so damage is readable at
    a glance: >80% = green, 30-80% = yellow, <30% = red.

    Pre-change everything rendered white on black and users had to
    mentally divide hp/hp_max for every row. Now the colour is the
    diff — green means "still full", yellow "bloodied", red
    "one good hit from dying".
    """
    try:
        hp_i = int(hp)
        hp_max_i = int(hp_max)
    except (TypeError, ValueError):
        return "white"
    if hp_max_i <= 0 or hp_i <= 0:
        return "red"
    ratio = hp_i / hp_max_i
    if ratio > 0.8:
        return "bold green"
    if ratio >= 0.3:
        return "bold yellow"
    return "bold red"


class PlayerPanel(Panel):
    """Turn / team / agent status + compact unit roster for both
    sides. Dead units stay in the roster, rendered dim + strikethrough
    — they don't silently disappear when killed.

    Per-unit cursor: j/k moves through units (not just scroll).
    When focused, the cursor drives a cross-highlight: the unit at
    cursor_idx sets screen.highlighted_unit_id, and the GameMapPanel
    renders that unit's glyph with a distinctive style. Vice versa:
    when the Map is focused and its cursor sits on a unit, the
    roster highlights that unit's row."""

    @property
    def title(self) -> str:
        from silicon_pantheon.client.locale import t
        return t("panel.player", self.screen.app.state.locale)

    def __init__(self, screen: "GameScreen") -> None:
        self.screen = screen
        self.scroll = 0
        self.cursor_idx = 0  # index into the flat _roster list
        self._roster: list[dict] = []  # rebuilt each render

    def key_hints(self) -> str:
        from silicon_pantheon.client.locale import t
        return t("game_player.key_hints", self.screen.app.state.locale)

    async def handle_key(self, key: str) -> "Screen | None":
        n = len(self._roster)
        if n == 0:
            return None
        if key in ("down", "j"):
            self.cursor_idx = min(self.cursor_idx + 1, n - 1)
        elif key in ("up", "k"):
            self.cursor_idx = max(self.cursor_idx - 1, 0)
        elif key == "ctrl-d":
            self.cursor_idx = min(self.cursor_idx + 6, n - 1)
        elif key == "ctrl-u":
            self.cursor_idx = max(self.cursor_idx - 6, 0)
        elif key == "enter" and 0 <= self.cursor_idx < n:
            u = self._roster[self.cursor_idx]
            if u.get("alive", u.get("hp", 0) > 0):
                self.screen.open_unit_card(u)
        # Drive the cross-highlight on every key and clear range
        # overlay (user must press `r` again on the new unit).
        if 0 <= self.cursor_idx < n:
            new_id = self._roster[self.cursor_idx].get("id")
            if new_id != self.screen.highlighted_unit_id:
                self.screen._clear_range_overlay()
            self.screen.highlighted_unit_id = new_id
        return None

    def render(self, focused: bool) -> RenderableType:
        gs = self.screen.state or {}
        my_team = gs.get("you") or "?"
        active = gs.get("active_player", "?")
        turn = gs.get("turn", "?")
        max_turns = gs.get("max_turns") or (gs.get("rules") or {}).get("max_turns", "?")
        status = gs.get("status", "?")
        winner = gs.get("winner")

        from silicon_pantheon.client.locale import t as _t
        _lc = self.screen.app.state.locale

        rows: list[RenderableType] = []
        rows.append(
            Text(
                f"{_t('status.you', _lc)}: {my_team}   {_t('status.turn', _lc)} {turn}/{max_turns}",
                style="bold cyan" if my_team == "blue" else "bold red",
            )
        )
        my_turn = active == my_team
        rows.append(
            Text(
                _t("status.your_turn", _lc) if my_turn else _t("status.opponent_turn", _lc),
                style="bold green" if my_turn else "dim",
            )
        )
        if status == "game_over":
            line = Text(_t("status.game_over", _lc), style="bold yellow")
            if winner:
                line.append(
                    f" — {winner}",
                    style=" bold green" if winner == my_team else " bold red",
                )
            rows.append(line)
        if self.screen.app.state.agent is not None:
            busy = (
                self.screen.app.state.agent_task is not None
                and not self.screen.app.state.agent_task.done()
            )
            rows.append(
                Text(
                    _t("status.agent_thinking", _lc) if busy else _t("status.agent_idle", _lc),
                    style="yellow" if busy else "dim",
                )
            )

        # Build flat roster used for cursor indexing + cross-highlight.
        units = gs.get("units") or []
        scen_desc = self.screen.app.state.scenario_description
        self._roster = []
        for team in ("blue", "red"):
            for u in units:
                if u.get("owner") == team:
                    self._roster.append(u)
        # Clamp cursor after roster rebuild (unit count can change
        # mid-game when units die).
        if self._roster:
            self.cursor_idx = max(0, min(self.cursor_idx, len(self._roster) - 1))

        # Cross-highlight: if Map focused AND its cursor is on a unit,
        # screen.highlighted_unit_id was set by the Map panel. We
        # check it here to find which roster row to highlight even
        # when PlayerPanel is NOT focused.
        highlight_id = self.screen.highlighted_unit_id

        # Per-unit action lookup for "last turn" annotations.
        last_actions = self.screen.unit_last_actions

        # Render per-team roster. Track which row in `rows` the
        # cursor unit occupies so we can auto-scroll to it.
        cursor_row_idx: int | None = None
        roster_idx = 0
        for team in ("blue", "red"):
            team_units = [u for u in self._roster if u.get("owner") == team]
            if not team_units:
                continue
            rows.append(Text(""))
            header_style = "bold cyan" if team == "blue" else "bold red"
            rows.append(Text(f"{team}:", style=header_style))
            rows.append(
                Text(
                    f"  {_t('game_player.unit_header', _lc):<14}  {_t('game_player.hp_header', _lc):>7}  {_t('game_player.status_header', _lc)}",
                    style="bold dim",
                )
            )
            for u in team_units:
                alive = u.get("alive", u.get("hp", 0) > 0)
                hp = u.get("hp", "?")
                hp_max = u.get("hp_max", "?")
                uid = u.get("id", "")
                name = _unit_display_name(u, scen_desc)
                is_cursor = focused and roster_idx == self.cursor_idx
                is_highlight = (not focused) and uid == highlight_id
                if is_cursor:
                    cursor_row_idx = len(rows)
                if alive:
                    unit_status = str(u.get("status", "ready"))
                    hp_str = f"{hp}/{hp_max}"
                    if is_cursor or is_highlight:
                        row = Text.assemble(
                            ("► ", "bold yellow" if is_cursor else "bold white"),
                            (f"{name[:14]:<14}", "reverse white"),
                            ("  ", None),
                            (f"{hp_str:>7}", _hp_style(hp, hp_max)),
                            ("  ", None),
                            (unit_status, _status_style(unit_status)),
                        )
                    else:
                        row = Text.assemble(
                            ("  ", None),
                            (f"{name[:14]:<14}", "white"),
                            ("  ", None),
                            (f"{hp_str:>7}", _hp_style(hp, hp_max)),
                            ("  ", None),
                            (unit_status, _status_style(unit_status)),
                        )
                    rows.append(row)
                    # Last-action annotation on a sub-line below the
                    # unit — only for meaningful actions (move/attack/
                    # heal), cleared at turn start.
                    action_desc = last_actions.get(uid)
                    if action_desc:
                        rows.append(
                            Text(f"    └ {action_desc}", style="dim italic")
                        )
                else:
                    marker = f"✗ {name[:12]}"
                    hp_str = f"0/{hp_max}"
                    prefix = "► " if (is_cursor or is_highlight) else "  "
                    prefix_style = "bold yellow" if is_cursor else (
                        "bold white" if is_highlight else None
                    )
                    row = Text.assemble(
                        (prefix, prefix_style),
                        (f"{marker:<14}", "dim"),
                        ("  ", None),
                        (f"{hp_str:>7}", "dim"),
                        ("  ", None),
                        (_t("unit_status.dead", _lc), "bold red"),
                    )
                    rows.append(row)
                roster_idx += 1

        # Auto-scroll: ensure the cursor row is always visible.
        # The PlayerPanel occupies the top-right cell of a 2×2 layout:
        #   top row = ratio 3 out of (3+2)=5 → ~60% of screen height
        #   minus panel border (2 lines) + header/footer (2 lines)
        # Using ch * 3/5 - 4 as the estimate. Conservative is better
        # than generous — a too-large `visible` prevents scrolling.
        try:
            ch = self.screen.app.console.height
        except Exception:
            ch = 30
        visible = max(1, int(ch * 3 / 5) - 4)
        if cursor_row_idx is not None:
            if self.cursor_idx == 0:
                # First unit: always show from the top so the turn
                # status / agent-thinking header rows are visible.
                self.scroll = 0
            elif cursor_row_idx < self.scroll:
                self.scroll = cursor_row_idx
            elif cursor_row_idx >= self.scroll + visible:
                self.scroll = cursor_row_idx - visible + 1
        max_scroll = max(0, len(rows) - visible)
        if self.scroll > max_scroll:
            self.scroll = max_scroll
        if self.scroll > 0 and rows:
            rows = rows[self.scroll :]
        return RichPanel(
            Group(*rows),
            title=self.title,
            border_style=border_style(focused),
            padding=(0, 1),
        )


# ---- panel: Map (cursor + unit card on Enter) ----


class GameMapPanel(Panel):
    @property
    def title(self) -> str:
        from silicon_pantheon.client.locale import t
        return t("panel.map", self.screen.app.state.locale)

    def __init__(self, screen: "GameScreen") -> None:
        self.screen = screen
        self.cx = 0
        self.cy = 0

    def key_hints(self) -> str:
        from silicon_pantheon.client.locale import t
        return t("game_map.key_hints", self.screen.app.state.locale)

    def _state(self) -> dict[str, Any]:
        return self.screen.state or {}

    def render(self, focused: bool) -> RenderableType:
        # Card takes the whole panel while it's up, same as the room
        # MapPanel — the board hides until the player closes the card.
        card = self.screen.unit_card
        if card is not None:
            return card.render()
        gs = self._state()
        board = gs.get("board") or {}
        w = int(board.get("width", 0))
        h = int(board.get("height", 0))
        tiles = board.get("tiles", [])
        units = gs.get("units", [])
        if w > 0 and h > 0:
            self.cx = max(0, min(self.cx, w - 1))
            self.cy = max(0, min(self.cy, h - 1))

        tile_by_pos = {(int(t.get("x", 0)), int(t.get("y", 0))): t for t in tiles}
        unit_at: dict[tuple[int, int], dict] = {}
        for u in units:
            if not u.get("alive", u.get("hp", 0) > 0):
                continue
            pos = u.get("pos") or {}
            unit_at[(int(pos.get("x", -1)), int(pos.get("y", -1)))] = u

        text = Text()
        text.append(
            "   " + " ".join(f"{x:>2}" for x in range(w)) + "\n", style="dim"
        )
        for y in range(h):
            text.append(f"{y:>2} ", style="dim")
            for x in range(w):
                u = unit_at.get((x, y))
                if u is not None:
                    g, st = _unit_cell_style(u)
                else:
                    t = tile_by_pos.get((x, y), {})
                    g, st = _terrain_cell(
                        t.get("type", "unknown"),
                        (self.screen.app.state.scenario_description or {}).get(
                            "terrain_types"
                        ),
                    )
                is_cursor = focused and x == self.cx and y == self.cy
                # Cross-highlight from PlayerPanel cursor.
                is_highlight = (
                    u is not None
                    and u.get("id") == self.screen.highlighted_unit_id
                    and not is_cursor
                )
                # Range overlay: move tiles (blue bg) / attack tiles
                # (red bg). Rendered BEHIND the glyph so units are
                # still readable. Only when range_overlay_unit is set.
                in_move_range = (x, y) in self.screen.range_move_tiles
                in_atk_range = (x, y) in self.screen.range_attack_tiles

                if is_cursor:
                    text.append(f"[{g}]", style=f"reverse {st}")
                elif is_highlight:
                    text.append(f"({g})", style=f"bold underline {st}")
                elif in_move_range:
                    text.append(f" {g} ", style=f"on dark_blue {st}")
                elif in_atk_range:
                    text.append(f" {g} ", style=f"on dark_red {st}")
                else:
                    text.append(f" {g} ", style=st)
            text.append("\n")
        footer_body = self._cursor_tooltip(w, h, tile_by_pos, unit_at)
        return RichPanel(
            Group(text, Text(""), footer_body),
            title=self.title,
            border_style=border_style(focused),
            padding=(0, 1),
        )

    def _cursor_tooltip(
        self,
        w: int,
        h: int,
        tile_by_pos: dict[tuple[int, int], dict],
        unit_at: dict[tuple[int, int], dict],
    ) -> RenderableType:
        from silicon_pantheon.client.locale import t as _loc
        _lc2 = self.screen.app.state.locale
        if w == 0 or h == 0:
            return Text(_loc("game_map.loading_map", _lc2), style="dim italic")
        tile = tile_by_pos.get((self.cx, self.cy), {})
        terrain = str(tile.get("type", "plain"))
        u = unit_at.get((self.cx, self.cy))
        line = Text()
        line.append(f"({self.cx}, {self.cy}) ", style="dim")
        line.append(f"{_loc('game_map.terrain_label', _lc2)}: {terrain}", style="yellow")
        summary = _terrain_effect_summary(
            self.screen.app.state.scenario_description, terrain, _lc2
        )
        if summary:
            line.append(f" — {summary}", style="dim")
        if u:
            owner = u.get("owner", "?")
            color = "cyan" if owner == "blue" else "red"
            name = _unit_display_name(
                u, self.screen.app.state.scenario_description
            )
            line.append("   ")
            line.append(
                f"{name} hp {u.get('hp', '?')}/{u.get('hp_max', '?')}",
                style=f"bold {color}",
            )
            line.append("   ")
            line.append(_loc("game_map.enter_details", _lc2), style="dim italic")
        return line

    async def handle_key(self, key: str) -> Screen | None:
        gs = self._state()
        board = gs.get("board") or {}
        w = int(board.get("width", 0))
        h = int(board.get("height", 0))
        if w == 0 or h == 0:
            return None
        card = self.screen.unit_card
        if card is not None:
            if key in ("left", "h"):
                card.navigate(-1)
                return None
            if key in ("right", "l"):
                card.navigate(1)
                return None
            if key in ("esc", "enter", "q"):
                pos = card.unit.get("pos") or {}
                self.cx = int(pos.get("x", self.cx))
                self.cy = int(pos.get("y", self.cy))
                self.screen.unit_card = None
                return None
            return None
        if key in ("up", "k"):
            self.cy = (self.cy - 1) % h
        elif key in ("down", "j"):
            self.cy = (self.cy + 1) % h
        elif key in ("left", "h"):
            self.cx = (self.cx - 1) % w
        elif key in ("right", "l"):
            self.cx = (self.cx + 1) % w
        elif key == "enter":
            for u in gs.get("units", []):
                if not u.get("alive", u.get("hp", 0) > 0):
                    continue
                pos = u.get("pos") or {}
                if int(pos.get("x", -1)) == self.cx and int(pos.get("y", -1)) == self.cy:
                    self.screen.open_unit_card(u)
                    break
            return None
        # After cursor move: drive cross-highlight from whatever unit
        # the map cursor now sits on (or clear if empty tile).
        unit_here = None
        for u in gs.get("units", []):
            if not u.get("alive", u.get("hp", 0) > 0):
                continue
            pos = u.get("pos") or {}
            if int(pos.get("x", -1)) == self.cx and int(pos.get("y", -1)) == self.cy:
                unit_here = u
                break
        self.screen.highlighted_unit_id = (
            unit_here.get("id") if unit_here else None
        )
        # Clear range overlay on any cursor movement — user must
        # press `r` again on the new unit to see its range.
        self.screen._clear_range_overlay()
        return None


# Terrain rendering has moved to silicon_pantheon.client.tui.terrain
# so the in-game map, room preview, and scenario picker all share one
# source of truth (was three divergent copies — see that module's
# docstring for the bug history).
from silicon_pantheon.client.tui.terrain import terrain_cell as _terrain_cell  # noqa: E402


# ---- panel: Reasoning (scrollable agent thoughts) ----


class ReasoningPanel(Panel):
    """Scrollable thought log.

    Scroll unit is a logical line, not an entry: a single reasoning
    block from a reasoning model can be thousands of chars / dozens
    of lines, and entry-based scrolling hid everything past the
    panel's visible height with no way to reach it. Now k/j move
    by 3 lines, K/J by a page (12 lines), and 0 jumps to the newest
    tail. Full raw text is also mirrored into the client log at
    silicon.agent.thoughts for out-of-band review.
    """

    @property
    def title(self) -> str:
        from silicon_pantheon.client.locale import t
        return t("panel.reasoning", self.screen.app.state.locale)

    def __init__(self, screen: "GameScreen") -> None:
        self.screen = screen
        # Line offset from the END of the text. 0 = pinned to newest.
        self.line_offset = 0
        self._last_total_lines = 0
        # One-shot latch for the vim `gg` shortcut (press g twice
        # → go to top). Also reset any time a non-g key comes in.
        self._gg_primed = False

    def key_hints(self) -> str:
        from silicon_pantheon.client.locale import t
        return t("game_reasoning.key_hints", self.screen.app.state.locale)

    def _build_all_lines(self) -> list[tuple[str, str]]:
        """Flatten thoughts into (style, text) line tuples, oldest
        first. Each entry in the result corresponds to one *display*
        row — long paragraphs are hard-wrapped to the panel's inner
        width before counting, so `line_offset += 3` reliably moves
        three visible rows instead of potentially hiding an entire
        wrapped paragraph behind a single scroll step.

        Earlier versions split only on '\\n' and let Rich do the
        wrapping, which made scrolling feel non-linear: a short
        thought scrolled one row per step, a 500-char paragraph
        scrolled the whole block at once.
        """
        import textwrap

        # Reasoning panel gets ratio=2 of the bottom half's 2:1 split,
        # so ~2/3 of the total console width. Subtract borders +
        # padding. Fall back conservatively if the console width
        # can't be read.
        try:
            cw = self.screen.app.console.width
        except Exception:
            cw = 80
        inner_width = max(20, int(cw * 2 / 3) - 6)

        out: list[tuple[str, str]] = []
        for ts, team, t in self.screen.app.state.thoughts:
            team_style = "cyan" if team == "blue" else "red"
            out.append((team_style + " bold", f"[{ts}] ({team})"))
            for raw_line in t.splitlines() or [""]:
                if not raw_line.strip():
                    out.append(("white", ""))
                    continue
                wrapped = textwrap.wrap(
                    raw_line,
                    width=inner_width,
                    break_long_words=True,
                    break_on_hyphens=False,
                ) or [raw_line]
                for w in wrapped:
                    out.append(("white", w))
            out.append(("", ""))  # blank separator between thoughts
        return out

    def render(self, focused: bool) -> RenderableType:
        lines = self._build_all_lines()
        total = len(lines)
        # Pin user's view: if they've scrolled up and new lines land
        # at the bottom, keep their eyes on the same content rather
        # than yanking them to the newest.
        new_lines = total - self._last_total_lines
        if new_lines > 0 and self.line_offset > 0:
            self.line_offset += new_lines
        self._last_total_lines = total

        from silicon_pantheon.client.locale import t as _t
        _lc = self.screen.app.state.locale
        if total == 0:
            return RichPanel(
                Text(_t("game_reasoning.no_reasoning", _lc), style="dim italic"),
                title=self.title,
                border_style=border_style(focused),
                padding=(0, 1),
            )

        self.line_offset = max(0, min(self.line_offset, max(0, total - 1)))
        # Rich renders content top-down and crops any overflow from
        # the bottom of the panel — if we feed it 400 lines into a
        # 10-row slot, it shows the oldest 10 and silently drops the
        # rest. That looked like "scrolling does nothing" from the
        # user's view because the hidden rows off the bottom were
        # the only ones our offset could move.
        #
        # Estimate the panel's visible height and render exactly that
        # many lines ending at `end`. Then Rich shows the full window
        # top-to-bottom with the newest at the bottom as intended.
        # Reasoning panel height ≈ (console_height - 2) * 2/5 - 2
        # (body = total - header/footer, bottom = 2/5 of body,
        # reasoning gets the bottom's full height minus borders).
        try:
            ch = self.screen.app.console.height
        except Exception:
            ch = 30
        visible_rows = max(4, (ch - 2) * 2 // 5 - 2)

        end = total - self.line_offset
        start = max(0, end - visible_rows)
        body = Text(no_wrap=False, overflow="fold")
        for i in range(start, end):
            style, txt = lines[i]
            body.append(txt, style=style or None)
            if i != end - 1:
                body.append("\n")

        if self.line_offset == 0:
            title = f"{self.title} — {_t('game_reasoning.live', _lc)} ({end}/{total})"
        else:
            hidden_below = total - end
            title = (
                f"{self.title} — {_t('game_reasoning.paused', _lc)}  ({_t('game_reasoning.showing', _lc)} {end}/{total}"
                + (f", {hidden_below} {_t('game_reasoning.new_below', _lc)}"
                   if hidden_below > 0 else "")
                + ")"
            )
        return RichPanel(
            body,
            title=title,
            border_style=border_style(focused),
            padding=(0, 1),
        )

    async def handle_key(self, key: str) -> Screen | None:
        # ReasoningPanel has INVERTED scroll: offset 0 = tail (newest),
        # higher offset = further back into history. So vim's
        # "forward/down" keys DECREASE offset (toward newer); "back/
        # up" keys INCREASE offset (toward older). The panel helper
        # assumes the opposite convention so we handle the keys
        # inline here.
        PAGE = 12
        HALF = PAGE // 2
        BACK_STEP = 3
        FORWARD_STEP = 3
        # Upper-bound clamp deferred to render(); we only floor at 0
        # here so the panel's "scroll back" still works on the first
        # render before _last_total_lines is populated.
        was_gg_primed = self._gg_primed
        if key != "g":
            self._gg_primed = False

        # Toward-newer (visual "down").
        if key in ("down", "j"):
            self.line_offset = max(0, self.line_offset - FORWARD_STEP)
            return None
        if key == "ctrl-d":
            self.line_offset = max(0, self.line_offset - HALF)
            return None
        if key in ("ctrl-f", "pgdown"):
            self.line_offset = max(0, self.line_offset - PAGE)
            return None
        # Toward-older (visual "up"). Render clamps to oldest line.
        if key in ("up", "k"):
            self.line_offset += BACK_STEP
            return None
        if key == "ctrl-u":
            self.line_offset += HALF
            return None
        if key in ("ctrl-b", "pgup"):
            self.line_offset += PAGE
            return None
        # Jumps.
        if key in ("shift-g", "end", "0"):
            # Tail (newest). '0' kept for the legacy hotkey the
            # key_hints string used to advertise.
            self.line_offset = 0
            return None
        if key == "home":
            # Top / oldest. Use a large value; render clamps.
            self.line_offset = 10**9
            return None
        if key == "g":
            if was_gg_primed:
                self.line_offset = 10**9  # render clamps to oldest
                self._gg_primed = False
            else:
                self._gg_primed = True
            return None
        return None


# ---- panel: Coach (text input + history) ----


class CoachPanel(Panel):
    @property
    def title(self) -> str:
        from silicon_pantheon.client.locale import t
        return t("panel.coach", self.screen.app.state.locale)

    def __init__(self, screen: "GameScreen") -> None:
        self.screen = screen
        self.buffer = ""
        self.history: deque[str] = deque(maxlen=5)

    def key_hints(self) -> str:
        from silicon_pantheon.client.locale import t
        return t("game_coach.key_hints_unfocused", self.screen.app.state.locale)

    def render(self, focused: bool) -> RenderableType:
        from silicon_pantheon.client.locale import t as _t
        _lc = self.screen.app.state.locale
        rows: list[RenderableType] = []
        if focused:
            prompt = Text(no_wrap=False, overflow="fold")
            prompt.append("> ", style="yellow bold")
            prompt.append(self.buffer, style="white")
            prompt.append("▌", style="yellow")
            rows.append(prompt)
            rows.append(
                Text(_t("game_coach.key_hints_focused", _lc), style="dim")
            )
        else:
            rows.append(
                Text(
                    _t("game_coach.tab_prompt", _lc),
                    style="dim italic",
                )
            )
        if self.history:
            rows.append(Text(""))
            rows.append(Text(_t("game_coach.recent", _lc), style="dim"))
            for m in list(self.history)[-3:]:
                rows.append(Text(f"  • {m}", style="dim"))
        return RichPanel(
            Group(*rows),
            title=self.title,
            border_style=border_style(focused),
            padding=(0, 1),
        )

    async def handle_key(self, key: str) -> Screen | None:
        if key == "esc":
            self.buffer = ""
            return None
        if key == "enter":
            text = self.buffer.strip()
            self.buffer = ""
            if not text:
                return None
            await self.screen.send_coach_message(text)
            self.history.append(text)
            return None
        if key == "backspace":
            self.buffer = self.buffer[:-1]
            return None
        # Use the RAW key (before lowercasing) so capital letters,
        # question marks, and other shifted characters reach the
        # buffer intact. The app stores _raw_key on each dispatch
        # cycle; fall back to `key` if unavailable.
        raw = getattr(self.screen.app, "_raw_key", key)
        if len(raw) == 1 and raw.isprintable():
            self.buffer += raw
            return None
        # Also accept paste events (bracketed paste delivers
        # multi-char strings prefixed with "paste:").
        if key.startswith("paste:"):
            self.buffer += key[6:]
            return None
        return None


# ---- the screen ----


class GameScreen(Screen):
    def __init__(self, app: TUIApp):
        self.app = app
        self.state: dict[str, Any] | None = None
        self._last_poll = 0.0
        # Inline unit card rendered inside the Map panel when the
        # cursor-Enter combo opens one. Not a full-screen modal — the
        # rest of the layout stays visible.
        self.unit_card: UnitCard | None = None
        self._confirm: ConfirmModal | None = None
        # F3 overlay: full-screen scenario description (story, win
        # conditions, armies, units). Same content the room screen
        # shows pre-game; reuses DescriptionPanel verbatim. While
        # open, scroll keys route to it and F3/Esc/q close it.
        self._scenario_overlay: Any = None
        # Cross-highlighting: when the Player panel cursor is on a
        # unit, its ID is stored here so the Map panel can render the
        # corresponding glyph with a highlight. Vice versa for map
        # cursor sitting on a unit. Cleared when focus moves to a
        # panel that doesn't participate (reasoning / coach).
        self.highlighted_unit_id: str | None = None
        # Range overlay: toggle with `r`. Shows move tiles (blue bg)
        # and attack tiles (red bg) for the highlighted unit. Cleared
        # on cursor move or second `r` press.
        self.range_overlay_unit: str | None = None
        self.range_move_tiles: set[tuple[int, int]] = set()
        self.range_attack_tiles: set[tuple[int, int]] = set()
        # Per-unit last-action cache. Populated from get_history at
        # turn boundaries. Maps unit_id → compact one-line description
        # like "moved (3,2)→(5,4)" or "attacked u_r_k1 dealt 8 dmg".
        self.unit_last_actions: dict[str, str] = {}
        # Track last_action identity so we update incrementally on
        # each state poll rather than fetching full history.
        self._last_action_seen: dict | None = None

        self.map_panel = GameMapPanel(self)
        self.reasoning_panel = ReasoningPanel(self)
        self.coach_panel = CoachPanel(self)
        # No Actions panel during gameplay: end-turn / concede are
        # agent-driven, and Quit lives in the footer as `q`. Skipping
        # Actions frees the full right column for the Player panel's
        # unit roster.
        self._panels: list[Panel] = [
            self.map_panel,
            PlayerPanel(self),
            self.reasoning_panel,
            self.coach_panel,
        ]
        # Default to the Map panel so the player can immediately scan
        # the board with the cursor.
        self._focus_idx = 0

    # Rate-limit "skipped trigger" logs to one entry per distinct
    # reason per N ticks, so we see the current blocker without
    # spamming the file.
    _trigger_skip_reason: str = ""
    _trigger_skip_count: int = 0

    def _log_trigger_skip(self, reason: str) -> None:
        if reason == self._trigger_skip_reason:
            self._trigger_skip_count += 1
            # Log every 30th repeat so a persistent block shows up.
            if self._trigger_skip_count % 30 == 0:
                log.info(
                    "_maybe_trigger_agent: still skipping (%s, %d ticks)",
                    reason, self._trigger_skip_count,
                )
            return
        # Reason changed — emit a transition line.
        if self._trigger_skip_reason:
            log.info(
                "_maybe_trigger_agent: skip %r -> %r after %d ticks",
                self._trigger_skip_reason, reason,
                self._trigger_skip_count,
            )
        else:
            log.info(
                "_maybe_trigger_agent: skipping (%s)", reason,
            )
        self._trigger_skip_reason = reason
        self._trigger_skip_count = 1

    async def on_enter(self, app: TUIApp) -> None:
        log.info("GameScreen.on_enter: starting")
        # Reasoning is per-match: clear the thought buffer so the new
        # game's panel doesn't start with old text. The buffer lives
        # on SharedState (which survives screen transitions — it's
        # how PostMatchScreen can show the final transcript) so we
        # have to reset it ourselves each time a new match starts.
        app.state.thoughts.clear()
        await self._refresh_state()
        if app.state.agent is None:
            await self._maybe_build_agent(app)
        log.info(
            "GameScreen.on_enter: finished agent=%s",
            "set" if app.state.agent is not None else "none",
        )

    async def on_exit(self, app: TUIApp) -> None:
        log.info("GameScreen.on_exit")
        if app.state.agent_task is not None and not app.state.agent_task.done():
            app.state.agent_task.cancel()
        app.state.agent_task = None
        # Intentionally do NOT close app.state.agent — PostMatchScreen
        # needs the live session for summarize_match.

    async def _maybe_build_agent(self, app: TUIApp) -> None:
        log.info(
            "maybe_build_agent: kind=%s provider=%s model=%s",
            app.state.kind, app.state.provider, app.state.model,
        )
        if app.state.kind not in ("ai", "hybrid"):
            return
        if not app.state.model:
            return
        if app.client is None:
            return
        scenario = (app.state.last_room_state or {}).get("scenario") or ""
        if not scenario:
            log.warning("maybe_build_agent: no scenario")
            return

        from silicon_pantheon.client.agent_bridge import NetworkedAgent

        async def on_thought(text: str) -> None:
            # Preserve newlines: reasoning models emit paragraphs, and
            # collapsing all whitespace to single spaces turned long
            # chain-of-thought into one giant run-on that the panel
            # was forced to fold into a wall of text. strip() just
            # trims leading/trailing blank space per entry.
            stripped = text.strip()
            if not stripped:
                return
            from datetime import datetime

            ts = datetime.now().strftime("%H:%M:%S")
            team = (app.state.last_game_state or {}).get("you") or "blue"
            app.state.thoughts.append((ts, team, stripped))
            # Mirror each thought to the client log so the full text
            # is always recoverable, even if the TUI panel crops it.
            # The file lives at ~/.silicon-pantheon/logs/client-*.log.
            import logging as _logging

            _logging.getLogger("silicon.agent.thoughts").info(
                "[%s team=%s] %s", ts, team, stripped
            )
            # Push to the server's replay so silicon-play renders the
            # reasoning alongside the actions. Fire-and-forget — a
            # transport hiccup must not block the agent loop. The
            # server pins this thought to the connection's team, so
            # we don't need to send team/turn explicitly.
            if app.client is not None:
                async def _push() -> None:
                    try:
                        await app.client.call("record_thought", text=stripped)
                    except Exception:
                        # Replay-loss is non-fatal; the panel + log
                        # still have the text.
                        pass
                import asyncio as _asyncio
                _asyncio.create_task(_push())

        # `lessons_dir=None` disables both lesson injection into the
        # system prompt AND saving the post-match summary. Toggled via
        # the room-screen Actions panel ("Lessons: on/off"); default
        # on preserves prior behavior.
        from pathlib import Path as _Path

        lessons_dir = _Path("lessons") if app.state.use_lessons else None
        # Per-turn agent time budget = room's turn_time_limit_s when
        # the host set it, otherwise the adapter's default (180s). The
        # host configures this in the room-setup Actions panel.
        # Matching the server-declared limit means the agent's loop
        # exits roughly when the server's turn timer would forfeit
        # anyway, so we don't burn requests past that point.
        room_state = app.state.last_room_state or {}
        turn_budget = room_state.get("turn_time_limit_s")
        # Fall back to 1800.0 (30 min) if the room state didn't carry
        # it (older server, or the field was dropped from the preview).
        time_budget_s = float(turn_budget) if turn_budget else 1800.0
        app.state.agent = NetworkedAgent(
            client=app.client,
            model=app.state.model,
            scenario=scenario,
            strategy=app.state.strategy_text,
            lessons_dir=lessons_dir,
            thoughts_callback=on_thought,
            # Hand over the scenario bundle the room screen already
            # fetched, localized for the user's language. The locale
            # merge only affects display strings (names, descriptions,
            # narrative); stats come from the base config unchanged.
            scenario_description=self._localized_scenario(app),
            time_budget_s=time_budget_s,
            locale=app.state.locale,
        )

    def _localized_scenario(self, app: TUIApp) -> dict | None:
        """Apply locale overrides to the cached scenario bundle."""
        bundle = app.state.scenario_description
        if bundle is None:
            return None
        locale = app.state.locale
        if locale == "en":
            return bundle
        from silicon_pantheon.client.locale.scenario import localize_scenario
        return localize_scenario(bundle, locale)

    # ---- render ----

    def render(self) -> RenderableType:
        if self._confirm is not None:
            return self._confirm.render()
        if self._scenario_overlay is not None:
            inner = self._scenario_overlay.render(focused=True)
            from silicon_pantheon.client.locale import t as _t
            footer = Text(
                _t("game_overlay.footer", self.app.state.locale),
                style="dim",
            )
            root = Layout()
            root.split_column(
                Layout(name="body"),
                Layout(name="ftr", size=1),
            )
            # No Align.center — the Panel must fill the body slot so
            # scrolling doesn't cause the panel to shrink with the
            # visible-row count. Align was the visible-shrinking bug
            # the user hit when holding `j`.
            root["body"].update(inner)
            root["ftr"].update(footer)
            return root
        gs = self.state or {}
        scenario = (gs.get("rules") or {}).get("scenario") or (
            self.app.state.last_room_state or {}
        ).get("scenario", "?")
        header_line = Text()
        header_line.append(scenario, style="yellow bold")

        if self.app.state.error_message:
            footer_line: RenderableType = Text(
                self.app.state.error_message, style="red"
            )
        else:
            focused = self._panels[self._focus_idx]
            hints = Text()
            panel_hints = focused.key_hints()
            if panel_hints:
                hints.append(f"[{focused.title}] ", style="bold yellow")
                hints.append(panel_hints, style="white")
                hints.append("   ", style="dim")
            from silicon_pantheon.client.locale import t as _t
            lc = self.app.state.locale
            hints.append(
                f"{_t('keys.tab_next', lc)}   {_t('keys.range', lc)}   "
                f"{_t('keys.help', lc)}   {_t('keys.scenario', lc)}   "
                f"{_t('keys.quit', lc)}",
                style="dim",
            )
            footer_line = hints

        root = Layout()
        root.split_column(
            Layout(name="hdr", size=1),
            Layout(name="body"),
            Layout(name="ftr", size=1),
        )
        root["hdr"].update(header_line)
        root["body"].update(self._build_body())
        root["ftr"].update(footer_line)
        return root

    def _build_body(self) -> Layout:
        body = Layout()
        body.split_column(
            Layout(name="top", ratio=3),
            Layout(name="bottom", ratio=2),
        )
        body["top"].split_row(
            Layout(name="map", ratio=2),
            Layout(name="player", ratio=1),
        )
        body["bottom"].split_row(
            Layout(name="reasoning", ratio=2),
            Layout(name="coach", ratio=1),
        )

        focused = self._panels[self._focus_idx]
        body["top"]["map"].update(self.map_panel.render(focused is self.map_panel))
        body["top"]["player"].update(
            self._panels[1].render(focused is self._panels[1])
        )
        body["bottom"]["reasoning"].update(
            self.reasoning_panel.render(focused is self.reasoning_panel)
        )
        body["bottom"]["coach"].update(
            self.coach_panel.render(focused is self.coach_panel)
        )
        return body

    # ---- input ----

    async def handle_key(self, key: str) -> Screen | None:
        if self._confirm is not None:
            close = await self._confirm.handle_key(key)
            if close:
                self._confirm = None
            return None
        # Scenario overlay: F3 toggles. While open, Esc/q/F3 close,
        # everything else (j/k/G/gg/^f/^b/^d/^u) routes to the panel
        # for vim-style scrolling. The underlying game state keeps
        # refreshing in the background.
        if self._scenario_overlay is not None:
            if key in ("f3", "escape", "q"):
                self._scenario_overlay = None
                return None
            await self._scenario_overlay.handle_key(key)
            return None
        if key == "f3":
            from silicon_pantheon.client.tui.screens.room import (
                DescriptionPanel as _DescriptionPanel,
            )

            self._scenario_overlay = _DescriptionPanel(self.app)
            return None
        # Range overlay toggle: `r` shows/hides move + attack range
        # for the highlighted unit. Works from Map or Player panel.
        if key == "r" and self.highlighted_unit_id:
            if self.range_overlay_unit == self.highlighted_unit_id:
                # Already showing this unit's range → dismiss.
                self._clear_range_overlay()
            else:
                # Fetch range from server and cache.
                import asyncio as _asyncio

                _asyncio.create_task(self._fetch_range_overlay(self.highlighted_unit_id))
            return None
        # When the Coach panel is focused, the buffer captures everything
        # so users can type 'q' / 'tab' / etc. into a message.
        coach_focused = self._panels[self._focus_idx] is self.coach_panel
        if coach_focused and key not in ("\t",):
            return await self.coach_panel.handle_key(key)
        # Tab from the coach panel only exits if the buffer is empty.
        if coach_focused and key == "\t" and self.coach_panel.buffer:
            return None

        # Unit card: dismiss on Esc / Enter / q from ANY focused panel.
        # The card renders inside the Map panel, but the user might
        # have opened it from the Player panel's cursor-Enter — keys
        # still route to the Player panel, so without this global
        # intercept the card would be stuck until Tab→Map→Esc.
        if self.unit_card is not None:
            if key in ("escape", "esc", "enter", "q"):
                # Snap map cursor to the card's unit position on close.
                pos = self.unit_card.unit.get("pos") or {}
                self.map_panel.cx = int(pos.get("x", self.map_panel.cx))
                self.map_panel.cy = int(pos.get("y", self.map_panel.cy))
                self.unit_card = None
                return None
            if key in ("left", "h"):
                self.unit_card.navigate(-1)
                return None
            if key in ("right", "l"):
                self.unit_card.navigate(1)
                return None
            # Swallow all other keys while card is up so they don't
            # accidentally trigger game actions underneath.
            return None

        # Global quit — but not when a unit card is open (handled
        # above). Route through the same ConfirmModal the Quit button
        # uses so q is consistent with the button (quitting an
        # in-progress match without confirmation was a footgun,
        # especially mid-turn).
        if key == "q" and self.unit_card is None:
            async def _quit(yes: bool) -> None:
                if yes:
                    self.app.exit()

            from silicon_pantheon.client.locale import t as _t
            self._confirm = ConfirmModal(
                prompt=_t("game_quit.confirm", self.app.state.locale),
                on_confirm=_quit,
                locale=self.app.state.locale,
            )
            return None
        if key == "\t":
            self.unit_card = None
            self._focus_next(1)
            return None
        return await self._panels[self._focus_idx].handle_key(key)

    def _focus_next(self, step: int) -> None:
        n = len(self._panels)
        if n == 0:
            return
        i = self._focus_idx
        for _ in range(n):
            i = (i + step) % n
            if self._panels[i].can_focus():
                self._focus_idx = i
                # Clear cross-highlight when focus moves to a panel
                # that doesn't participate (reasoning, coach). Map +
                # Player drive the highlight; others don't.
                new_panel = self._panels[i]
                if new_panel is not self.map_panel and not isinstance(
                    new_panel, PlayerPanel
                ):
                    self.highlighted_unit_id = None
                return

    # ---- public API used by panels ----

    def open_unit_card(self, unit: dict[str, Any]) -> None:
        gs = self.state or {}
        units = [u for u in (gs.get("units") or []) if u.get("alive", u.get("hp", 0) > 0)]
        units.sort(
            key=lambda u: (
                int((u.get("pos") or {}).get("y", 0)),
                int((u.get("pos") or {}).get("x", 0)),
            )
        )
        try:
            idx = units.index(unit)
        except ValueError:
            idx = 0
            units = [unit] + units
        unit_classes = (
            self.app.state.scenario_description or {}
        ).get("unit_classes") or {}
        self.unit_card = UnitCard(units=units, index=idx, unit_classes=unit_classes, locale=self.app.state.locale)

    def _clear_range_overlay(self) -> None:
        self.range_overlay_unit = None
        self.range_move_tiles = set()
        self.range_attack_tiles = set()

    async def _fetch_range_overlay(self, unit_id: str) -> None:
        if self.app.client is None:
            return
        try:
            r = await self.app.client.call("get_unit_range", unit_id=unit_id)
        except Exception:
            return
        if not r.get("ok"):
            return
        result = r.get("result") or {}
        self.range_overlay_unit = unit_id
        self.range_move_tiles = {
            (t["x"], t["y"]) for t in result.get("move_tiles") or []
        }
        self.range_attack_tiles = {
            (t["x"], t["y"]) for t in result.get("attack_tiles") or []
        }

    async def send_coach_message(self, text: str) -> None:
        gs = self.state or {}
        my_team = gs.get("you")
        if not my_team or self.app.client is None:
            return
        try:
            r = await self.app.client.call(
                "send_to_agent", team=my_team, text=text
            )
        except Exception as e:
            log.exception("send_to_agent raised")
            self.app.state.error_message = f"send_to_agent failed: {e}"
            return
        if r.get("ok"):
            self.app.state.error_message = ""
        else:
            self.app.state.error_message = r.get("error", {}).get(
                "message", "send_to_agent rejected"
            )

    # ---- server interactions ----

    async def tick(self) -> None:
        import time

        now = time.time()
        if now - self._last_poll >= POLL_INTERVAL_S:
            await self._refresh_state()

    async def _refresh_state(self) -> Screen | None:
        import time

        self._last_poll = time.time()
        if self.app.client is None:
            return None
        try:
            r = await self.app.client.call("get_state")
        except Exception as e:
            self.app.state.error_message = f"get_state failed: {e}"
            return None
        if not r.get("ok"):
            self.app.state.error_message = r.get("error", {}).get(
                "message", "get_state rejected"
            )
            return None
        self.app.state.error_message = ""
        self.state = r.get("result", {})
        self.app.state.last_game_state = self.state

        # Update per-unit last-action annotation incrementally from
        # the polled state's last_action field. No get_history call
        # needed — we just track each new action as it appears on
        # every 1s poll. Only meaningful actions (move / attack /
        # heal) are cached; "wait" and "end_turn" are not informational.
        #
        # Clear the cache when a new turn starts (active_player
        # flipped). Units are back to READY — showing stale actions
        # from the previous turn is confusing ("it says moved but
        # the unit is in ready state").
        la = self.state.get("last_action")
        if la is not None and la.get("type") == "end_turn":
            # end_turn = turn boundary. Clear all annotations so the
            # new turn starts clean.
            if la is not self._last_action_seen:
                self.unit_last_actions.clear()
        if la is not None and la is not self._last_action_seen:
            self._last_action_seen = la
            uid = la.get("unit_id") or la.get("healer_id")
            if uid:
                from silicon_pantheon.client.locale import t as _lt
                _alc = self.app.state.locale
                la_type = la.get("type")
                if la_type == "move":
                    dest = la.get("dest") or {}
                    self.unit_last_actions[uid] = (
                        _lt("action.moved", _alc)
                        .replace("{x}", str(dest.get("x", "?")))
                        .replace("{y}", str(dest.get("y", "?")))
                    )
                elif la_type == "attack":
                    tid = la.get("target_id", "?")
                    dmg = la.get("damage_dealt", "?")
                    killed = f" {_lt('action.killed', _alc)}" if la.get("target_killed") else ""
                    self.unit_last_actions[uid] = (
                        _lt("action.atk", _alc)
                        .replace("{tid}", str(tid))
                        .replace("{dmg}", str(dmg))
                        + killed
                    )
                elif la_type == "heal":
                    tid = la.get("target_id", "?")
                    amt = la.get("heal_amount", la.get("healed", "?"))
                    self.unit_last_actions[uid] = (
                        _lt("action.healed", _alc)
                        .replace("{tid}", str(tid))
                        .replace("{amt}", str(amt))
                    )
                # "wait" and "end_turn" are intentionally skipped.

        await self._maybe_trigger_agent()

        if self.state.get("status") == "game_over":
            from silicon_pantheon.client.tui.screens.post_match import PostMatchScreen

            next_screen = PostMatchScreen(self.app)
            await self.app.transition(next_screen)
            return next_screen
        return None

    async def _maybe_trigger_agent(self) -> None:
        # Each early return gets a WHY log so the Q3 "blue just stops
        # firing play_turn" mystery is diagnosable from the log alone.
        # We sample the log (don't spam every tick) so the file stays
        # readable — emit once every ~30 calls that return early for
        # the same reason.
        if self.app.state.agent is None:
            self._log_trigger_skip("agent_is_none")
            return
        if (
            self.app.state.agent_task is not None
            and not self.app.state.agent_task.done()
        ):
            self._log_trigger_skip("task_running")
            return
        gs = self.state or {}
        if gs.get("status") == "game_over":
            self._log_trigger_skip("game_over")
            return
        my_team = gs.get("you")
        active = gs.get("active_player")
        if not my_team or active != my_team:
            self._log_trigger_skip(
                f"not_my_turn(me={my_team}, active={active})"
            )
            return
        # Reset skip-counter so the NEXT idle period's log is clean.
        self._trigger_skip_reason = ""
        self._trigger_skip_count = 0
        log.info(
            "triggering agent.play_turn for team=%s turn=%s",
            my_team, gs.get("turn"),
        )

        from silicon_pantheon.server.engine.state import Team

        viewer = Team.BLUE if my_team == "blue" else Team.RED
        max_turns = int(
            gs.get("max_turns")
            or (gs.get("rules", {}) or {}).get("max_turns")
            or 20
        )

        async def _run() -> None:
            import time as _time

            from silicon_pantheon.client.providers.errors import (
                ProviderError,
                ProviderErrorReason,
            )

            t0 = _time.time()
            log.info("agent_task START team=%s", my_team)
            try:
                await self.app.state.agent.play_turn(viewer, max_turns=max_turns)
                log.info(
                    "agent_task END team=%s dt=%.1fs (clean)",
                    my_team, _time.time() - t0,
                )
            except asyncio.CancelledError:
                log.info(
                    "agent_task CANCELLED team=%s dt=%.1fs",
                    my_team, _time.time() - t0,
                )
                return
            except ProviderError as e:
                log.warning(
                    "agent_task END team=%s dt=%.1fs (provider error: %s)",
                    my_team, _time.time() - t0, e,
                )
                if e.is_terminal:
                    self.app.state.error_message = (
                        f"{e.reason.value}: {e} — conceding match"
                    )
                    try:
                        await self._call("concede")
                    except Exception:
                        log.exception("concede-after-provider-error raised")
                elif e.reason == ProviderErrorReason.RATE_LIMIT:
                    self.app.state.error_message = (
                        "rate-limited — retrying on next poll"
                    )
                else:
                    self.app.state.error_message = f"agent error: {e}"
            except Exception as e:
                log.exception(
                    "agent_task END team=%s dt=%.1fs (exception): %s",
                    my_team, _time.time() - t0, e,
                )
                self.app.state.error_message = f"agent error: {e}"

        self.app.state.agent_task = asyncio.create_task(_run())

    async def _call(self, tool: str) -> Screen | None:
        if self.app.client is None:
            return None
        try:
            r = await self.app.client.call(tool)
        except Exception as e:
            self.app.state.error_message = f"{tool} failed: {e}"
            return None
        if not r.get("ok"):
            self.app.state.error_message = r.get("error", {}).get(
                "message", f"{tool} rejected"
            )
        else:
            self.app.state.error_message = ""
        await self._refresh_state()
        return None
