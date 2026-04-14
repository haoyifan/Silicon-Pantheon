"""TUIApp: screen-routing frame + main event loop.

Architecture
------------
- `TUIApp` owns shared state: the ServerClient (once connected), a
  ScreenStack of one Screen at a time, a rich.Console, and a
  rich.Live.
- Each concrete Screen subclass implements `render()` and
  `handle_key()`, optionally `tick()` for periodic refresh.
- The main loop runs three concurrent asyncio tasks:
    (1) key_reader: blocks on stdin in cbreak mode via to_thread,
        pushes keys into an asyncio.Queue.
    (2) ticker: wakes every `tick_interval_s`, calls screen.tick()
        + refreshes the Live.
    (3) dispatcher: pops keys off the queue, calls
        screen.handle_key(); swaps the screen if a new one is returned.
- Screens can schedule MCP tool calls directly via `app.client.call()`
  since we're on the same asyncio loop.

Typing the key literals
-----------------------
Key returns from the reader mirror `clash-play`'s:
  - "enter"   for newline
  - single lowercase characters for printable keys
  - "up" / "down" / "left" / "right" for arrow escape sequences
"""

from __future__ import annotations

import asyncio
import logging
import sys
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, TYPE_CHECKING

_log = logging.getLogger("clash.tui.app")

from rich.align import Align
from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.panel import Panel as RichPanel
from rich.text import Text

from clash_of_odin.client.transport import ServerClient

if TYPE_CHECKING:
    from clash_of_odin.client.agent_bridge import NetworkedAgent

TICK_INTERVAL_S = 0.25
POLL_INTERVAL_S = 1.0  # lobby / room state polling cadence
THOUGHTS_BUFFER_SIZE = 100


class Screen:
    """Base class. Subclasses fill in render / handle_key / tick."""

    def render(self) -> RenderableType:  # noqa: D401
        raise NotImplementedError

    async def handle_key(self, key: str) -> "Screen | None":
        """Return a new screen to transition to, or None to stay."""
        return None

    async def tick(self) -> None:
        """Called every tick. Default: no-op."""
        return None

    async def on_enter(self, app: "TUIApp") -> None:
        """Called once when this screen becomes active."""
        return None

    async def on_exit(self, app: "TUIApp") -> None:
        """Called once before this screen is swapped out."""
        return None


@dataclass
class SharedState:
    """Mutable client-side state shared across screens.

    Backend responses are copied into this dataclass so screens don't
    have to race each other on the transport.
    """

    server_url: str = "http://127.0.0.1:8080/mcp/"
    display_name: str = ""
    kind: str = "ai"
    provider: str | None = None
    model: str | None = None
    connection_id: str | None = None
    room_id: str | None = None
    slot: str | None = None  # "a" | "b"
    last_rooms: list[dict[str, Any]] = field(default_factory=list)
    last_room_state: dict[str, Any] | None = None
    last_game_state: dict[str, Any] | None = None
    status_message: str = ""
    error_message: str = ""
    # Optional path to a pre-written STRATEGY.md; read at game start.
    strategy_path: Path | None = None
    strategy_text: str | None = None
    # Agent bridge for in-game play — populated when GameScreen enters
    # and the player declared themselves as ai/hybrid with a provider+model.
    agent: "NetworkedAgent | None" = None
    agent_task: asyncio.Task | None = None
    # Live reasoning stream from the agent (newest last). Each entry is
    # (timestamp_iso_local, team, text). Team is the player the thought
    # belongs to ('blue' / 'red') so the panel can color the timestamp
    # prefix with the team color — helps when reviewing alternating
    # halves in a shared scrollback.
    thoughts: deque[tuple[str, str, str]] = field(
        default_factory=lambda: deque(maxlen=THOUGHTS_BUFFER_SIZE)
    )
    # Full scenario bundle from the server's describe_scenario tool.
    # Populated when the room screen first fetches the room's scenario
    # so the preview + game-screen legends don't have to refetch.
    scenario_description: dict[str, Any] | None = None


