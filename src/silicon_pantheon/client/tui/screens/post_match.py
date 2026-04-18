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
        self._match_stats: "MatchStats | None" = None
        self._agent_stats: dict | None = None

    async def on_enter(self, app: TUIApp) -> None:
        # Compute match stats from history before the agent closes.
        await self._compute_stats(app)

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
        from silicon_pantheon.client.locale import t
        lc = self.app.state.locale

        gs = self.app.state.last_game_state or {}
        winner = gs.get("winner")
        my_team = gs.get("you")
        reason = (gs.get("last_action") or {}).get("reason", "")

        if winner is None:
            banner = Text(t("post_match.draw", lc), style="bold yellow")
        elif my_team and winner == my_team:
            banner = Text(f"{t('post_match.you_won', lc)} (team {winner})", style="bold green")
        else:
            banner = Text(f"{t('post_match.you_lost', lc)} — {winner}", style="bold red")
        if reason:
            banner.append(f"  ({reason})", style="dim")

        summary = Text(
            f"{t('post_match_summary.turns', lc)}: {gs.get('turn', '?')} / {gs.get('max_turns', '?')}\n"
            f"{t('post_match_summary.blue', lc)}: {sum(1 for u in gs.get('units', []) if u.get('owner') == 'blue')}  "
            f"{t('post_match_summary.red', lc)}: {sum(1 for u in gs.get('units', []) if u.get('owner') == 'red')}",
            style="dim",
        )

        parts: list[RenderableType] = [banner, Text(""), summary]

        # ---- Match stats ----
        ms = self._match_stats
        if ms is not None:
            parts.append(Text(""))
            parts.append(self._render_stats(ms, lc))

        # ---- Download / lesson status ----
        parts.append(Text(""))
        download_line = Text("")
        if self._downloaded_path is not None:
            download_line.append(
                f"{t('post_match.replay_saved', lc)}: {self._downloaded_path}", style="green"
            )
        elif self._download_error:
            download_line.append(
                f"{t('post_match.download_failed', lc)}: {self._download_error}", style="red"
            )
        parts.append(download_line)

        summary_line = Text("")
        if self._summary_state == "pending":
            summary_line.append(t("post_match.lesson_pending", lc), style="yellow")
        elif self._summary_state == "done" and self._summary_path is not None:
            summary_line.append(f"{t('post_match.lesson_done', lc)}: {self._summary_path}", style="green")
        elif self._summary_state == "failed":
            summary_line.append(t("post_match.lesson_failed", lc), style="dim red")
        parts.append(summary_line)

        parts.append(Text(""))
        parts.append(Text(t("post_match.footer", lc), style="dim"))

        return Align.center(
            Panel(Group(*parts), title=t("post_match.title", lc), border_style="green"), vertical="middle"
        )

    def _render_stats(self, ms: "MatchStats", lc: str) -> RenderableType:
        """Render the match statistics table."""
        from rich.table import Table

        from silicon_pantheon.client.locale import t

        rows: list[RenderableType] = []

        # ---- Team comparison table ----
        tbl = Table(expand=False, show_lines=False, padding=(0, 2))
        tbl.add_column("", style="bold")
        tbl.add_column(t("post_match_summary.blue", lc), style="cyan", justify="right")
        tbl.add_column(t("post_match_summary.red", lc), style="red", justify="right")

        b, r = ms.blue, ms.red
        tbl.add_row("Damage dealt", str(b.total_damage_dealt), str(r.total_damage_dealt))
        tbl.add_row("Healing done", str(b.total_healing), str(r.total_healing))
        tbl.add_row(
            "Units lost",
            f"{b.units_lost}/{b.units_fielded}",
            f"{r.units_lost}/{r.units_fielded}",
        )

        # Server-side telemetry for both teams.
        telemetry = self._agent_stats or {}
        blue_t = telemetry.get("blue", {})
        red_t = telemetry.get("red", {})
        if blue_t.get("turns_played", 0) > 0 or red_t.get("turns_played", 0) > 0:
            def _fmt_time(d: dict) -> str:
                v = d.get("avg_thinking_time_s", 0)
                return f"{v:.1f}s" if v else "—"
            def _fmt_tokens(d: dict) -> str:
                v = d.get("total_tokens", 0)
                return f"{v:,}" if v else "—"
            def _fmt_int(d: dict, key: str) -> str:
                v = d.get(key, 0)
                return str(v) if v else "—"

            tbl.add_row("Avg think/turn", _fmt_time(blue_t), _fmt_time(red_t))
            tbl.add_row("Tokens used", _fmt_tokens(blue_t), _fmt_tokens(red_t))
            tbl.add_row("Tool calls", _fmt_int(blue_t, "total_tool_calls"), _fmt_int(red_t, "total_tool_calls"))
            b_err = blue_t.get("total_errors", 0)
            r_err = red_t.get("total_errors", 0)
            if b_err or r_err:
                tbl.add_row("Errors", str(b_err) if b_err else "—", str(r_err) if r_err else "—")

        rows.append(tbl)

        # ---- MVP + top killers ----
        mvp = ms.mvp()
        if mvp:
            rows.append(Text(""))
            color = "cyan" if mvp.owner == "blue" else "red"
            rows.append(Text.assemble(
                ("MVP: ", "bold"),
                (f"{mvp.display_name}", f"bold {color}"),
                (f" — {mvp.kills} kills, {mvp.damage_dealt} damage", "dim"),
            ))

        # Top killers (units with kills > 0, sorted by kills).
        killers = sorted(
            [u for u in ms.units.values() if u.kills > 0],
            key=lambda u: (-u.kills, -u.damage_dealt),
        )
        if len(killers) > 1:
            rows.append(Text(""))
            for u in killers[:6]:
                color = "cyan" if u.owner == "blue" else "red"
                rows.append(Text(
                    f"  {u.display_name}: {u.kills} kills, {u.damage_dealt} dmg",
                    style=color,
                ))

        if ms.first_kill_turn is not None:
            rows.append(Text(""))
            rows.append(Text(f"First kill: turn {ms.first_kill_turn}", style="dim"))

        return Group(*rows)

    async def _compute_stats(self, app: TUIApp) -> None:
        """Fetch history + server telemetry and compute match stats."""
        from silicon_pantheon.match_stats import compute_match_stats

        gs = app.state.last_game_state or {}
        units = gs.get("units") or []
        history: list[dict] = []
        if app.client is not None:
            try:
                r = await app.client.call("get_history", last_n=0)
                history = (r.get("result") or {}).get("history") or []
            except Exception:
                pass
            # Fetch server-side telemetry (turn times, tool calls,
            # tokens for BOTH players).
            try:
                r = await app.client.call("get_match_telemetry")
                if r.get("ok"):
                    self._agent_stats = (r.get("result") or {})
            except Exception:
                pass
        self._match_stats = compute_match_stats(
            history=history,
            units=units,
            game_state=gs,
            scenario_description=app.state.scenario_description,
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
            from silicon_pantheon.client.locale import t as _t
            self._download_error = _t("errors.not_connected", self.app.state.locale)
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
            self._download_error = (r.get("error") or {}).get("message", "rejected")
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
