"""Replay screen — step through a recorded match using the same
rendering panels as the live GameScreen.

Loads replay.jsonl events, precomputes per-step GameState snapshots,
and converts them to the dict format the panels consume. Arrow keys /
j/k navigate forward/backward; s skips to the next action event.

The screen satisfies the same duck-type attribute contract as
GameScreen so PlayerPanel, GameMapPanel, and ReasoningPanel can be
reused verbatim — no panel code changes needed.
"""

from __future__ import annotations

import copy
import json
import logging
from collections import deque
from pathlib import Path
from typing import Any

from rich.console import RenderableType
from rich.layout import Layout
from rich.text import Text

from silicon_pantheon.client.locale import t
from silicon_pantheon.client.tui.app import Screen, TUIApp
from silicon_pantheon.client.tui.screens.game import (
    CoachPanel,
    GameMapPanel,
    PlayerPanel,
    ReasoningPanel,
    UnitCard,
)
from silicon_pantheon.match.replay_schema import (
    AgentThought,
    MatchStart,
    ReplayEvent,
    action_from_payload,
    parse_event,
    UnreconstructibleAction,
)
from silicon_pantheon.server.engine.rules import IllegalAction, apply
from silicon_pantheon.server.engine.scenarios import load_scenario
from silicon_pantheon.server.engine.serialize import state_to_dict
from silicon_pantheon.server.engine.state import GameState

log = logging.getLogger("silicon.tui.replay")


# ---- data loading -------------------------------------------------------


def load_events(replay_path: Path) -> list[ReplayEvent]:
    events: list[ReplayEvent] = []
    with replay_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue
            events.append(parse_event(raw))
    return events


def find_match_start(events: list[ReplayEvent]) -> MatchStart | None:
    for ev in events:
        if ev.kind == "match_start" and isinstance(ev.data, MatchStart):
            return ev.data
    return None


def build_snapshots(
    initial_state: GameState,
    timeline: list[ReplayEvent],
) -> list[GameState]:
    """Precompute per-step snapshots. snapshots[0] is initial state,
    snapshots[i] for i>=1 is state AFTER timeline[i-1]."""
    snapshots: list[GameState] = [copy.deepcopy(initial_state)]
    state = copy.deepcopy(initial_state)
    for ev in timeline:
        if ev.kind == "action" and isinstance(ev.data, dict):
            try:
                action = action_from_payload(ev.data)
                apply(state, action)
            except (UnreconstructibleAction, IllegalAction) as e:
                log.warning("replay diverged at action: %s", e)
        snapshots.append(copy.deepcopy(state))
    return snapshots


def _action_summary(payload: dict) -> str:
    """One-line description of an action for the step info panel."""
    t = payload.get("type")
    if t == "move":
        dest = payload.get("dest") or payload.get("to") or {}
        return f"{payload.get('unit_id')} → ({dest.get('x')},{dest.get('y')})"
    if t == "attack":
        dmg = payload.get("damage_dealt") or payload.get("damage_to_defender") or "?"
        parts = [f"{payload.get('unit_id')} attacks {payload.get('target_id')}", f"dmg={dmg}"]
        if payload.get("defender_dies"):
            parts.append("KILL")
        return " | ".join(parts)
    if t == "heal":
        return f"{payload.get('healer_id')} heals {payload.get('target_id')}"
    if t == "wait":
        return f"{payload.get('unit_id')} waits"
    if t == "end_turn":
        parts = [f"{payload.get('by')} ends turn"]
        if payload.get("winner"):
            parts.append(f"WINNER: {payload['winner']}")
        return " | ".join(parts)
    return str(t)


# ---- StepInfoPanel (replaces CoachPanel in the bottom-right) -----------