class TUIApp:
    """Single-screen-at-a-time TUI application."""

    def __init__(self, initial_screen_factory: Callable[["TUIApp"], Screen]):
        self.console = Console()
        self.state = SharedState()
        self.client: ServerClient | None = None
        self._screen: Screen | None = None
        self._key_queue: asyncio.Queue[str] = asyncio.Queue()
        self._should_exit = False
        self._live: Live | None = None
        self._initial_factory = initial_screen_factory
        # Help overlay state. App-level so it works on every screen
        # without each one having to wire it. The overlay is purely
        # client-side: tick / poll / agent loops keep running while
        # it's open, so the heartbeat survives and the agent (if any)
        # can still take its turn.
        self._help_visible = False
        self._help_scroll = 0

    # ---- lifecycle ----

    async def run(self) -> int:
        self._screen = self._initial_factory(self)
        await self._screen.on_enter(self)
        # refresh_per_second caps Live's *background* repaint thread
        # when nothing has explicitly asked for a refresh. We pass
        # refresh=True on every update() below so the screen paints
        # immediately on user input; the background cadence is only
        # used by things like art-frame animation (1 frame / 2 s).
        # Keep the background low — a high rate showed a flickering
        # terminal cursor at the bottom-right between frames.
        with Live(
            self._render_with_overlay(),
            console=self.console,
            refresh_per_second=2,
            screen=True,
        ) as live:
            # Pin the cursor off. Live already hides it when it starts
            # but some terminals briefly re-show between screen clears;
            # an explicit show_cursor(False) on the real console keeps
            # it hidden throughout the app's lifetime.
            self.console.show_cursor(False)
            self._live = live
            tasks = [
                asyncio.create_task(self._key_reader()),
                asyncio.create_task(self._ticker()),
                asyncio.create_task(self._dispatcher()),
            ]
            try:
                while not self._should_exit:
                    await asyncio.sleep(0.05)
            finally:
                for t in tasks:
                    t.cancel()
                for t in tasks:
                    try:
                        await t
                    except asyncio.CancelledError:
                        pass
        # Shut down the persistent agent session if one is still alive
        # (user quit mid-match, or skipped post-match).
        if self.state.agent is not None:
            try:
                await self.state.agent.close()
            except Exception:
                pass
            self.state.agent = None
        # Disconnect client cleanly if still connected.
        if self.client is not None:
            try:
                await self.client.stop_heartbeat()
            except Exception:
                pass
        # Tear down the transport context opened by the login screen.
        cleanup = getattr(self, "_transport_cleanup", None)
        if cleanup is not None:
            try:
                await cleanup()
            except Exception:
                pass
        return 0

    def exit(self) -> None:
        self._should_exit = True

    # ---- screen management ----

    async def transition(self, next_screen: Screen) -> None:
        if self._screen is not None:
            try:
                await self._screen.on_exit(self)
            except Exception as e:
                _log.exception("on_exit raised")
                self.state.error_message = f"on_exit error: {e}"
        self._screen = next_screen
        try:
            await self._screen.on_enter(self)
        except Exception as e:
            _log.exception("on_enter raised")
            self.state.error_message = f"on_enter error: {e}"
        self._refresh()

    def _render_with_overlay(self) -> RenderableType:
        """Wrap the screen renderable with the help overlay if open.

        We re-render the screen even when help is up so any
        background animation / poll keeps ticking visibly the moment
        help closes — there's no stale frame to clear."""
        if self._screen is None:
            return Text("")
        base = self._screen.render()
        if not self._help_visible:
            return base
        return _HelpOverlay(self._help_scroll).render()

    def _refresh(self) -> None:
        """Repaint the screen immediately.

        Rich's Live.update() defaults to refresh=False — it just sets
        the next renderable and lets the background thread paint at
        refresh_per_second. With our default 4 Hz that meant ~250 ms
        of input-to-screen lag, which felt awful when holding down an
        arrow key. Forcing refresh=True paints right now; we cap rate
        ourselves by coalescing key events in _dispatcher.
        """
        if self._live is not None and self._screen is not None:
            try:
                self._live.update(self._render_with_overlay(), refresh=True)
            except Exception as e:
                _log.exception("render raised")
                self.state.error_message = f"render error: {e}"

    # ---- input ----

    async def _key_reader(self) -> None:
        """Read single keys in cbreak mode and push onto the queue."""
        try:
            while not self._should_exit:
                key = await asyncio.to_thread(_read_key_blocking)
                if key:
                    await self._key_queue.put(key)
        except asyncio.CancelledError:
            return

    async def _dispatcher(self) -> None:
        """Apply key events; coalesce bursts so we render at most once
        per drained run.

        Holding down an arrow key generates ~30 events/sec. Painting
        the full alternate-screen layout that often is wasted work —
        the user only sees the final frame anyway. So: pop the queue,
        run handle_key, and ONLY refresh when there's no key
        immediately waiting. The background refresh_per_second cap
        guarantees the screen updates even if keys keep arriving.
        """
        try:
            while not self._should_exit:
                key = await self._key_queue.get()
                if self._screen is None:
                    continue
                # Help overlay intercepts before any screen handler.
                if self._handle_help_key(key):
                    self._refresh()
                    continue
                try:
                    nxt = await self._screen.handle_key(key)
                except Exception as e:
                    _log.exception("handle_key raised")
                    self.state.error_message = f"key handler error: {e}"
                    self._refresh()
                    continue
                if nxt is not None:
                    await self.transition(nxt)
                    continue
                # Drain any key that arrived while we were handling
                # this one — apply them all before painting.
                while not self._key_queue.empty():
                    try:
                        next_key = self._key_queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    if self._handle_help_key(next_key):
                        continue
                    try:
                        nxt = await self._screen.handle_key(next_key)
                    except Exception as e:
                        _log.exception("handle_key raised (drain)")
                        self.state.error_message = f"key handler error: {e}"
                        nxt = None
                    if nxt is not None:
                        await self.transition(nxt)
                        nxt = None
                        break
                self._refresh()
        except asyncio.CancelledError:
            return

    def _handle_help_key(self, key: str) -> bool:
        """Toggle / scroll the help overlay. Returns True if the key
        was consumed.

        F2 toggles. While the overlay is up, ↑/↓/k/j scroll, Esc/q/F2
        dismiss; everything else is swallowed so it doesn't leak to
        the underlying screen and accidentally end a turn."""
        if key in ("f2", "?"):
            self._help_visible = not self._help_visible
            self._help_scroll = 0
            return True
        if not self._help_visible:
            return False
        if key in ("esc", "q"):
            self._help_visible = False
            self._help_scroll = 0
            return True
        if key in ("up", "k"):
            self._help_scroll = max(0, self._help_scroll - 1)
            return True
        if key in ("down", "j"):
            self._help_scroll += 1
            return True
        if key == "pgup":
            self._help_scroll = max(0, self._help_scroll - 10)
            return True
        if key in ("pgdn", " "):
            self._help_scroll += 10
            return True
        # While help is open, swallow everything else so 'e', 'x', etc.
        # don't reach the game screen and end the turn / concede.
        return True

    async def _ticker(self) -> None:
        try:
            while not self._should_exit:
                await asyncio.sleep(TICK_INTERVAL_S)
                if self._screen is None:
                    continue
                try:
                    await self._screen.tick()
                except Exception as e:
                    _log.exception("tick raised")
                    self.state.error_message = f"tick error: {e}"
                self._refresh()
        except asyncio.CancelledError:
            return


