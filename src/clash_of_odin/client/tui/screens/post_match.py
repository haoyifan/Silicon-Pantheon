"""Post-match screen — winner banner, replay download, exit to lobby.

Keys:
  d    download replay, save to ~/.clash-of-odin/replays/<id>.jsonl
  Enter or l   back to lobby (if the server still accepts — tokens
               expire ~60s after game_over)
  q    quit
"""

from __future__ import annotations

from pathlib import Path

from rich.align import Align
from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.text import Text

from clash_of_odin.client.tui.app import Screen, TUIApp


def _default_download_dir() -> Path:
    return Path.home() / ".clash-of-odin" / "replays"


class PostMatchScreen(Screen):
    def __init__(self, app: TUIApp):
        self.app = app
        self._downloaded_path: Path | None = None
        self._download_error: str | None = None
        self._summary_state: str = ""  # "", "pending", "done", "failed"
        self._summary_path: Path | None = None

    async def on_enter(self, app: TUIApp) -> None:
        # Kick off a background summary if an agent is attached.
        if app.state.agent is None:
            return
        self._summary_state = "pending"

        async def _summarize() -> None:
            gs = app.state.last_game_state or {}
            my_team = gs.get("you") or "blue"
            from clash_of_odin.server.engine.state import Team

            viewer = Team.BLUE if my_team == "blue" else Team.RED
            try:
                lesson = await app.state.agent.summarize_match(viewer)
            except Exception:
                self._summary_state = "failed"
                return
            finally:
                # Close the persistent SDK session now that we're done
                # with it — the TUI may loop back to the lobby after
                # this screen, and next match needs a fresh agent.
                if app.state.agent is not None:
                    try:
                        await app.state.agent.close()
                    except Exception:
                        pass
                    app.state.agent = None
            if lesson is None:
                self._summary_state = "failed"
                return
            self._summary_state = "done"
            # summarize_match already saved it if lessons_dir was set;
            # expose the agent-reported title for the TUI.
            self._summary_path = Path(
                f"lessons/{lesson.scenario}/{lesson.slug}.md"
            )

        import asyncio as _asyncio

        _asyncio.create_task(_summarize())

    def render(self) -> RenderableType:
        gs = self.app.state.last_game_state or {}
        winner = gs.get("winner")
        my_team = gs.get("you")
        reason = (gs.get("last_action") or {}).get("reason", "")

        if winner is None:
            banner = Text("Match ended in a draw", style="bold yellow")
        elif my_team and winner == my_team:
            banner = Text(f"You won! (team {winner})", style="bold green")
        else:
            # Use magenta for the loss banner — bold red would
            # collide with the red team color (the message names
            # winner explicitly, and the message renders for either
            # losing side, so it shouldn't read as a team label).
            banner = Text(f"You lost — {winner} wins", style="bold magenta")
        if reason:
            banner.append(f"  (reason: {reason})", style="dim")

        summary = Text(
            f"Turns played: {gs.get('turn', '?')} / {gs.get('max_turns', '?')}\n"
            f"Survivors — blue: {sum(1 for u in gs.get('units', []) if u.get('owner') == 'blue')}  "
            f"red: {sum(1 for u in gs.get('units', []) if u.get('owner') == 'red')}",
            style="dim",
        )

        download_line = Text("")
        if self._downloaded_path is not None:
            download_line.append(
                f"Replay saved to: {self._downloaded_path}", style="green"
            )
        elif self._download_error:
            download_line.append(
                f"Download failed: {self._download_error}", style="red"
            )

        keys = Text(
            "d download replay   l back to lobby   q quit",
            style="dim",
        )

        summary_line = Text("")
        if self._summary_state == "pending":
            summary_line.append("agent reviewing the match…", style="yellow")
        elif self._summary_state == "done" and self._summary_path is not None:
            summary_line.append(f"lesson saved: {self._summary_path}", style="green")
        elif self._summary_state == "failed":
            summary_line.append("lesson summary failed", style="dim red")

        body = Group(
            banner,
            Text(""),
            summary,
            Text(""),
            download_line,
            summary_line,
            Text(""),
            keys,
        )
        return Align.center(
            Panel(body, title="match over", border_style="green"), vertical="middle"
        )

    async def handle_key(self, key: str) -> Screen | None:
        if key == "q":
            self.app.exit()
            return None
        if key == "d":
            await self._download_replay()
            return None
        if key in ("enter", "l"):
            return await self._back_to_lobby()
        return None

    # ---- actions ----

    async def _download_replay(self) -> None:
        if self.app.client is None:
            self._download_error = "not connected"
            return
        try:
            r = await self.app.client.call("download_replay")
        except Exception as e:
            self._download_error = str(e)
            return
        if not r.get("ok"):
            self._download_error = r.get("error", {}).get("message", "rejected")
            return
        body = r.get("replay_jsonl", "")
        dir_ = _default_download_dir()
        dir_.mkdir(parents=True, exist_ok=True)
        match_id = self.app.state.room_id or "match"
        path = dir_ / f"{match_id}.jsonl"
        try:
            path.write_text(body, encoding="utf-8")
        except OSError as e:
            self._download_error = str(e)
            return
        self._downloaded_path = path
        self._download_error = None

    async def _back_to_lobby(self) -> Screen | None:
        # Tell the server we're leaving — this flips our connection
        # back to IN_LOBBY and lets the server tear down the (now
        # FINISHED) room. Without this, creating a new room fails
        # with 'requires state=in_lobby' because the server still
        # thinks we're IN_GAME, and the zombie room stays listed.
        if self.app.client is not None:
            try:
                await self.app.client.call("leave_room")
            except Exception:
                # Non-fatal — the heartbeat sweeper will eventually
                # evict us anyway.
                pass
        self.app.state.room_id = None
        self.app.state.slot = None
        self.app.state.last_game_state = None
        from clash_of_odin.client.tui.screens.lobby import LobbyScreen

        return LobbyScreen(self.app)