class StepInfoPanel:
    """Shows current step info and controls. Occupies the coach slot."""

    def __init__(self, screen: "ReplayScreen"):
        self.screen = screen

    @property
    def title(self) -> str:
        return t("replay.step_info", self.screen.app.state.locale)

    def key_hints(self) -> str:
        lc = self.screen.app.state.locale
        return t("replay.key_hints", lc)

    def render(self, focused: bool) -> RenderableType:
        from rich.panel import Panel as RichPanel

        border = "bright_cyan" if focused else "dim"
        lc = self.screen.app.state.locale
        step = self.screen._step
        total = len(self.screen._timeline)
        ev = self.screen._timeline[step - 1] if step >= 1 else None

        lines = Text()
        lines.append(
            f"{t('replay.step', lc)} {step}/{total}\n",
            style="bold yellow",
        )

        if ev is None:
            lines.append(t("replay.initial_state", lc), style="dim italic")
        elif ev.kind == "agent_thought" and isinstance(ev.data, AgentThought):
            style = "cyan" if ev.data.team == "blue" else "red"
            lines.append(f"T{ev.turn} [{ev.data.team}] ", style=style + " bold")
            lines.append(t("replay.thought", lc), style=style)
        elif ev.kind == "action" and isinstance(ev.data, dict):
            by = ev.data.get("by") or ""
            style = "cyan" if by == "blue" else "red" if by == "red" else "white"
            lines.append(f"T{ev.turn} ", style=style + " bold")
            lines.append(_action_summary(ev.data), style=style)
        elif ev.kind == "forced_end_turn":
            lines.append(f"T{ev.turn} forced end_turn", style="yellow")
        else:
            lines.append(f"T{ev.turn} {ev.kind}", style="magenta")

        # Navigation status
        lines.append("\n\n")
        if step == 0:
            lines.append(f"► {t('replay.press_next', lc)}", style="dim")
        elif step == total:
            lines.append(f"✓ {t('replay.end_of_match', lc)}", style="bold green")

        return RichPanel(
            lines,
            title=f"{self.title} [{step}/{total}]",
            border_style=border,
            padding=(0, 1),
        )

    async def handle_key(self, key: str) -> Screen | None:
        return None


# ---- ReplayScreen -------------------------------------------------------