# ---- key-reading helper (POSIX cbreak) ----


def _read_key_blocking() -> str:
    """Blocking one-key read. Returns a normalized key token.

    ESC-prefixed sequences (arrow keys) map to "up" / "down" / "left" /
    "right". Newline/Carriage-return → "enter". Ctrl-C raises
    KeyboardInterrupt in the runner so the app can exit.
    """
    try:
        import termios
        import tty
    except ImportError:
        # Non-POSIX fallback: line-buffered input.
        try:
            return sys.stdin.readline().strip().lower() or "enter"
        except Exception:
            return "q"

    if not sys.stdin.isatty():
        line = sys.stdin.readline()
        if not line:
            return "q"
        return line.strip().lower() or "enter"

    import os
    import select

    fd = sys.stdin.fileno()
    try:
        old = termios.tcgetattr(fd)
    except termios.error:
        return "q"

    def _read_byte() -> str:
        """Blocking single-byte read, bypassing Python's TextIOWrapper.

        sys.stdin.read(1) goes through a buffered text wrapper that may
        slurp the trailing bytes of an escape sequence into Python's
        internal buffer, after which select() on the kernel fd reports
        nothing readable and our peek loop gives up. os.read on the
        raw fd doesn't have that buffer, so each byte is observed
        exactly when the kernel makes it available.
        """
        try:
            b = os.read(fd, 1)
        except OSError:
            return ""
        return b.decode("utf-8", errors="replace") if b else ""

    def _peek(timeout: float) -> str:
        r, _, _ = select.select([fd], [], [], timeout)
        return _read_byte() if r else ""

    try:
        tty.setcbreak(fd)
        ch = _read_byte()
        if not ch:
            return "q"
        if ch == "\x1b":
            # Escape sequence: drain follow-up bytes with a short
            # per-byte timeout so unmodified ESC reads still work.
            c1 = _peek(0.05)
            if not c1:
                return "esc"
            # CSI ('[') or SS3 ('O') introducer. Both can carry
            # parameters between the introducer and a final letter
            # (e.g. ESC [ 1 ; 5 A for Ctrl+Up). Drain until the final
            # byte (a letter or '~') so leftover parameter bytes don't
            # bleed into the next keypress and look like stray input.
            if c1 in ("[", "O"):
                # Accumulate the parameter bytes too — F-keys can come
                # in either form (ESC O Q for F2, or ESC [ 12~ for the
                # CSI variant) and we need the parameters to tell them
                # apart from arrow keys.
                params = ""
                final = ""
                for _ in range(16):
                    nxt = _peek(0.02)
                    if not nxt:
                        break
                    if nxt.isalpha() or nxt == "~":
                        final = nxt
                        break
                    params += nxt
                # Arrow keys (no params, single-letter final).
                if final == "A" and not params:
                    return "up"
                if final == "B" and not params:
                    return "down"
                if final == "C" and not params:
                    return "right"
                if final == "D" and not params:
                    return "left"
                # Function keys. SS3 form: ESC O P/Q/R/S = F1-F4.
                # CSI form: ESC [ 11~ ... 15~ = F1-F5; 17~..21~ = F6-F10.
                if c1 == "O" and final == "Q":
                    return "f2"
                if c1 == "[" and final == "~":
                    if params == "12":
                        return "f2"
                    if params == "11":
                        return "f1"
                    if params == "13":
                        return "f3"
                    if params == "14":
                        return "f4"
            return "esc"
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)

    if ch in ("\r", "\n"):
        return "enter"
    if ch == "\x03":
        return "q"
    if ch == "\x7f":  # backspace
        return "backspace"
    return ch.lower()


