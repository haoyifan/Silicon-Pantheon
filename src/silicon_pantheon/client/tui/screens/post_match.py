"""Post-match screen — winner banner, replay download, exit to lobby.

Keys:
  d    download replay, save to ~/.silicon-pantheon/replays/<id>.jsonl
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

from silicon_pantheon.client.tui.app import Screen, TUIApp


def _default_download_dir() -> Path:
    return Path.home() / ".silicon-pantheon" / "replays"


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
            from silicon_pantheon.server.engine.state import Team

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
            banner = Text(f"You lost — {winner} wins", style="bold red")
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

    async def _back_to_lobby_debug(self) -> None:
        """Log the connection state just before leaving, so we can
        see if the session was already dead when the user presses l."""
        import logging as _logging

        _dlog = _logging.getLogger("silicon.tui.post_match")
        if self.app.client is not None:
            _dlog.info(
                "back_to_lobby: cid=%s — about to call leave_room",
                self.app.client.connection_id,
            )
        else:
            _dlog.warning("back_to_lobby: app.client is None")

    async def _download_replay(self) -> None:
        import logging as _logging

        _dlog = _logging.getLogger("silicon.tui.post_match")
        if self.app.client is None:
            self._download_error = "not connected"
            _dlog.warning("download_replay: app.client is None")
            return
        # Log the cid we're about to use so it can be cross-referenced
        # with the server-side download_replay log line that prints
        # the connection's actual state. If these don't match we know
        # the TUI is using a stale/fresh cid.
        _dlog.info(
            "download_replay: pressing d cid=%s room_id=%s",
            self.app.client.connection_id,
            self.app.state.room_id,
        )
        try:
            r = await self.app.client.call("download_replay")
        except Exception as e:
            self._download_error = str(e)
            _dlog.exception("download_replay: transport raised")
            return
        if not r.get("ok"):
            self._download_error = r.get("error", {}).get("message", "rejected")
            _dlog.warning(
                "download_replay: server rejected cid=%s err=%s",
                self.app.client.connection_id, r.get("error"),
            )
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
        import logging as _logging

        _dlog = _logging.getLogger("silicon.tui.post_match")
        # Tell the server we're leaving — this flips our connection
        # back to IN_LOBBY and lets the server tear down the (now
        # FINISHED) room. Without this, creating a new room fails
        # with 'requires state=in_lobby' because the server still
        # thinks we're IN_GAME, and the zombie room stays listed.
        if self.app.client is not None:
            _dlog.info(
                "back_to_lobby: cid=%s calling leave_room",
                self.app.client.connection_id,
            )
            try:
                r = await self.app.client.call("leave_room")
                _dlog.info(
                    "back_to_lobby: leave_room result ok=%s err=%s",
                    r.get("ok"), r.get("error"),
                )
            except Exception as e:
                # This is the likely failure point: if the server
                # already evicted the connection (heartbeat timeout,
                # hard disconnect), leave_room will fail. Log it so
                # we can see the exact error.
                _dlog.exception(
                    "back_to_lobby: leave_room FAILED cid=%s — "
                    "server may have already evicted this connection. "
                    "The lobby screen will see 'set_player_metadata "
                    "first' because the connection state is stale.",
                )
        else:
            _dlog.warning("back_to_lobby: app.client is None")
        self.app.state.room_id = None
        self.app.state.slot = None
        self.app.state.last_game_state = None
        from silicon_pantheon.client.tui.screens.lobby import LobbyScreen

        return LobbyScreen(self.app)
