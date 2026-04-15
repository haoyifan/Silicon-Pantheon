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

    Scrollable when focused: scenarios with large rosters
    (Journey to the West has 13 units) don't always fit in the
    available rows, so the user can page through."""

    title = "Player"

    def __init__(self, screen: "GameScreen") -> None:
        self.screen = screen
        self.scroll = 0
        self._gg: list[bool] = [False]

    def key_hints(self) -> str:
        return "j/k ↕   ^f/^b page   ^d/^u ½page   gg top   G bottom"

    async def handle_key(self, key: str) -> "Screen | None":
        from silicon_pantheon.client.tui.panels import apply_vim_scroll

        nxt = apply_vim_scroll(
            key,
            current=self.scroll,
            gg_state=self._gg,
        )
        if nxt is not None:
            self.scroll = nxt
        return None

    def render(self, focused: bool) -> RenderableType:
        gs = self.screen.state or {}
        my_team = gs.get("you") or "?"
        active = gs.get("active_player", "?")
        turn = gs.get("turn", "?")
        max_turns = gs.get("max_turns") or (gs.get("rules") or {}).get("max_turns", "?")
        status = gs.get("status", "?")
        winner = gs.get("winner")

        rows: list[RenderableType] = []
        rows.append(
            Text(
                f"You: {my_team}   Turn {turn}/{max_turns}",
                style="bold cyan" if my_team == "blue" else "bold red",
            )
        )
        my_turn = active == my_team
        rows.append(
            Text(
                "YOUR TURN" if my_turn else "opponent's turn",
                style="bold green" if my_turn else "dim",
            )
        )
        if status == "game_over":
            line = Text("GAME OVER", style="bold yellow")
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
                    f"agent {'thinking…' if busy else 'idle'}",
                    style="yellow" if busy else "dim",
                )
            )
        # Unit roster per team, rendered as a 3-column table:
        # name / HP / status. Dead units stay in the list so the map
        # doesn't silently swallow them — they render dim +
        # strikethrough with the "dead" status in place of the live
        # value. The same status vocabulary the engine uses (ready,
        # moved, done) shows through so the player can see at a
        # glance which units have already acted this turn.
        units = gs.get("units") or []
        scen_desc = self.screen.app.state.scenario_description
        for team in ("blue", "red"):
            team_units = [u for u in units if u.get("owner") == team]
            if not team_units:
                continue
            rows.append(Text(""))
            header_style = "bold cyan" if team == "blue" else "bold red"
            rows.append(Text(f"{team}:", style=header_style))
            # Emit one Text per unit instead of a Rich Table so the
            # panel's line-scroll advances per unit — earlier we wrapped
            # the whole roster in a single Table.grid row, which the
            # scroll logic treated as one atomic item (scroll 1 = lose
            # all units at once). Fixed-column padding reproduces the
            # table look without the atomicity.
            rows.append(
                Text(
                    f"  {'Unit':<14}  {'HP':>7}  Status",
                    style="bold dim",
                )
            )
            for u in team_units:
                alive = u.get("alive", u.get("hp", 0) > 0)
                hp = u.get("hp", "?")
                hp_max = u.get("hp_max", "?")
                name = _unit_display_name(u, scen_desc)
                if alive:
                    status = str(u.get("status", "ready"))
                    hp_str = f"{hp}/{hp_max}"
                    # Per-column styling: neutral name, HP colored by
                    # remaining ratio (green/yellow/red), status
                    # colored by progress. Before the per-column split
                    # the whole row inherited _status_style and the
                    # entire roster rendered green/yellow/dim.
                    row = Text.assemble(
                        ("  ", None),
                        (f"{name[:14]:<14}", "white"),
                        ("  ", None),
                        (f"{hp_str:>7}", _hp_style(hp, hp_max)),
                        ("  ", None),
                        (status, _status_style(status)),
                    )
                    rows.append(row)
                else:
                    # Dead unit: the dim color + ✗ prefix + "dead"
                    # status column together convey the state clearly.
                    # Earlier we wrapped the name in tildes as a
                    # portable strikethrough, but it just looked like
                    # noise once the row was already dim.
                    marker = f"✗ {name[:12]}"
                    hp_str = f"0/{hp_max}"
                    row = Text.assemble(
                        ("  ", None),
                        (f"{marker:<14}", "dim"),
                        ("  ", None),
                        (f"{hp_str:>7}", "dim"),
                        ("  ", None),
                        ("dead", "bold red"),
                    )
                    rows.append(row)
        # Apply scroll by dropping leading rows. Clamp so scrolling
        # past the bottom snaps back to the last full row.
        if self.scroll > 0 and rows:
            self.scroll = min(self.scroll, max(0, len(rows) - 1))
            rows = rows[self.scroll :]
        return RichPanel(
            Group(*rows),
            title=self.title,
            border_style=border_style(focused),
            padding=(0, 1),
        )


# ---- panel: Map (cursor + unit card on Enter) ----


class GameMapPanel(Panel):
    title = "Map"

    def __init__(self, screen: "GameScreen") -> None:
        self.screen = screen
        self.cx = 0
        self.cy = 0

    def key_hints(self) -> str:
        return "←↑↓→ (or h/j/k/l) move   Enter unit stats"

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
                if focused and x == self.cx and y == self.cy:
                    text.append(f"[{g}]", style=f"reverse {st}")
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
        if w == 0 or h == 0:
            return Text("(loading map…)", style="dim italic")
        t = tile_by_pos.get((self.cx, self.cy), {})
        terrain = str(t.get("type", "plain"))
        u = unit_at.get((self.cx, self.cy))
        line = Text()
        line.append(f"({self.cx}, {self.cy}) ", style="dim")
        line.append(f"terrain: {terrain}", style="yellow")
        summary = _terrain_effect_summary(
            self.screen.app.state.scenario_description, terrain
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
            line.append("Enter for details", style="dim italic")
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
            return None
        if key in ("down", "j"):
            self.cy = (self.cy + 1) % h
            return None
        if key in ("left", "h"):
            self.cx = (self.cx - 1) % w
            return None
        if key in ("right", "l"):
            self.cx = (self.cx + 1) % w
            return None
        if key == "enter":
            for u in gs.get("units", []):
                if not u.get("alive", u.get("hp", 0) > 0):
                    continue
                pos = u.get("pos") or {}
                if int(pos.get("x", -1)) == self.cx and int(pos.get("y", -1)) == self.cy:
                    self.screen.open_unit_card(u)
                    break
            return None
        return None


def _terrain_cell(
    ttype: str, scenario_terrain_types: dict | None = None
) -> tuple[str, str]:
    """Pick the (glyph, Rich style) for a terrain cell on the game map.

    Honors scenario-defined terrain types (Troy's troy_wall / xanthus
    / ford / greek_ship, Helm's Deep's deeping_wall, etc.) by looking
    up the scenario_terrain_types map for glyph + color. Without this
    lookup every custom terrain rendered as the catch-all dot, which
    is why Troy's map looked blank.

    Falls back to the built-in defaults for the canonical engine
    terrains (plain / forest / mountain / fort / unknown) so scenarios
    that don't declare a terrain_types block still render correctly.
    """
    if ttype == "unknown":
        return "?", "bright_black"
    spec = (scenario_terrain_types or {}).get(ttype) or {}
    glyph = spec.get("glyph")
    color = spec.get("color")
    if glyph and color:
        return str(glyph)[:1], str(color)
    if ttype == "forest":
        return "f", "green"
    if ttype == "mountain":
        return "^", "bright_black"
    if ttype == "fort":
        return "*", "yellow"
    if glyph:
        return str(glyph)[:1], "dim"
    return ".", "dim"


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

    title = "Agent Reasoning"

    def __init__(self, screen: "GameScreen") -> None:
        self.screen = screen
        # Line offset from the END of the text. 0 = pinned to newest.
        self.line_offset = 0
        self._last_total_lines = 0
        # One-shot latch for the vim `gg` shortcut (press g twice
        # → go to top). Also reset any time a non-g key comes in.
        self._gg_primed = False

    def key_hints(self) -> str:
        return "j/k ↕   ^f/^b page   ^d/^u ½page   G tail   gg top   0 tail"

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

        if total == 0:
            return RichPanel(
                Text("(no reasoning yet)", style="dim italic"),
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
            title = f"{self.title} — live ({end}/{total})"
        else:
            # Surface how many lines are hidden below the user's
            # current view so they know there's new content waiting —
            # pressing 0 returns to live tail. Without this hint the
            # pinned-while-scrolled behaviour is invisible: you'd see
            # the agent's old thought sitting still and have no cue
            # that a newer one landed behind it.
            hidden_below = total - end
            title = (
                f"{self.title} — PAUSED · scrolled  (showing {end}/{total}"
                + (f", {hidden_below} new below — press 0 to resume"
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
    title = "Coach"

    def __init__(self, screen: "GameScreen") -> None:
        self.screen = screen
        self.buffer = ""
        self.history: deque[str] = deque(maxlen=5)

    def key_hints(self) -> str:
        return "type a message   Enter send   Esc clear   Tab leave (empty)"

    def render(self, focused: bool) -> RenderableType:
        rows: list[RenderableType] = []
        if focused:
            prompt = Text(no_wrap=False, overflow="fold")
            prompt.append("> ", style="yellow bold")
            prompt.append(self.buffer, style="white")
            prompt.append("▌", style="yellow")
            rows.append(prompt)
            rows.append(
                Text("Enter send  Esc clear  Tab leave panel", style="dim")
            )
        else:
            rows.append(
                Text(
                    "(Tab here to type a coach message)",
                    style="dim italic",
                )
            )
        if self.history:
            rows.append(Text(""))
            rows.append(Text("recent:", style="dim"))
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
        if len(key) == 1 and key.isprintable():
            self.buffer += key
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
        app.state.agent = NetworkedAgent(
            client=app.client,
            model=app.state.model,
            scenario=scenario,
            strategy=app.state.strategy_text,
            lessons_dir=lessons_dir,
            thoughts_callback=on_thought,
            # Hand over the scenario bundle the room screen already
            # fetched so the agent doesn't need to re-call the server.
            # Falls back to a first-turn fetch if it's missing.
            scenario_description=app.state.scenario_description,
        )

    # ---- render ----

    def render(self) -> RenderableType:
        if self._confirm is not None:
            return self._confirm.render()
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
            hints.append("Tab next panel   F2 help   q quit", style="dim")
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
        # When the Coach panel is focused, the buffer captures everything
        # so users can type 'q' / 'tab' / etc. into a message.
        coach_focused = self._panels[self._focus_idx] is self.coach_panel
        if coach_focused and key not in ("\t",):
            return await self.coach_panel.handle_key(key)
        # Tab from the coach panel only exits if the buffer is empty.
        if coach_focused and key == "\t" and self.coach_panel.buffer:
            return None

        # Global quit — but not when a unit card is open (Esc/Enter/q
        # close the card instead). Route through the same ConfirmModal
        # the Quit button uses so q is consistent with the button
        # (quitting an in-progress match without confirmation was a
        # footgun, especially mid-turn).
        if key == "q" and self.unit_card is None:
            async def _quit(yes: bool) -> None:
                if yes:
                    self.app.exit()

            self._confirm = ConfirmModal(
                prompt="Quit SiliconPantheon?", on_confirm=_quit,
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
        self.unit_card = UnitCard(units=units, index=idx, unit_classes=unit_classes)

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

        await self._maybe_trigger_agent()

        if self.state.get("status") == "game_over":
            from silicon_pantheon.client.tui.screens.post_match import PostMatchScreen

            next_screen = PostMatchScreen(self.app)
            await self.app.transition(next_screen)
            return next_screen
        return None

    async def _maybe_trigger_agent(self) -> None:
        if self.app.state.agent is None:
            return
        if (
            self.app.state.agent_task is not None
            and not self.app.state.agent_task.done()
        ):
            return
        gs = self.state or {}
        if gs.get("status") == "game_over":
            return
        my_team = gs.get("you")
        active = gs.get("active_player")
        if not my_team or active != my_team:
            return
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
            from silicon_pantheon.client.providers.errors import (
                ProviderError,
                ProviderErrorReason,
            )

            try:
                await self.app.state.agent.play_turn(viewer, max_turns=max_turns)
            except asyncio.CancelledError:
                return
            except ProviderError as e:
                log.warning("agent.play_turn provider error: %s", e)
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
                log.exception("agent.play_turn raised: %s", e)
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