# ---- help overlay ----


HELP_TEXT = """\
Welcome to Clash of Odin
========================

Tactical grid combat played by AI agents. You watch (and optionally
coach) while two LLM-driven players move units across a board to
satisfy the scenario's win conditions.

This help screen never blocks the game — turns, polls, and the
agent (if you're hosting one) keep running while you read.

Navigation
----------

  Tab            cycle focus through the panels in this screen
  ↑ ↓ ← →        move within the focused panel (cursor / scroll /
                 button select / typing)
  h j k l        same as ← ↓ ↑ → (vim bindings)
  Enter          activate the focused thing (button / unit on map /
                 scenario in picker)
  Esc            close a modal / dropdown / unit card
  q              quit (or close a unit card)
  F2 / ?         toggle this help overlay
  Esc            (when help is open) close it

Each panel shows its own keys in the footer when focused —
"[PanelName] panel-specific keys   Tab next panel   q quit".

The Map panel
-------------

When focused, the Map panel shows a tile cursor. Move it with the
arrows (or h/j/k/l). The footer reports the tile's terrain type,
movement cost, defense bonus, and any HP-affecting effects.

Press Enter on a unit's tile to open its stat card. Inside the
card:

  ← / h         previous unit
  → / l         next unit
  Esc / Enter   close (cursor lands on the displayed unit)

The card cycles through the unit's ASCII portrait frames if the
scenario shipped any (1 frame every 2 seconds).

Reading the unit stats
----------------------

  HP            current hit points / maximum
  ATK           attack value (versus enemy DEF for physical units,
                or enemy RES for magical ones)
  DEF           defense versus physical attacks
  RES           defense versus magical attacks
  SPD           speed — affects who counter-attacks (faster
                attackers may double-strike)
  MOVE          how far the unit can travel in one turn
  RANGE         min–max attack distance
  type=magic    attack uses RES instead of DEF
  can_heal      may heal an adjacent ally instead of attacking
  tags          flavor / future-mechanics labels
                (vip, monk, demon, mount, …)

Reading the map
---------------

  K A C M       built-in classes: Knight / Archer / Cavalry / Mage
                Uppercase = blue team, lowercase = red team.
  *             fort tile (seizing the enemy fort wins by default)
  f             forest (cover, +DEF, costs 2 to enter)
  ^             mountain (high cover, most classes can't enter)
  .             plain
  custom glyphs scenarios may define their own letter + color
                per class (e.g. T = Tang Monk in Journey to the West)

How a turn plays out
--------------------

The active player's units act one at a time:

  1. Move:    pick a destination within MOVE range, paying the
              terrain's move_cost on each tile entered.
  2. Action:  attack an enemy in RANGE, heal an adjacent ally
              (Mage / scenario healers only), or wait.
  3. End turn: hand over to the opponent.

A defender that survives an attack and is in range may
counter-attack in the same exchange.

Win conditions
--------------

Each scenario lists its own. Built-ins:

  Either side wins by moving onto the opponent's fort.
  Either side wins by eliminating every enemy unit.
  Match ends in a draw at the turn cap.

Scenarios may add their own:

  protect_unit       a side loses if a specific unit dies
  reach_tile         a side wins if a unit ends a turn on a tile
  hold_tile          a side wins by holding a tile for N turns
  reach_goal_line    a side wins by crossing a row / column

The Description panel always lists the active scenario's actual
rules in plain prose, side-explicit ("Red wins if Tang Monk dies").

Coaching the agent (game screen)
--------------------------------

Tab to the Coach panel and just type. Enter sends the message to
your own agent's coach queue; it'll see your message at the start
of its next turn and may incorporate the advice.

Press Esc to clear the buffer; Tab leaves the panel only if the
buffer is empty (so you can't accidentally cycle away mid-message).

Press F2 again or Esc to close this help."""


class _HelpOverlay:
    """Whole-screen scrollable help / tutorial. Rendered on top of
    the current screen; the underlying screen's tick loop keeps
    running so the heartbeat and any agent turns proceed normally."""

    def __init__(self, scroll: int) -> None:
        self.scroll = scroll

    def render(self) -> RenderableType:
        lines = HELP_TEXT.split("\n")
        if self.scroll > 0:
            self.scroll = min(self.scroll, max(0, len(lines) - 1))
            lines = lines[self.scroll:]
        body = Text("\n".join(lines))
        footer = Text(
            "↑/↓ scroll   PgUp/PgDn page   Esc / F2 close",
            style="dim",
        )
        return Align.center(
            RichPanel(
                Group(body, Text(""), footer),
                title="Help — Clash of Odin",
                border_style="yellow",
                padding=(1, 3),
            ),
            vertical="middle",
        )
