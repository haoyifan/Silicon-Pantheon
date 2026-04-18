"""Replay picker — browse and select a replay file to watch.

Scans two directories for replay files:
  1. ~/.silicon-pantheon/replays/*.jsonl  (networked match downloads)
  2. runs/<ts>_<scenario>/replay.jsonl    (offline runs)

Lists them sorted by modification time (newest first). Enter opens
the selected replay in the ReplayScreen.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from rich.align import Align
from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from silicon_pantheon.client.locale import t
from silicon_pantheon.client.tui.app import Screen, TUIApp

log = logging.getLogger("silicon.tui.replay_picker")


def _scan_replays() -> list[dict[str, Any]]:
    """Find all replay files and return metadata dicts sorted newest first."""
    results: list[dict[str, Any]] = []

    # 1. ~/.silicon-pantheon/replays/*.jsonl
    replays_dir = Path.home() / ".silicon-pantheon" / "replays"
    if replays_dir.is_dir():
        for f in replays_dir.glob("*.jsonl"):
            meta = _extract_meta(f)
            results.append(meta)

    # 2. runs/*/replay.jsonl (relative to project root)
    # Try multiple candidate roots
    for root_candidate in (
        Path.cwd(),
        Path(__file__).resolve().parents[5],
    ):
        runs_dir = root_candidate / "runs"
        if runs_dir.is_dir():
            for f in runs_dir.glob("*/replay.jsonl"):
                meta = _extract_meta(f)
                results.append(meta)
            break  # Only scan the first found runs/ dir

    # Dedup by path, sort newest first
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for m in results:
        key = str(m["path"])
        if key not in seen:
            seen.add(key)
            deduped.append(m)
    deduped.sort(key=lambda m: m.get("mtime", 0), reverse=True)
    return deduped


def _extract_meta(path: Path) -> dict[str, Any]:
    """Read the replay header events to extract metadata.

    Scans the first ~10 lines for match_start and match_players,
    and the last ~5 lines for match_end. Gracefully handles old
    replays that lack these events.
    """
    import datetime

    meta: dict[str, Any] = {
        "path": path,
        "mtime": path.stat().st_mtime if path.exists() else 0,
        "scenario": "?",
        "filename": path.name,
        "blue_model": "?",
        "red_model": "?",
        "winner": "?",
        "turns": "?",
        "date": "?",
    }
    try:
        lines: list[str] = []
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                lines.append(line.strip())
        # Parse header events (first ~20 lines)
        for raw_line in lines[:20]:
            if not raw_line:
                continue
            try:
                raw = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            kind = raw.get("kind", "")
            data = raw.get("payload") or raw.get("data") or {}
            if kind == "match_start":
                meta["scenario"] = data.get("scenario") or "?"
                started_at = data.get("started_at")
                if started_at:
                    dt = datetime.datetime.fromtimestamp(started_at)
                    meta["date"] = dt.strftime("%Y-%m-%d %H:%M")
            elif kind == "match_players":
                players = data.get("players") or {}
                blue = players.get("blue") or {}
                red = players.get("red") or {}
                meta["blue_model"] = blue.get("model") or blue.get("display_name") or "?"
                meta["red_model"] = red.get("model") or red.get("display_name") or "?"
        # Parse tail events (last ~10 lines) for match_end
        for raw_line in reversed(lines[-10:]):
            if not raw_line:
                continue
            try:
                raw = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            kind = raw.get("kind", "")
            data = raw.get("payload") or raw.get("data") or {}
            if kind == "match_end":
                meta["winner"] = data.get("winner") or "draw"
                meta["turns"] = data.get("turns_played") or "?"
                break
            # Fallback: check last end_turn action for winner
            if kind == "action":
                w = data.get("winner")
                if w is not None:
                    meta["winner"] = w or "draw"
                    break
    except Exception:
        pass

    # Fallback date from mtime if not in replay
    if meta["date"] == "?" and meta["mtime"]:
        dt = datetime.datetime.fromtimestamp(meta["mtime"])
        meta["date"] = dt.strftime("%Y-%m-%d %H:%M")
    return meta


class ReplayPickerScreen(Screen):
    def __init__(self, app: TUIApp):
        self.app = app
        self._replays: list[dict[str, Any]] = []
        self._selected = 0
        self._loaded = False

    async def on_enter(self, app: TUIApp) -> None:
        self._replays = _scan_replays()
        self._loaded = True
        log.info("ReplayPicker: found %d replay files", len(self._replays))

    def render(self) -> RenderableType:
        lc = self.app.state.locale
        header = Text(t("replay_picker.title", lc), style="bold magenta")

        if not self._loaded:
            body = Group(header, Text(t("replay_picker.scanning", lc), style="dim"))
            return Panel(Align.center(body), border_style="magenta")

        table = Table(expand=True, show_lines=False, header_style="bold")
        table.add_column(" ", width=2)
        table.add_column(t("replay_picker.col_date", lc), width=18)
        table.add_column(t("replay_picker.col_scenario", lc))
        table.add_column(t("replay_picker.col_blue", lc))
        table.add_column(t("replay_picker.col_red", lc))
        table.add_column(t("replay_picker.col_winner", lc), width=6)
        table.add_column(t("replay_picker.col_turns", lc), width=5)

        if not self._replays:
            table.add_row("", t("replay_picker.no_replays", lc), "", "", "", "", "")
        else:
            for i, m in enumerate(self._replays):
                marker = "➤" if i == self._selected else " "
                winner = m.get("winner", "?")
                winner_style = (
                    "bold cyan" if winner == "blue"
                    else "bold red" if winner == "red"
                    else "dim" if winner == "draw"
                    else ""
                )
                table.add_row(
                    marker,
                    m.get("date", "?"),
                    m.get("scenario", "?"),
                    m.get("blue_model", "?"),
                    m.get("red_model", "?"),
                    Text(str(winner), style=winner_style),
                    str(m.get("turns", "?")),
                    style="bold" if i == self._selected else None,
                )

        footer = Text(t("replay_picker.footer", lc), style="dim")
        status = Text("")
        if self.app.state.error_message:
            status.append(self.app.state.error_message, style="red")

        body = Group(header, Text(""), table, Text(""), footer, status)
        return Panel(
            Align.center(body, vertical="top"),
            border_style="magenta",
            title=t("replay_picker.title", lc),
        )

    async def handle_key(self, key: str) -> Screen | None:
        if key in ("q", "escape"):
            from silicon_pantheon.client.tui.screens.lobby import LobbyScreen
            return LobbyScreen(self.app)
        if key in ("down", "j"):
            if self._replays:
                self._selected = (self._selected + 1) % len(self._replays)
            return None
        if key in ("up", "k"):
            if self._replays:
                self._selected = (self._selected - 1) % len(self._replays)
            return None
        if key == "r":
            self._replays = _scan_replays()
            self._selected = 0
            return None
        if key == "enter":
            return await self._open_selected()
        return None

    async def _open_selected(self) -> Screen | None:
        if not self._replays:
            return None
        meta = self._replays[self._selected]
        path = meta["path"]
        try:
            from silicon_pantheon.client.tui.screens.replay import ReplayScreen
            return ReplayScreen(self.app, path)
        except Exception as e:
            self.app.state.error_message = f"Failed to load replay: {e}"
            log.exception("Failed to load replay %s", path)
            return None

    async def tick(self) -> None:
        pass