class ReplayScreen(Screen):
    """Step-through match replay using the same panels as GameScreen."""

    def __init__(
        self,
        app: TUIApp,
        replay_path: Path,
    ):
        self.app = app
        self._replay_path = replay_path

        # Load and parse replay
        events = load_events(replay_path)
        meta = find_match_start(events)
        if meta is None or meta.scenario is None:
            raise ValueError("replay missing match_start with scenario")

        self._scenario_name = meta.scenario
        initial_state = load_scenario(meta.scenario)
        if meta.max_turns:
            initial_state.max_turns = meta.max_turns

        # Skip match_start event itself
        self._timeline = [ev for ev in events if ev.kind != "match_start"]

        # Precompute snapshots + dict cache
        self._snapshots = build_snapshots(initial_state, self._timeline)
        self._dict_cache: dict[int, dict] = {}

        # Extract all thoughts indexed by step
        self._thoughts_by_step: dict[int, list[tuple[str, str, str]]] = {}
        for i, ev in enumerate(self._timeline):
            step = i + 1  # step 1 = after timeline[0]
            if ev.kind == "agent_thought" and isinstance(ev.data, AgentThought):
                self._thoughts_by_step.setdefault(step, []).append(
                    (f"T{ev.turn}", ev.data.team, ev.data.text)
                )

        # Current step (0 = initial state, 1..N = after timeline events)
        self._step = 0

        # ---- GameScreen-compatible attributes ----
        # These satisfy the duck-type contract for PlayerPanel,
        # GameMapPanel, and ReasoningPanel.
        self.state: dict[str, Any] | None = None
        self.unit_card: UnitCard | None = None
        self.highlighted_unit_id: str | None = None
        self.range_overlay_unit: str | None = None
        self.range_move_tiles: set[tuple[int, int]] = set()
        self.range_attack_tiles: set[tuple[int, int]] = set()
        self._combat_attacker_id: str | None = None
        self._combat_target_id: str | None = None
        self.unit_last_actions: dict[str, str] = {}

        # Build panels using self as screen (duck-typed GameScreen)
        self.map_panel = GameMapPanel(self)
        self.reasoning_panel = ReasoningPanel(self)
        self._step_panel = StepInfoPanel(self)
        self._panels = [
            self.map_panel,
            PlayerPanel(self),
            self.reasoning_panel,
            self._step_panel,
        ]
        self._focus_idx = 0

        # Load scenario description for unit display names
        self._load_scenario_description()

        # Set initial state
        self._apply_step()

    def _load_scenario_description(self) -> None:
        """Load scenario metadata for display names, terrain, etc.

        Uses the server connection if available (describe_scenario tool),
        otherwise falls back to loading the config.yaml directly.
        """
        try:
            import yaml
            from silicon_pantheon.server.engine.scenarios import _games_root
            from silicon_pantheon.client.locale.scenario import localize_scenario

            path = _games_root() / self._scenario_name / "config.yaml"
            if path.is_file():
                cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
                # Build minimal description dict from config
                raw: dict[str, Any] = {
                    "name": self._scenario_name,
                    "description": cfg.get("description", ""),
                    "unit_classes": {},
                    "armies": cfg.get("armies", {}),
                    "board": cfg.get("board", {}),
                }
                # Extract unit class info for display names
                for cls_name, cls_data in (cfg.get("unit_classes") or {}).items():
                    if isinstance(cls_data, dict):
                        raw["unit_classes"][cls_name] = cls_data
                lc = self.app.state.locale
                self.app.state.scenario_description = localize_scenario(
                    raw, self._scenario_name, lc,
                )
            else:
                self.app.state.scenario_description = None
        except Exception:
            log.debug("Failed to load scenario description for replay", exc_info=True)
            self.app.state.scenario_description = None

    def _state_dict(self, step: int) -> dict:
        """Get the serialized state dict for a given step, with caching."""
        if step not in self._dict_cache:
            gs = self._snapshots[step]
            d = state_to_dict(gs, viewer=None)
            # Add fields the panels expect
            d["you"] = "blue"  # replay shows from blue perspective
            d["status"] = gs.status.value if hasattr(gs.status, "value") else str(gs.status)
            self._dict_cache[step] = d
        return self._dict_cache[step]

    def _apply_step(self) -> None:
        """Update visible state to match current step."""
        self.state = self._state_dict(self._step)

        # Update combat highlights from current step's event
        self._combat_attacker_id = None
        self._combat_target_id = None
        if self._step >= 1:
            ev = self._timeline[self._step - 1]
            if ev.kind == "action" and isinstance(ev.data, dict):
                if ev.data.get("type") == "attack":
                    self._combat_attacker_id = ev.data.get("unit_id")
                    self._combat_target_id = ev.data.get("target_id")

        # Build action annotations from events up to current step
        self.unit_last_actions = {}
        for i in range(self._step):
            ev = self._timeline[i]
            if ev.kind == "action" and isinstance(ev.data, dict):
                payload = ev.data
                uid = payload.get("unit_id") or payload.get("healer_id") or ""
                atype = payload.get("type", "")
                if atype == "move":
                    dest = payload.get("dest") or payload.get("to") or {}
                    self.unit_last_actions[uid] = f"moved → ({dest.get('x')},{dest.get('y')})"
                elif atype == "attack":
                    dmg = payload.get("damage_dealt") or payload.get("damage_to_defender") or "?"
                    self.unit_last_actions[uid] = f"attacked {payload.get('target_id')} dmg={dmg}"
                elif atype == "heal":
                    self.unit_last_actions[uid] = f"healed {payload.get('target_id')}"
                elif atype == "wait":
                    self.unit_last_actions[uid] = "waited"
                elif atype == "end_turn":
                    # Clear actions at turn boundary
                    self.unit_last_actions.clear()

        # Populate thoughts deque up to current step
        self.app.state.thoughts.clear()
        for step_idx in range(1, self._step + 1):
            for ts, team, text in self._thoughts_by_step.get(step_idx, []):
                self.app.state.thoughts.append((ts, team, text))

    # ---- GameScreen duck-type methods ----

    def open_unit_card(self, unit: dict[str, Any]) -> None:
        self.unit_card = UnitCard(
            unit,
            self.app.state.scenario_description,
            self.app.state.locale,
        )

    def _clear_range_overlay(self) -> None:
        self.range_overlay_unit = None
        self.range_move_tiles.clear()
        self.range_attack_tiles.clear()

    # ---- Screen interface ----

    async def on_enter(self, app: TUIApp) -> None:
        log.info("ReplayScreen: loaded %s (%d events)", self._replay_path, len(self._timeline))

    def render(self) -> RenderableType:
        lc = self.app.state.locale
        header_line = Text()
        header_line.append(f"▶ {t('replay.title', lc)}", style="bold magenta")
        header_line.append(f"  {self._scenario_name}", style="yellow bold")

        hints = Text()
        hints.append(
            f"{t('replay.nav_hints', lc)}   "
            f"Enter {t('replay.unit_card', lc)}   "
            f"{t('keys.tab_next', lc)}   "
            f"{t('keys.quit', lc)}",
            style="dim",
        )

        root = Layout()
        root.split_column(
            Layout(name="hdr", size=1),
            Layout(name="body"),
            Layout(name="ftr", size=1),
        )
        root["hdr"].update(header_line)
        root["body"].update(self._build_body())
        root["ftr"].update(hints)
        return root

    def _build_body(self) -> Layout:
        body = Layout()
        body.split_column(
            Layout(name="top", ratio=3),
            Layout(name="bottom", ratio=2),
        )
        body["top"].split_row(
            Layout(name="map", ratio=2),
            Layout(name="player", ratio=1),
        )
        body["bottom"].split_row(
            Layout(name="reasoning", ratio=2),
            Layout(name="step", ratio=1),
        )

        focused = self._panels[self._focus_idx]
        body["top"]["map"].update(self.map_panel.render(focused is self.map_panel))
        body["top"]["player"].update(
            self._panels[1].render(focused is self._panels[1])
        )
        body["bottom"]["reasoning"].update(
            self.reasoning_panel.render(focused is self.reasoning_panel)
        )
        body["bottom"]["step"].update(
            self._step_panel.render(focused is self._step_panel)
        )
        return body

    async def handle_key(self, key: str) -> Screen | None:
        # Unit card overlay — Esc/Enter/q close it. Left/right
        # cycles between units (same as GameScreen).
        if self.unit_card is not None:
            if key in ("esc", "enter", "q"):
                pos = self.unit_card.unit.get("pos") or {}
                self.map_panel.cx = int(pos.get("x", self.map_panel.cx))
                self.map_panel.cy = int(pos.get("y", self.map_panel.cy))
                self.unit_card = None
            elif key in ("left", "h"):
                self.unit_card.navigate(-1)
            elif key in ("right", "l"):
                self.unit_card.navigate(1)
            return None

        # Tab between panels — always available.
        # The key reader returns "\t" for the Tab key.
        if key == "\t":
            self._focus_idx = (self._focus_idx + 1) % len(self._panels)
            return None

        # Quit back to lobby.
        if key == "q" or key == "esc":
            from silicon_pantheon.client.tui.screens.lobby import LobbyScreen
            return LobbyScreen(self.app)

        # Global step navigation — works regardless of focused panel.
        total = len(self._timeline)

        # Skip to next/prev action (always global).
        # Check backward FIRST — "S" and "a" go backward, "s" goes
        # forward. Checking "s" first would never catch "S" (case-
        # sensitive), but being explicit about order is clearer.
        if key == "a":
            prev_step = self._step - 1
            while prev_step > 0 and self._timeline[prev_step - 1].kind != "action":
                prev_step -= 1
            self._step = max(prev_step, 0)
            self._apply_step()
            return None
        if key == "s":
            next_step = self._step + 1
            while next_step <= total and self._timeline[next_step - 1].kind != "action":
                next_step += 1
            self._step = min(next_step, total)
            self._apply_step()
            return None

        # Home / End (always global).
        if key == "g" and self._focus_idx not in (0, 1):
            self._step = 0
            self._apply_step()
            return None
        if key in ("G", "shift-g"):
            self._step = total
            self._apply_step()
            return None

        # Arrow right/left for step nav — only when step or reasoning
        # panel is focused. When map/player panel is focused, these
        # keys are cursor movement and go to the panel handler below.
        if self._focus_idx not in (0, 1):
            if key in ("right", "l"):
                if self._step < total:
                    self._step += 1
                    self._apply_step()
                return None
            if key in ("left", "h"):
                if self._step > 0:
                    self._step -= 1
                    self._apply_step()
                return None

        # Delegate to focused panel (cursor movement, Enter for unit
        # card, scroll, range overlay, etc.).
        result = await self._panels[self._focus_idx].handle_key(key)
        return result

    async def tick(self) -> None:
        # No polling needed — replay is fully offline
        pass
