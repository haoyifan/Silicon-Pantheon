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

from typing import Any

from rich.align import Align
from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from clash_of_robots.client.tui.app import POLL_INTERVAL_S, Screen, TUIApp


class GameScreen(Screen):
    def __init__(self, app: TUIApp):
        self.app = app
        self._last_poll = 0.0
        self._state: dict[str, Any] | None = None

    async def on_enter(self, app: TUIApp) -> None:
        await self._refresh_state()
        # Build the agent bridge once we know the scenario (which is
        # pulled from the room state we observed before transitioning).
        if app.state.agent is None:
            await self._maybe_build_agent(app)

    async def on_exit(self, app: TUIApp) -> None:
        # Cancel any in-flight agent turn when leaving the screen.
        if app.state.agent_task is not None and not app.state.agent_task.done():
            app.state.agent_task.cancel()
        app.state.agent_task = None

    async def _maybe_build_agent(self, app: TUIApp) -> None:
        """Construct a NetworkedAgent if the login declared an LLM."""
        if app.state.kind not in ("ai", "hybrid"):
            return
        if app.state.provider != "anthropic" or not app.state.model:
            return  # only claude is wired up today
        if app.client is None:
            return
        scenario = (app.state.last_room_state or {}).get("scenario") or ""
        if not scenario:
            return

        from clash_of_robots.client.agent_bridge import NetworkedAgent

        async def on_thought(text: str) -> None:
            collapsed = " ".join(text.split())
            if collapsed:
                app.state.thoughts.append(collapsed)

        app.state.agent = NetworkedAgent(
            client=app.client,
            model=app.state.model,
            scenario=scenario,
            strategy=app.state.strategy_text,
            thoughts_callback=on_thought,
        )

    def render(self) -> RenderableType:
        gs = self._state or {}
        turn = gs.get("turn", "?")
        max_turns = gs.get("max_turns") or gs.get("rules", {}).get("max_turns", "?")
        active = gs.get("active_player", "?")
        status = gs.get("status", "?")
        winner = gs.get("winner")
        you = self.app.state.slot or "?"
        my_team = (gs.get("you") or {}).get("team", "?")

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

        keys = Text("e end_turn   c concede   q quit", style="dim")
        status_line = Text("")
        if self.app.state.error_message:
            status_line.append(self.app.state.error_message, style="red")

        thoughts_panel = self._render_thoughts()

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
            Text(""),
            you_info,
            agent_line,
            Text(""),
            keys,
            status_line,
        )
        return Panel(body, title="game", border_style="red")

    def _render_thoughts(self) -> RenderableType:
        """Fixed-height panel of the last few reasoning snippets."""
        thoughts = list(self.app.state.thoughts)
        height = 10
        inner = height - 2
        body = Text(no_wrap=True, overflow="ellipsis")
        visible = thoughts[-inner:] if inner > 0 else []
        if not visible:
            body.append("(no reasoning yet)", style="dim italic")
        else:
            for i, t in enumerate(visible):
                body.append(t)
                if i != len(visible) - 1:
                    body.append("\n")
        return Panel(body, title="agent reasoning", border_style="dim", height=height)

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
        if key == "q":
            self.app.exit()
            return None
        if key == "e":
            return await self._call("end_turn")
        if key == "c":
            return await self._call("concede")
        return None

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
        my_team = (gs.get("you") or {}).get("team")
        active = gs.get("active_player")
        if not my_team or active != my_team:
            return

        from clash_of_robots.server.engine.state import Team

        viewer = Team.BLUE if my_team == "blue" else Team.RED
        max_turns = int(
            gs.get("max_turns")
            or (gs.get("rules", {}) or {}).get("max_turns")
            or 20
        )

        async def _run() -> None:
            try:
                await self.app.state.agent.play_turn(viewer, max_turns=max_turns)
            except asyncio.CancelledError:
                pass
            except Exception as e:
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
