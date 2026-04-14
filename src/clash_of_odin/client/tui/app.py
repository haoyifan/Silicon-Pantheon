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

from rich.console import Console, RenderableType
from rich.live import Live

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

    # ---- lifecycle ----

    async def run(self) -> int:
        self._screen = self._initial_factory(self)
        await self._screen.on_enter(self)
        with Live(
            self._screen.render(),
            console=self.console,
            refresh_per_second=4,
            screen=True,
        ) as live:
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

    def _refresh(self) -> None:
        if self._live is not None and self._screen is not None:
            try:
                self._live.update(self._screen.render())
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
        try:
            while not self._should_exit:
                key = await self._key_queue.get()
                if self._screen is None:
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
                else:
                    self._refresh()
        except asyncio.CancelledError:
            return

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

    fd = sys.stdin.fileno()
    try:
        old = termios.tcgetattr(fd)
    except termios.error:
        return "q"
    try:
        tty.setcbreak(fd)
        ch = sys.stdin.read(1)
        if ch == "\x1b":
            # Escape sequence: read continuation bytes with a short
            # timeout each. Terminals can delay the follow-up bytes
            # of an arrow key (ESC, '[', 'A') by tens of milliseconds;
            # the previous 10 ms window was too tight and the 2nd/3rd
            # bytes were routed to the next _read_key_blocking call,
            # showing up as stray '[' / 'A' characters in the login
            # fields. 50 ms is well below perceptual ESC latency.
            import select

            def _peek(timeout: float) -> str:
                r, _, _ = select.select([sys.stdin], [], [], timeout)
                return sys.stdin.read(1) if r else ""

            c1 = _peek(0.05)
            if not c1:
                return "esc"
            c2 = _peek(0.02)
            seq = c1 + c2
            # Two cursor-key encodings exist and either may show up
            # depending on whether the terminal is in "normal" or
            # "application" keypad mode (DECCKM). Rich's prior output
            # can flip the terminal into application mode, after which
            # arrows arrive as ESC O A instead of ESC [ A — which used
            # to look broken because j/k worked but ↑/↓ did not.
            if seq in ("[A", "OA"):
                return "up"
            if seq in ("[B", "OB"):
                return "down"
            if seq in ("[C", "OC"):
                return "right"
            if seq in ("[D", "OD"):
                return "left"
            return "esc"
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)

    if not ch:
        return "q"
    if ch in ("\r", "\n"):
        return "enter"
    if ch == "\x03":
        return "q"
    if ch == "\x7f":  # backspace
        return "backspace"
    return ch.lower()
