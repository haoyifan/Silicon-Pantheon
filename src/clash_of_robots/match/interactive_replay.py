"""Interactive step-by-step match replayer.

Usage:
    clash-play runs/20260412T143022_01_tiny_skirmish

Reconstructs the match from replay.jsonl:
- Starts from the scenario's initial state.
- Each Enter press advances one replay event.
- If the event is an agent_thought / coach_message / error: the board is
  unchanged; the current reasoning is shown in the side panel.
- If the event is an action: the action is applied to the state first so
  the board updates, then the step panel describes what happened.

Commands at the prompt:
    <Enter>  advance one step
    s        skip forward to the next action (past any thoughts)
    q        quit
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from rich.console import Console, Group
from rich.panel import Panel
from rich.text import Text

from clash_of_robots.match.replay_schema import (
    AgentThought,
    CoachMessage,
    ErrorPayload,
    ForcedEndTurn,
    MatchStart,
    ReplayEvent,
    UnreconstructibleAction,
    action_from_payload,
    parse_event,
)
from clash_of_robots.renderer.board_view import render_board
from clash_of_robots.renderer.sidebar import render_header, render_units_table
from clash_of_robots.server.engine.rules import IllegalAction, apply
from clash_of_robots.server.engine.scenarios import load_scenario
from clash_of_robots.server.engine.state import GameState, Team


# ---- loading ----


def _load_events(replay_path: Path) -> list[ReplayEvent]:
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


def _find_match_start(events: list[ReplayEvent]) -> MatchStart | None:
    for ev in events:
        if ev.kind == "match_start" and isinstance(ev.data, MatchStart):
            return ev.data
    return None


# ---- step description ----


def _describe_event(ev: ReplayEvent) -> Text:
    """One-line + optional body description of what this step represents."""
    t = Text()
    if ev.kind == "agent_thought" and isinstance(ev.data, AgentThought):
        style = "cyan" if ev.data.team == "blue" else "red"
        t.append(f"T{ev.turn} [{ev.data.team}] thought\n", style=style + " bold")
        t.append(ev.data.text)
        return t
    if ev.kind == "action" and isinstance(ev.data, dict):
        action_type = str(ev.data.get("type", "?"))
        by = ev.data.get("by")
        style = "cyan" if by == "blue" else "red" if by == "red" else "white"
        t.append(f"T{ev.turn} action: {action_type}\n", style=style + " bold")
        t.append(_action_detail(ev.data))
        return t
    if ev.kind == "coach_message" and isinstance(ev.data, CoachMessage):
        t.append(f"T{ev.turn} coach -> {ev.data.to}\n", style="yellow bold")
        t.append(ev.data.text)
        return t
    if ev.kind == "forced_end_turn" and isinstance(ev.data, ForcedEndTurn):
        t.append(f"T{ev.turn} forced end_turn ({ev.data.team})", style="yellow")
        return t
    if isinstance(ev.data, ErrorPayload):
        t.append(f"T{ev.turn} {ev.kind} ({ev.data.team})\n", style="red bold")
        t.append(ev.data.error)
        return t
    if ev.kind == "match_start" and isinstance(ev.data, MatchStart):
        t.append(f"Match start — scenario: {ev.data.scenario}", style="bold")
        return t
    t.append(f"T{ev.turn} {ev.kind}", style="magenta")
    return t


def _action_detail(payload: dict) -> str:
    t = payload.get("type")
    if t == "move":
        u = payload.get("unit_id")
        dest = payload.get("dest") or payload.get("to") or {}
        return f"{u} moves to ({dest.get('x')},{dest.get('y')})"
    if t == "attack":
        dmg = payload.get("damage_to_defender")
        counter = payload.get("counter_damage")
        kills = "killed target" if payload.get("defender_dies") else ""
        parts = [
            f"{payload.get('unit_id')} attacks {payload.get('target_id')}",
            f"damage={dmg}",
            f"counter={counter}",
        ]
        if kills:
            parts.append(kills)
        return " | ".join(parts)
    if t == "heal":
        return f"{payload.get('healer_id')} heals {payload.get('target_id')}"
    if t == "wait":
        return f"{payload.get('unit_id')} waits"
    if t == "end_turn":
        parts = [f"{payload.get('by')} ends turn"]
        if payload.get("winner"):
            parts.append(f"WINNER: {payload.get('winner')}")
        if payload.get("reason"):
            parts.append(f"reason={payload.get('reason')}")
        if payload.get("seized_at"):
            at = payload["seized_at"]
            parts.append(f"seized ({at.get('x')},{at.get('y')})")
        return " | ".join(parts)
    return json.dumps(payload, default=str)


# ---- rendering ----


def _frame(state: GameState, ev: ReplayEvent | None, step: int, total: int) -> Group:
    header = render_header(state)
    board = Panel(render_board(state), title="Board", border_style="dim")
    units = render_units_table(state)
    if ev is None:
        step_panel = Panel(
            Text("(press Enter to begin)", style="dim italic"),
            title=f"Step 0/{total}",
            border_style="bright_black",
        )
    else:
        step_panel = Panel(
            _describe_event(ev),
            title=f"Step {step}/{total}",
            border_style="bright_black",
        )
    return Group(header, board, units, step_panel)


# ---- main loop ----


def _apply_action_event(state: GameState, ev: ReplayEvent, console: Console) -> None:
    """Re-apply an action event to `state`. Logs errors but does not raise."""
    if ev.kind != "action" or not isinstance(ev.data, dict):
        return
    try:
        action = action_from_payload(ev.data)
    except UnreconstructibleAction as e:
        console.print(f"[red]skip unreconstructible action:[/red] {e}")
        return
    try:
        apply(state, action)
    except IllegalAction as e:
        console.print(f"[red]replay diverged at action:[/red] {e}")


def _read_command() -> str:
    """Block until the user presses Enter, returning any typed text."""
    try:
        return input().strip().lower()
    except (EOFError, KeyboardInterrupt):
        return "q"


def interactive_replay(replay_path: Path) -> int:
    events = _load_events(replay_path)
    if not events:
        print(f"no events in {replay_path}", file=sys.stderr)
        return 2

    meta = _find_match_start(events)
    if meta is None or meta.scenario is None:
        print(
            "replay is missing a match_start event with a scenario name; "
            "cannot reconstruct initial state",
            file=sys.stderr,
        )
        return 2

    try:
        state = load_scenario(meta.scenario)
    except Exception as e:
        print(f"failed to load scenario {meta.scenario!r}: {e}", file=sys.stderr)
        return 2
    if meta.max_turns:
        state.max_turns = meta.max_turns

    # Skip the match_start event itself; the user has already "seen" it
    # implicitly via the initial state.
    timeline = [ev for ev in events if ev.kind != "match_start"]
    total = len(timeline)

    console = Console()

    def _render(step: int, ev: ReplayEvent | None) -> None:
        console.clear()
        console.print(_frame(state, ev, step, total))
        console.print(
            Text(
                "[Enter] next   [s] skip to next action   [q] quit",
                style="dim",
            )
        )

    # Initial frame: state at t=0, no event yet.
    _render(0, None)

    i = 0
    while i < total:
        cmd = _read_command()
        if cmd == "q":
            break
        ev = timeline[i]
        # For action events we mutate state BEFORE rendering so the board
        # shows the effect of the action alongside its description.
        if ev.kind == "action":
            _apply_action_event(state, ev, console)
        _render(i + 1, ev)
        # `s` advances through thoughts/coach/errors until we land on an
        # action (which was just applied above if ev was itself an action).
        if cmd == "s":
            i += 1
            while i < total and timeline[i].kind != "action":
                ev = timeline[i]
                _render(i + 1, ev)
                i += 1
            # apply the action we paused on, if any
            if i < total:
                ev = timeline[i]
                _apply_action_event(state, ev, console)
                _render(i + 1, ev)
                i += 1
            continue
        i += 1

    # Final frame with a hint that playback is done.
    console.print(Text("\n(end of replay)", style="bold green"))
    return 0


def main() -> int:
    p = argparse.ArgumentParser(
        description="Interactive step-through replayer. Press Enter to advance."
    )
    p.add_argument(
        "run_dir",
        nargs="?",
        type=Path,
        default=None,
        help="run directory containing replay.jsonl",
    )
    p.add_argument(
        "--replay",
        type=Path,
        default=None,
        help="explicit path to replay.jsonl (overrides run_dir)",
    )
    args = p.parse_args()
    if args.replay is not None:
        replay_path = args.replay
    elif args.run_dir is not None:
        replay_path = args.run_dir / "replay.jsonl"
    else:
        p.error("provide a run_dir positional argument or --replay PATH")
        return 2
    if not replay_path.is_file():
        print(f"replay file not found: {replay_path}", file=sys.stderr)
        return 2
    return interactive_replay(replay_path)


if __name__ == "__main__":
    raise SystemExit(main())
