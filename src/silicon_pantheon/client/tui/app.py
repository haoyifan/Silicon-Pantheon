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
Key returns from the reader mirror `silicon-play`'s:
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

_log = logging.getLogger("silicon.tui.app")

from rich.align import Align
from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.panel import Panel as RichPanel
from rich.text import Text

from silicon_pantheon.client.transport import ServerClient

if TYPE_CHECKING:
    from silicon_pantheon.client.agent_bridge import NetworkedAgent

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

    server_url: str = "https://game.siliconpantheon.com/mcp/"
    locale: str = "en"  # set at login; threads through to TUI + prompts
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
    # Selected lesson files to inject into the agent's system prompt.
    # Empty list = no lessons (agent starts cold). None = auto (last 5).
    # Set from the RoomScreen Actions panel lesson picker.
    selected_lessons: list[Path] = field(default_factory=list)
    # Whether to save new lessons after the match.
    save_lessons: bool = True
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
        # Raw key before lowercasing — text-input panels (coach,
        # API-key paste) read this to preserve capital letters,
        # question marks, and other shifted characters.
        self._raw_key: str = ""
        # Help overlay state. App-level so it works on every screen
        # without each one having to wire it. The overlay is purely
        # client-side: tick / poll / agent loops keep running while
        # it's open, so the heartbeat survives and the agent (if any)
        # can still take its turn.
        self._help_visible = False
        self._help_scroll = 0
        self._help_gg = False

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
        # Enable bracketed-paste mode on the terminal. With this on,
        # pasting wraps the content in ESC[200~ ... ESC[201~ so the
        # reader can treat it atomically (see _read_key_blocking).
        # Without it, Terminal.app / iTerm / most Linux terminals just
        # dump the pasted chars as if they were typed one at a time —
        # which triggers global shortcuts mid-paste (e.g. 'q' → quit).
        _enable_bracketed_paste()
        try:
            with Live(
                self._render_with_overlay(),
                console=self.console,
                refresh_per_second=1,
                screen=True,
                auto_refresh=False,
            ) as live:
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
        finally:
            _disable_bracketed_paste()
            _restore_terminal()
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
        return _HelpOverlay(self._help_scroll, self.state.locale).render()

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
                raw_key = await self._key_queue.get()
                if self._screen is None:
                    continue
                # Normalize for navigation (j/k/q/etc.) but preserve
                # the raw key for text-input screens (coach panel,
                # API-key paste). Single printable chars get lowercased
                # for navigation matching; multi-char tokens (escape,
                # ctrl-d, f3, etc.) stay as-is. The raw_key is stored
                # on the app so text-input handlers can read it.
                self._raw_key = raw_key
                key = raw_key.lower() if len(raw_key) == 1 else raw_key
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
                        raw_next = self._key_queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    self._raw_key = raw_next
                    next_key = raw_next.lower() if len(raw_next) == 1 else raw_next
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

        F2 toggles. While the overlay is up, full vim-style scroll
        keys are accepted (j/k, ^f/^b, ^d/^u, gg, G, home/end,
        pgup/pgdown). Esc/q/F2 dismiss; everything else is swallowed
        so it doesn't leak to the underlying screen and accidentally
        end a turn."""
        if key in ("f2", "?"):
            self._help_visible = not self._help_visible
            self._help_scroll = 0
            self._help_gg = False
            return True
        if not self._help_visible:
            return False
        if key in ("esc", "q"):
            self._help_visible = False
            self._help_scroll = 0
            self._help_gg = False
            return True
        from silicon_pantheon.client.tui.panels import apply_vim_scroll

        # Stash gg latch as a list so apply_vim_scroll can mutate it.
        gg_state = [getattr(self, "_help_gg", False)]
        nxt = apply_vim_scroll(
            key, current=self._help_scroll, gg_state=gg_state
        )
        self._help_gg = gg_state[0]
        if nxt is not None:
            self._help_scroll = nxt
            return True
        # Spacebar is the classic page-down for help-style overlays;
        # keep that compatibility.
        if key == " ":
            self._help_scroll += 12
            return True
        # While help is open, swallow everything else so 'e', 'x', etc.
        # don't reach the game screen and end the turn / concede.
        return True

    async def _ticker(self) -> None:
        # Heartbeat so we can see in the log whether this task is alive.
        # The Q3 "blue just stops" mystery had no exception, no transport
        # error, but no more tick() activity either. Without a heartbeat
        # we can't tell whether the ticker was cancelled silently, is
        # stuck inside an await that never returns, or something else.
        # Emit a pulse every ~30s and log each tick() call's duration
        # so a hang shows up as "pulse fires but no tick start"
        # or "tick start with no tick end".
        import time as _time

        _tick_n = 0
        _last_pulse = _time.time()
        try:
            while not self._should_exit:
                await asyncio.sleep(TICK_INTERVAL_S)
                if self._screen is None:
                    continue
                _tick_n += 1
                now = _time.time()
                if now - _last_pulse >= 30.0:
                    _log.info(
                        "ticker pulse: tick_n=%d screen=%s should_exit=%s",
                        _tick_n, type(self._screen).__name__,
                        self._should_exit,
                    )
                    _last_pulse = now
                t0 = _time.time()
                try:
                    await self._screen.tick()
                except Exception as e:
                    _log.exception("tick raised")
                    self.state.error_message = f"tick error: {e}"
                dt = _time.time() - t0
                # Flag abnormally slow ticks — anything over 5s is
                # suspicious (we make a single get_state at 1s poll
                # cadence; the rest is cheap). Hangs show up as dt
                # large or the next tick never arriving.
                if dt > 5.0:
                    _log.warning(
                        "slow tick: tick_n=%d screen=%s dt=%.1fs",
                        _tick_n, type(self._screen).__name__, dt,
                    )
                self._refresh()
        except asyncio.CancelledError:
            _log.info("ticker cancelled: tick_n=%d", _tick_n)
            return
        except BaseException:
            # Normally impossible, but log before propagating so the
            # next hang repro captures it.
            _log.exception("ticker died")
            raise


# ---- key-reading helper (POSIX cbreak) ----


def _enable_bracketed_paste() -> None:
    """Turn on DEC mode 2004 so pastes arrive wrapped in ESC[200~/[201~."""
    try:
        if sys.stdout.isatty():
            sys.stdout.write("\x1b[?2004h")
            sys.stdout.flush()
    except Exception:
        pass


def _disable_bracketed_paste() -> None:
    try:
        if sys.stdout.isatty():
            sys.stdout.write("\x1b[?2004l")
            sys.stdout.flush()
    except Exception:
        pass


def _restore_terminal() -> None:
    """Restore terminal to cooked mode on exit.

    We set cbreak once in _read_key_blocking and never restore it
    during the app's lifetime. This function restores the saved
    settings so the shell works normally after the TUI exits.
    """
    saved = getattr(_read_key_blocking, "_original_termios", None)
    if saved is not None:
        try:
            import termios
            fd = sys.stdin.fileno()
            termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        except Exception:
            pass
    # Also re-show cursor and reset terminal.
    try:
        if sys.stdout.isatty():
            sys.stdout.write("\033[?25h")  # show cursor
            sys.stdout.flush()
    except Exception:
        pass


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

    def _read_char() -> str:
        """Blocking single-character read, bypassing Python's TextIOWrapper.

        Reads the raw fd one byte at a time and handles multi-byte
        UTF-8 sequences (Chinese, emoji, etc.) by inspecting the lead
        byte to determine how many continuation bytes to expect.
        """
        try:
            b = os.read(fd, 1)
        except OSError:
            return ""
        if not b:
            return ""
        # Determine how many bytes this UTF-8 character needs.
        lead = b[0]
        if lead < 0x80:
            return b.decode("utf-8")  # ASCII
        if lead < 0xC0:
            return b.decode("utf-8", errors="replace")  # stray continuation
        if lead < 0xE0:
            n_more = 1  # 2-byte char
        elif lead < 0xF0:
            n_more = 2  # 3-byte char (CJK, most Chinese)
        else:
            n_more = 3  # 4-byte char (emoji, rare CJK)
        for _ in range(n_more):
            try:
                more = os.read(fd, 1)
            except OSError:
                break
            if not more:
                break
            b += more
        return b.decode("utf-8", errors="replace")

    def _peek(timeout: float) -> str:
        r, _, _ = select.select([fd], [], [], timeout)
        return _read_char() if r else ""

    # Set cbreak once, not every call. tty.setcbreak uses TCSAFLUSH
    # by default which flushes the input buffer — if the IME sent
    # multiple characters and we only read the first, the second
    # would be flushed on the next call. Save the original settings
    # so we can restore on exit.
    if not getattr(_read_key_blocking, "_cbreak_set", False):
        try:
            _read_key_blocking._original_termios = old  # type: ignore[attr-defined]
            tty.setcbreak(fd, termios.TCSANOW)  # TCSANOW = no flush
        except termios.error:
            return "q"
        _read_key_blocking._cbreak_set = True  # type: ignore[attr-defined]
    ch = _read_char()
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
                # Most modern terminals (xterm, iTerm2, Terminal.app,
                # tmux in default mode) send F1-F4 via SS3, not CSI —
                # missing any of these here means the key falls through
                # to "esc". F3's SS3 form was the one the in-game
                # scenario-overlay couldn't open from.
                if c1 == "O" and final in ("P", "Q", "R", "S"):
                    return {"P": "f1", "Q": "f2", "R": "f3", "S": "f4"}[final]
                if c1 == "[" and final == "~":
                    # Bracketed-paste: terminals wrap clipboard paste
                    # with ESC[200~ ... ESC[201~ when we enable mode
                    # ?2004 (TUIApp.run does this on startup). Slurp
                    # everything between the markers into one synthetic
                    # `paste:<content>` key event. Benefits over
                    # letting the bytes flow through as individual keys:
                    #
                    #   1. No per-char .lower() mangling API-key case.
                    #   2. No intermediate char triggers a global
                    #      shortcut mid-paste (e.g. the literal 'q' in
                    #      an xAI key `sk-xai-...q...` was exiting the
                    #      app because provider_auth treats 'q' as
                    #      quit).
                    #   3. The paste arrives atomically, so screens
                    #      can validate / save without racing the
                    #      char stream.
                    if params == "200":
                        buf: list[str] = []
                        # Slurp until we hit the end marker. 60s cap
                        # is there only as a runaway guard; a real
                        # paste completes in milliseconds.
                        import time as _time
                        deadline = _time.monotonic() + 60.0
                        while _time.monotonic() < deadline:
                            nb = _peek(0.5)
                            if not nb:
                                continue
                            if nb == "\x1b":
                                # Look for [201~
                                end_c1 = _peek(0.05)
                                if end_c1 == "[":
                                    end_params = ""
                                    end_final = ""
                                    for _ in range(8):
                                        z = _peek(0.05)
                                        if not z:
                                            break
                                        if z.isalpha() or z == "~":
                                            end_final = z
                                            break
                                        end_params += z
                                    if end_params == "201" and end_final == "~":
                                        return "paste:" + "".join(buf)
                                    # Not the end marker — put it back as literal text.
                                    buf.append("\x1b")
                                    buf.append(end_c1)
                                    buf.append(end_params)
                                    buf.append(end_final)
                                else:
                                    buf.append("\x1b")
                                    if end_c1:
                                        buf.append(end_c1)
                                continue
                            buf.append(nb)
                        return "paste:" + "".join(buf)
                    if params == "201":
                        # Stray end-marker (shouldn't happen). Swallow.
                        return ""
                    if params == "12":
                        return "f2"
                    if params == "11":
                        return "f1"
                    if params == "13":
                        return "f3"
                    if params == "14":
                        return "f4"
                    # Page Up / Page Down (standard CSI).
                    if params == "5":
                        return "pgup"
                    if params == "6":
                        return "pgdown"
                    # Home / End (CSI variants).
                    if params in ("1", "7"):
                        return "home"
                    if params in ("4", "8"):
                        return "end"
            return "esc"

    if ch in ("\r", "\n"):
        return "enter"
    if ch == "\x03":
        return "q"
    if ch == "\x7f":  # backspace
        return "backspace"
    # Ctrl-letter shortcuts (vim-style scrollers). Emit named tokens
    # so screens match on intent rather than raw control bytes. We
    # avoid ctrl-c (0x03 above → "q") and ctrl-j / ctrl-m which
    # collide with enter.
    _CTRL = {
        "\x02": "ctrl-b",  # vim page up
        "\x04": "ctrl-d",  # vim half-page down
        "\x06": "ctrl-f",  # vim page down
        "\x15": "ctrl-u",  # vim half-page up
    }
    if ch in _CTRL:
        return _CTRL[ch]
    # Preserve 'G' (shift-g) as a distinct token so "G = bottom /
    # gg = top" works.
    if ch == "G":
        return "shift-g"
    # DON'T lowercase printable characters — text-input panels
    # (coach, API-key paste) need the raw case and symbols like
    # ?, !, @, capital letters. Navigation handlers use lowercase
    # comparisons (if key in ("j", "k")) which naturally match
    # lowercase input; uppercase input from Caps Lock is rare and
    # handled below.
    #
    # Return the raw char for all printable single characters.
    # Non-printable control chars (already handled above as named
    # tokens like "ctrl-d") don't reach here.
    return ch


# ---- help overlay ----


def _t_help(overlay):
    from silicon_pantheon.client.locale import t
    return t("help.title", getattr(overlay, "locale", "en"))


class _HelpOverlay:
    """Whole-screen scrollable help / tutorial. Rendered on top of
    the current screen; the underlying screen's tick loop keeps
    running so the heartbeat and any agent turns proceed normally."""

    def __init__(self, scroll: int, locale: str = "en") -> None:
        self.scroll = scroll
        self.locale = locale

    def render(self) -> RenderableType:
        from silicon_pantheon.client.locale import t as _tl
        help_body = _tl("help.body", self.locale).rstrip()
        lines = help_body.split("\n")
        if self.scroll > 0:
            self.scroll = min(self.scroll, max(0, len(lines) - 1))
            lines = lines[self.scroll:]
        body = Text("\n".join(lines))
        footer = Text(
            _tl("help_footer", self.locale),
            style="dim",
        )
        return Align.center(
            RichPanel(
                Group(body, Text(""), footer),
                title=_t_help(self),
                border_style="yellow",
                padding=(1, 3),
            ),
            vertical="middle",
        )
