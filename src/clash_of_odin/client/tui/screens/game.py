"""In-game screen — five-panel grid mirroring the room layout.

    ┌────────────┬─────────┐
    │   Map      │ Player  │
    │            ├─────────┤
    │            │ Actions │
    ├────────────┼─────────┤
    │ Reasoning  │ Coach   │
    └────────────┴─────────┘

Tab cycles focus across focusable panels (Map / Actions / Reasoning /
Coach). Arrows / j-k / Enter dispatch to the focused panel only.

  - Map (focused): tile cursor with ←↑↓→. Enter on a unit opens its
    UnitCard with full stats / tags / abilities / inventory.
  - Reasoning (focused): up/down scroll the agent-thought log.
  - Coach (focused): type freely — Enter sends, Esc clears the buffer.
  - Actions (focused): button list — end_turn / concede / quit.

Two sources of game actions:

  1. **Agent-driven**: NetworkedAgent runs on every poll where the
     active player matches the viewer's team and no agent task is
     already running. The TUI just renders + surfaces reasoning.
  2. **Manual**: the human focuses the Actions panel and hits Enter
     on `End Turn` / `Concede`. Useful for hybrid / kibitzing modes.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from dataclasses import dataclass
from typing import Any

from rich.align import Align
from rich.console import Group, RenderableType
from rich.layout import Layout
from rich.panel import Panel as RichPanel
from rich.text import Text

from clash_of_odin.client.tui.app import POLL_INTERVAL_S, Screen, TUIApp
from clash_of_odin.client.tui.panels import Panel, border_style
from clash_of_odin.client.tui.screens.room import (
    UnitCard,
    _unit_cell_style,
    _describe_win_condition,
)

log = logging.getLogger("clash.tui.game")


# ---- panel: Player (turn / team / agent status) ----


class PlayerPanel(Panel):
    title = "Player"

    def __init__(self, screen: "GameScreen") -> None:
        self.screen = screen

    def can_focus(self) -> bool:
        return False

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
                f"You: {my_team}",
                style="bold cyan" if my_team == "blue" else "bold red",
            )
        )
        rows.append(Text(f"Turn {turn}/{max_turns}"))
        rows.append(Text(""))
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
            rows.append(Text(""))
            rows.append(
                Text(
                    f"agent {'thinking…' if busy else 'idle'}",
                    style="yellow" if busy else "dim",
                )
            )
        return RichPanel(
            Group(*rows),
            title=self.title,
            border_style=border_style(focused),
            padding=(1, 2),
        )


# ---- panel: Actions (end_turn / concede / quit) ----


@dataclass
class _Btn:
    label: str
    action: str
    enabled: bool = True


class ActionsPanel(Panel):
    title = "Actions"

    def __init__(self, screen: "GameScreen") -> None:
        self.screen = screen
        self.focus = 0

    def _buttons(self) -> list[_Btn]:
        gs = self.screen.state or {}
        active = gs.get("active_player")
        my_team = gs.get("you")
        my_turn = active == my_team and gs.get("status") != "game_over"
        return [
            _Btn(label="End Turn", action="end_turn", enabled=my_turn),
            _Btn(label="Concede", action="concede", enabled=gs.get("status") != "game_over"),
            _Btn(label="Quit", action="quit"),
        ]

    def render(self, focused: bool) -> RenderableType:
        buttons = self._buttons()
        self.focus = max(0, min(self.focus, len(buttons) - 1))
        lines: list[Text] = []
        for i, btn in enumerate(buttons):
            is_focused = focused and i == self.focus
            marker = "➤ " if is_focused else "  "
            if not btn.enabled:
                style = "dim strike" if is_focused else "dim"
            elif is_focused:
                style = "bold cyan"
            else:
                style = "white"
            lines.append(Text(f"{marker}{btn.label}", style=style))
        return RichPanel(
            Group(*lines),
            title=self.title,
            border_style=border_style(focused),
            padding=(1, 2),
        )

    async def handle_key(self, key: str) -> Screen | None:
        buttons = self._buttons()
        if not buttons:
            return None
        if key in ("down", "j"):
            self.focus = (self.focus + 1) % len(buttons)
            return None
        if key in ("up", "k"):
            self.focus = (self.focus - 1) % len(buttons)
            return None
        if key == "enter":
            btn = buttons[self.focus]
            if not btn.enabled:
                return None
            return await self.screen.run_action(btn.action)
        return None


# ---- panel: Map (cursor + unit card on Enter) ----


class GameMapPanel(Panel):
    title = "Map"

    def __init__(self, screen: "GameScreen") -> None:
        self.screen = screen
        self.cx = 0
        self.cy = 0

    def _state(self) -> dict[str, Any]:
        return self.screen.state or {}

    def render(self, focused: bool) -> RenderableType:
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
                    g, st = _terrain_cell(t.get("type", "unknown"))
                if focused and x == self.cx and y == self.cy:
                    text.append(f"[{g}]", style=f"reverse {st}")
                else:
                    text.append(f" {g} ", style=st)
            text.append("\n")
        info = self._cursor_tooltip(w, h, tile_by_pos, unit_at)
        return RichPanel(
            Group(text, Text(""), info),
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
        if u:
            owner = u.get("owner", "?")
            color = "cyan" if owner == "blue" else "red"
            line.append("   ")
            line.append(
                f"{u.get('class', '?')} hp {u.get('hp', '?')}/{u.get('hp_max', '?')}",
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
        if key in ("up", "k"):
            self.cy = (self.cy - 1) % h
            return None
        if key in ("down", "j"):
            self.cy = (self.cy + 1) % h
            return None
        if key == "left":
            self.cx = (self.cx - 1) % w
            return None
        if key == "right":
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


def _terrain_cell(ttype: str) -> tuple[str, str]:
    if ttype == "unknown":
        return "?", "bright_black"
    if ttype == "forest":
        return "f", "green"
    if ttype == "mountain":
        return "^", "bright_black"
    if ttype == "fort":
        return "*", "yellow"
    return ".", "dim"


# ---- panel: Reasoning (scrollable agent thoughts) ----


class ReasoningPanel(Panel):
    title = "Agent Reasoning"

    def __init__(self, screen: "GameScreen") -> None:
        self.screen = screen
        self.offset = 0
        self._last_count = 0

    def render(self, focused: bool) -> RenderableType:
        thoughts = list(self.screen.app.state.thoughts)
        total = len(thoughts)
        # Pin user's view if they've scrolled back and new thoughts arrive.
        new_count = total - self._last_count
        if new_count > 0 and self.offset > 0:
            self.offset += new_count
        self._last_count = total

        if total == 0:
            body = Text("(no reasoning yet)", style="dim italic")
            title = self.title
        else:
            self.offset = max(0, min(self.offset, total - 1))
            # Greedy pack from newest backward.
            window: list[tuple[str, str, str]] = []
            end = total - self.offset
            # Approximate: 6 thoughts max per panel render. Wrap handles
            # the rest visually; the panel auto-crops.
            for i in range(end - 1, max(-1, end - 7), -1):
                window.append(thoughts[i])
            window.reverse()
            body = Text(no_wrap=False, overflow="fold")
            for i, (ts, team, t) in enumerate(window):
                team_style = "cyan" if team == "blue" else "red"
                body.append(f"[{ts}] ", style=team_style)
                body.append(t)
                if i != len(window) - 1:
                    body.append("\n")
            if self.offset == 0:
                title = f"{self.title} — latest {len(window)}/{total}"
            else:
                title = (
                    f"{self.title} — scrolled {self.offset}/{total}"
                )
        return RichPanel(
            body,
            title=title,
            border_style=border_style(focused),
            padding=(0, 1),
        )

    async def handle_key(self, key: str) -> Screen | None:
        if key in ("up", "k"):
            self.offset += 1
            return None
        if key in ("down", "j"):
            self.offset = max(0, self.offset - 1)
            return None
        if key == "0":
            self.offset = 0
            return None
        return None


# ---- panel: Coach (text input + history) ----


class CoachPanel(Panel):
    title = "Coach"

    def __init__(self, screen: "GameScreen") -> None:
        self.screen = screen
        self.buffer = ""
        self.history: deque[str] = deque(maxlen=5)

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
            padding=(1, 2),
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
        self._modal: UnitCard | None = None

        self.map_panel = GameMapPanel(self)
        self.actions_panel = ActionsPanel(self)
        self.reasoning_panel = ReasoningPanel(self)
        self.coach_panel = CoachPanel(self)
        self._panels: list[Panel] = [
            self.map_panel,
            PlayerPanel(self),
            self.actions_panel,
            self.reasoning_panel,
            self.coach_panel,
        ]
        # Default to the Map panel so the player can immediately scan
        # the board with the cursor.
        self._focus_idx = 0

    async def on_enter(self, app: TUIApp) -> None:
        log.info("GameScreen.on_enter: starting")
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

        from clash_of_odin.client.agent_bridge import NetworkedAgent

        async def on_thought(text: str) -> None:
            collapsed = " ".join(text.split())
            if not collapsed:
                return
            from datetime import datetime

            ts = datetime.now().strftime("%H:%M:%S")
            team = (app.state.last_game_state or {}).get("you") or "blue"
            app.state.thoughts.append((ts, team, collapsed))

        app.state.agent = NetworkedAgent(
            client=app.client,
            model=app.state.model,
            scenario=scenario,
            strategy=app.state.strategy_text,
            thoughts_callback=on_thought,
        )

    # ---- render ----

    def render(self) -> RenderableType:
        if self._modal is not None:
            return self._modal.render()

        gs = self.state or {}
        scenario = (gs.get("rules") or {}).get("scenario") or (
            self.app.state.last_room_state or {}
        ).get("scenario", "?")
        header = Text()
        header.append(scenario, style="yellow bold")
        if self.app.state.error_message:
            footer: RenderableType = Text(self.app.state.error_message, style="red")
        else:
            footer = Text(
                "Tab cycle panels   ↑/↓/←/→ navigate focused panel   "
                "Enter activate   q quit",
                style="dim",
            )
        layout = self._build_layout()
        return Group(header, Text(""), layout, Text(""), footer)

    def _build_layout(self) -> Layout:
        root = Layout()
        root.split_column(
            Layout(name="top", ratio=3),
            Layout(name="bottom", ratio=2),
        )
        root["top"].split_row(
            Layout(name="map", ratio=2),
            Layout(name="right", ratio=1),
        )
        root["top"]["right"].split_column(
            Layout(name="player", ratio=2),
            Layout(name="actions", ratio=3),
        )
        root["bottom"].split_row(
            Layout(name="reasoning", ratio=2),
            Layout(name="coach", ratio=1),
        )

        focused = self._panels[self._focus_idx]
        root["top"]["map"].update(self.map_panel.render(focused is self.map_panel))
        root["top"]["right"]["player"].update(
            self._panels[1].render(focused is self._panels[1])
        )
        root["top"]["right"]["actions"].update(
            self.actions_panel.render(focused is self.actions_panel)
        )
        root["bottom"]["reasoning"].update(
            self.reasoning_panel.render(focused is self.reasoning_panel)
        )
        root["bottom"]["coach"].update(
            self.coach_panel.render(focused is self.coach_panel)
        )
        return root

    # ---- input ----

    async def handle_key(self, key: str) -> Screen | None:
        if self._modal is not None:
            close = await self._modal.handle_key(key)
            if close:
                self._modal = None
            return None

        # When the Coach panel is focused, the buffer captures everything
        # so users can type 'q' / 'tab' / etc. into a message. The two
        # exits are Esc (clear+stay) and a blank-buffer Tab cycle handled
        # below by checking buffer state.
        coach_focused = self._panels[self._focus_idx] is self.coach_panel
        if coach_focused and key not in ("\t",):
            return await self.coach_panel.handle_key(key)
        # Tab from the coach panel only exits if the buffer is empty.
        # Otherwise the user might cycle away mid-message.
        if coach_focused and key == "\t" and self.coach_panel.buffer:
            return None

        if key == "q":
            self.app.exit()
            return None
        if key == "\t":
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
        spec = (
            (self.app.state.scenario_description or {}).get("unit_classes") or {}
        ).get(unit.get("class"))
        self._modal = UnitCard(unit=unit, class_spec=spec)

    async def run_action(self, action: str) -> Screen | None:
        if action == "end_turn":
            return await self._call("end_turn")
        if action == "concede":
            return await self._call("concede")
        if action == "quit":
            self.app.exit()
            return None
        return None

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
            from clash_of_odin.client.tui.screens.post_match import PostMatchScreen

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

        from clash_of_odin.server.engine.state import Team

        viewer = Team.BLUE if my_team == "blue" else Team.RED
        max_turns = int(
            gs.get("max_turns")
            or (gs.get("rules", {}) or {}).get("max_turns")
            or 20
        )

        async def _run() -> None:
            from clash_of_odin.client.providers.errors import (
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
