"""In-game screen — server-authoritative state display + agent bridge.

Two sources of game actions:

1. **Agent-driven** (when the player declared themselves as
   `kind in {"ai", "hybrid"}` with provider="anthropic" and a model
   set): GameScreen spawns a NetworkedAgent at on_enter and, on every
   tick, triggers `agent.play_turn()` whenever `active_player` matches
   the viewer's team and no agent task is already running. The agent
   drives move/attack/end_turn directly over the ServerClient; the
   TUI just renders + surfaces reasoning.

2. **Manual** (human, or agent disabled): the player hits keys.

Common keys:
  e   call end_turn
  c   concede
  q   quit
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from typing import Any

from rich.align import Align
from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from clash_of_robots.client.tui.app import POLL_INTERVAL_S, Screen, TUIApp

log = logging.getLogger("clash.tui.game")


class GameScreen(Screen):
    # Total rows reserved for the reasoning panel (including borders + title).
    THOUGHTS_PANEL_HEIGHT = 18

    def __init__(self, app: TUIApp):
        self.app = app
        self._last_poll = 0.0
        self._state: dict[str, Any] | None = None
        # Scroll position for the reasoning panel. 0 = newest thought visible
        # at the bottom; +N = scrolled N thoughts toward older history.
        self._reasoning_offset = 0
        # Snapshot of the thoughts-deque length the last time we rendered.
        # When new thoughts arrive AND the user is at offset=0, we want
        # them to see the new content; when the user has scrolled back,
        # we increase the offset so the panel stays visually stable.
        self._last_thought_count = 0
        # Coach input mode: "normal" for game keys, "coach" for text entry.
        self._input_mode: str = "normal"
        self._coach_buffer: str = ""
        # Ring buffer of recently-sent coach messages so the user can
        # see what they've already said without scrolling.
        self._coach_sent: deque[str] = deque(maxlen=5)

    async def on_enter(self, app: TUIApp) -> None:
        log.info("GameScreen.on_enter: starting")
        await self._refresh_state()
        log.info("GameScreen.on_enter: initial refresh done")
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
        # Note: we intentionally do NOT close `app.state.agent` here.
        # PostMatchScreen needs the live session for summarize_match,
        # and the TUIApp shutdown path (app.run cleanup) will close it
        # if the user never reaches post-match.

    async def _maybe_build_agent(self, app: TUIApp) -> None:
        """Construct a NetworkedAgent if the login declared an LLM."""
        log.info(
            "maybe_build_agent: kind=%s provider=%s model=%s",
            app.state.kind,
            app.state.provider,
            app.state.model,
        )
        if app.state.kind not in ("ai", "hybrid"):
            log.info("maybe_build_agent: skipping, kind not ai/hybrid")
            return
        if app.state.provider != "anthropic" or not app.state.model:
            log.info("maybe_build_agent: skipping, provider not anthropic or no model")
            return
        if app.client is None:
            log.warning("maybe_build_agent: skipping, app.client is None")
            return
        scenario = (app.state.last_room_state or {}).get("scenario") or ""
        if not scenario:
            log.warning(
                "maybe_build_agent: skipping, no scenario (last_room_state=%s)",
                app.state.last_room_state,
            )
            return
        log.info("maybe_build_agent: importing NetworkedAgent")

        from clash_of_robots.client.agent_bridge import NetworkedAgent
        log.info("maybe_build_agent: NetworkedAgent imported")

        async def on_thought(text: str) -> None:
            collapsed = " ".join(text.split())
            if collapsed:
                from datetime import datetime

                ts = datetime.now().strftime("%H:%M:%S")
                app.state.thoughts.append((ts, collapsed))

        app.state.agent = NetworkedAgent(
            client=app.client,
            model=app.state.model,
            scenario=scenario,
            strategy=app.state.strategy_text,
            thoughts_callback=on_thought,
        )
        log.info("maybe_build_agent: NetworkedAgent constructed")

    def render(self) -> RenderableType:
        gs = self._state or {}
        turn = gs.get("turn", "?")
        max_turns = gs.get("max_turns") or gs.get("rules", {}).get("max_turns", "?")
        active = gs.get("active_player", "?")
        status = gs.get("status", "?")
        winner = gs.get("winner")
        you = self.app.state.slot or "?"
        my_team = gs.get("you") or "?"

        header = Text()
        header.append(f"Turn {turn}/{max_turns}   ", style="bold")
        header.append(f"Active: ", style="dim")
        header.append(active, style="cyan bold" if active == "blue" else "red bold")
        header.append("   ", style="")
        my_turn = active == my_team
        header.append(
            "YOUR TURN" if my_turn else "opponent's turn",
            style="bold green" if my_turn else "dim",
        )
        if status == "game_over":
            header.append(f"   GAME OVER", style="bold yellow")
            if winner:
                header.append(
                    f" — winner: {winner}",
                    style="bold green" if winner == my_team else "bold red",
                )

        board = self._render_board(gs)
        units = self._render_units(gs)

        you_info = Text(
            f"You are slot {you} ({my_team}). Fog visible tiles: "
            f"{len(gs.get('_visible_tiles', []))}",
            style="dim",
        )

        if self._input_mode == "coach":
            keys = Text(
                "typing coach message — Enter send   Esc cancel   Backspace delete",
                style="yellow",
            )
        else:
            keys = Text(
                "e end_turn   c coach   k/j scroll reasoning   0 jump-latest   x concede   q quit",
                style="dim",
            )
        status_line = Text("")
        if self.app.state.error_message:
            status_line.append(self.app.state.error_message, style="red")

        thoughts_panel = self._render_thoughts()
        coach_panel = self._render_coach()

        agent_line = Text("")
        if self.app.state.agent is not None:
            busy = (
                self.app.state.agent_task is not None
                and not self.app.state.agent_task.done()
            )
            agent_line.append(
                f"agent: {self.app.state.model} {'[thinking...]' if busy else '[idle]'}",
                style="yellow" if busy else "dim",
            )

        body = Group(
            header,
            Text(""),
            Panel(board, title="board", border_style="dim"),
            Text(""),
            units,
            Text(""),
            thoughts_panel,
            coach_panel,
            Text(""),
            you_info,
            agent_line,
            Text(""),
            keys,
            status_line,
        )
        # No outer frame — the inner Panels (board / reasoning) are
        # enough structure. A full-screen red border was visually
        # noisy and easy to mistake for a danger / error indicator.
        return body

    def _render_thoughts(self) -> RenderableType:
        """Scrollable, word-wrapped panel of agent reasoning.

        Each thought is rendered in full (no ellipsis truncation) with
        word-wrap on; the panel takes a fixed number of rows and we
        pick which slice of thoughts to render based on `_reasoning_offset`.

        Scroll model: offset 0 = newest thought occupies the bottom of
        the panel; increasing offset scrolls toward older history.
        Because wrapped thoughts can occupy multiple rows each, we greedily
        pack thoughts from newest to oldest until the row budget is used,
        then skip `offset` thoughts worth from the tail before rendering.
        """
        thoughts = list(self.app.state.thoughts)
        total = len(thoughts)
        height = self.THOUGHTS_PANEL_HEIGHT
        inner_rows = max(1, height - 2)

        # If the user is scrolled back and new thoughts arrive, keep the
        # *same historical thoughts* pinned in view by bumping the offset
        # by however many new entries landed. Without this, new thoughts
        # would silently push the user's window around.
        new_count = total - self._last_thought_count
        if new_count > 0 and self._reasoning_offset > 0:
            self._reasoning_offset += new_count
        self._last_thought_count = total

        if total == 0:
            body = Text("(no reasoning yet)", style="dim italic")
            return Panel(
                body,
                title="agent reasoning",
                border_style="dim",
                height=height,
            )

        # Clamp offset to valid range so out-of-bound scrolls snap.
        self._reasoning_offset = max(0, min(self._reasoning_offset, total - 1))

        # Approximate visible window: we don't know the terminal width
        # precisely, so assume ~80 chars/row for wrapped length budgeting.
        # Err on the side of packing more thoughts; the Panel auto-crops
        # if we overshoot.
        approx_cols = 80
        window: list[str] = []
        rows_used = 0
        # Walk thoughts from newest to oldest, starting `offset` back.
        end = total - self._reasoning_offset
        for i in range(end - 1, -1, -1):
            ts, t = thoughts[i]
            est_rows = max(
                1, (len(ts) + 3 + len(t) + approx_cols - 1) // approx_cols
            )
            if rows_used + est_rows > inner_rows and window:
                break
            window.append((ts, t))
            rows_used += est_rows
        window.reverse()

        body = Text(no_wrap=False, overflow="fold")
        for i, (ts, t) in enumerate(window):
            body.append(f"[{ts}] ", style="cyan")
            body.append(t)
            if i != len(window) - 1:
                body.append("\n")

        # Position header: indicates where the window is within the full
        # transcript. "latest 5/42" when at offset 0 showing 5 thoughts;
        # "12-16/42" when scrolled back.
        if self._reasoning_offset == 0:
            title = f"agent reasoning — latest {len(window)}/{total}"
        else:
            shown_end = total - self._reasoning_offset
            shown_start = shown_end - len(window) + 1
            title = f"agent reasoning — {shown_start}-{shown_end}/{total} (scrolled {self._reasoning_offset})"
        return Panel(
            body,
            title=title,
            border_style="dim",
            height=height,
        )

    def _render_coach(self) -> RenderableType:
        """Coach input + history panel.

        - When in coach input mode, shows a '> <buffer>_' prompt so the
          user sees exactly what they've typed.
        - Below, a one-line history of the last few sent messages so
          they don't need to remember what they already said.
        - When neither has content (no history, not typing), returns a
          blank Text so the surrounding Group layout stays stable.
        """
        has_history = bool(self._coach_sent)
        typing = self._input_mode == "coach"
        if not (has_history or typing):
            return Text("")

        lines: list[Text] = []
        if typing:
            prompt = Text()
            prompt.append("coach> ", style="yellow bold")
            prompt.append(self._coach_buffer, style="white")
            prompt.append("▌", style="yellow")  # non-blinking cursor
            lines.append(prompt)
        if has_history:
            recent = " · ".join(
                f'"{m}"' for m in list(self._coach_sent)[-3:]
            )
            lines.append(Text(f"recently sent: {recent}", style="dim"))
        body = Group(*lines) if len(lines) > 1 else lines[0]
        return Panel(
            body,
            title=("coach input" if typing else "coach"),
            border_style="yellow" if typing else "dim",
            height=4 if len(lines) > 1 else 3,
        )

    def _render_board(self, gs: dict[str, Any]) -> RenderableType:
        board = gs.get("board", {})
        w = int(board.get("width", 0))
        h = int(board.get("height", 0))
        tiles = board.get("tiles", [])
        units = gs.get("units", [])
        tile_by_pos = {(int(t.get("x", 0)), int(t.get("y", 0))): t for t in tiles}
        unit_by_pos = {
            (int((u.get("pos") or {}).get("x", 0)), int((u.get("pos") or {}).get("y", 0))): u
            for u in units
        }

        text = Text()
        text.append("   " + " ".join(f"{x:>2}" for x in range(w)) + "\n", style="dim")
        glyph_for = {"knight": "K", "archer": "A", "cavalry": "C", "mage": "M"}
        for y in range(h):
            text.append(f"{y:>2} ", style="dim")
            for x in range(w):
                u = unit_by_pos.get((x, y))
                t = tile_by_pos.get((x, y), {})
                if u is not None:
                    glyph = glyph_for.get(u.get("class", ""), "?")
                    if u.get("owner") == "red":
                        glyph = glyph.lower()
                    style = "bold cyan" if u.get("owner") == "blue" else "bold red"
                    text.append(f" {glyph}", style=style)
                else:
                    ttype = t.get("type", "unknown")
                    if ttype == "unknown":
                        text.append(" ?", style="bright_black")
                    elif ttype == "forest":
                        text.append(" f", style="green")
                    elif ttype == "mountain":
                        text.append(" ^", style="bright_black")
                    elif ttype == "fort":
                        text.append(" *", style="yellow")
                    else:
                        text.append(" .", style="dim")
                text.append(" ")
            text.append("\n")
        return text

    def _render_units(self, gs: dict[str, Any]) -> RenderableType:
        t = Table(show_header=True, header_style="bold", expand=False, title="Units")
        t.add_column("ID")
        t.add_column("Team")
        t.add_column("Class")
        t.add_column("Pos")
        t.add_column("HP")
        t.add_column("Status")
        for u in gs.get("units", []):
            pos = u.get("pos") or {}
            team = u.get("owner", "")
            style = "cyan" if team == "blue" else "red"
            t.add_row(
                u.get("id", ""),
                Text(team, style=style),
                u.get("class", ""),
                f"({pos.get('x', '?')},{pos.get('y', '?')})",
                f"{u.get('hp', '?')}/{u.get('hp_max', '?')}",
                u.get("status", ""),
            )
        return t

    async def tick(self) -> None:
        import time

        now = time.time()
        if now - self._last_poll >= POLL_INTERVAL_S:
            await self._refresh_state()

    async def handle_key(self, key: str) -> Screen | None:
        # All keys route through the coach handler while in input mode
        # (so typing 'q' in a message doesn't quit, typing 'e' doesn't
        # end_turn, etc.).
        if self._input_mode == "coach":
            return await self._handle_coach_key(key)

        if key == "q":
            self.app.exit()
            return None
        if key == "e":
            return await self._call("end_turn")
        if key == "x":
            return await self._call("concede")
        if key == "c":
            self._input_mode = "coach"
            self._coach_buffer = ""
            return None
        # Scroll the reasoning panel. vim-style: k = older, j = newer,
        # 0 jumps back to the bottom (latest). Also accept arrow keys.
        if key in ("k", "up"):
            self._reasoning_offset += 1
            return None
        if key in ("j", "down"):
            self._reasoning_offset = max(0, self._reasoning_offset - 1)
            return None
        if key == "0":
            self._reasoning_offset = 0
            return None
        return None

    async def _handle_coach_key(self, key: str) -> Screen | None:
        """Keys while the coach-input prompt is active.

        Esc cancels, Enter sends. Any printable single char extends
        the buffer; Backspace deletes the tail. Every other key token
        (arrow keys, etc.) is ignored inside the input.
        """
        if key == "esc":
            self._input_mode = "normal"
            self._coach_buffer = ""
            return None
        if key == "enter":
            text = self._coach_buffer.strip()
            self._input_mode = "normal"
            self._coach_buffer = ""
            if not text:
                return None
            await self._send_coach_message(text)
            return None
        if key == "backspace":
            self._coach_buffer = self._coach_buffer[:-1]
            return None
        # Only accept ordinary printable characters — avoid eating
        # multi-character tokens like "up" / "down" that aren't real
        # single keystrokes.
        if len(key) == 1 and key.isprintable():
            self._coach_buffer += key
        return None

    async def _send_coach_message(self, text: str) -> None:
        """Send a coach message to THIS player's own agent."""
        gs = self._state or {}
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
            self._coach_sent.append(text)
            self.app.state.error_message = ""
        else:
            self.app.state.error_message = r.get("error", {}).get(
                "message", "send_to_agent rejected"
            )

    # ---- actions ----

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
        self._state = r.get("result", {})
        self.app.state.last_game_state = self._state

        # Agent-driven play: on our turn, kick off agent.play_turn if
        # none is already running. Async-fire-and-forget — the agent
        # drives tool calls directly; subsequent ticks will observe the
        # resulting state.
        await self._maybe_trigger_agent()

        # Auto-transition on game over.
        if self._state.get("status") == "game_over":
            from clash_of_robots.client.tui.screens.post_match import PostMatchScreen

            next_screen = PostMatchScreen(self.app)
            await self.app.transition(next_screen)
            return next_screen
        return None

    async def _maybe_trigger_agent(self) -> None:
        """Launch agent.play_turn() if it's our turn and none is running."""
        if self.app.state.agent is None:
            return
        if self.app.state.agent_task is not None and not self.app.state.agent_task.done():
            return
        gs = self._state or {}
        if gs.get("status") == "game_over":
            return
        my_team = gs.get("you")
        active = gs.get("active_player")
        # Don't spam every tick — but log the first time a new (my_team,
        # active) pair fails to match so we can see whether it's a
        # missing 'you' key, a missing 'team', or an active_player
        # mismatch.
        key = (my_team, active, gs.get("status"))
        if getattr(self, "_last_guard_log_key", None) != key:
            self._last_guard_log_key = key  # type: ignore[attr-defined]
            log.info(
                "agent guard: my_team=%r active=%r status=%r keys=%s",
                my_team,
                active,
                gs.get("status"),
                sorted(list(gs.keys())),
            )
        if not my_team or active != my_team:
            return
        log.info("triggering agent.play_turn for team=%s turn=%s", my_team, gs.get("turn"))

        from clash_of_robots.server.engine.state import Team

        viewer = Team.BLUE if my_team == "blue" else Team.RED
        max_turns = int(
            gs.get("max_turns")
            or (gs.get("rules", {}) or {}).get("max_turns")
            or 20
        )

        async def _run() -> None:
            try:
                log.info("agent.play_turn: starting")
                await self.app.state.agent.play_turn(viewer, max_turns=max_turns)
                log.info("agent.play_turn: finished")
            except asyncio.CancelledError:
                log.info("agent.play_turn: cancelled")
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
