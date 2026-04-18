"""Interactive step-by-step match replayer (DEPRECATED).

.. deprecated::
    Use ``silicon-join`` → lobby → ``w`` (replay) instead.
    The TUI replay reuses the full GameScreen rendering (unit cards,
    HP bars, combat highlights, scrollable reasoning) while this
    tool uses a simpler Rich layout. ``silicon-play`` will be removed
    in a future release.

Usage:
    silicon-play runs/20260412T143022_01_tiny_skirmish

Reconstructs the match from replay.jsonl and walks the user through it:
- Starts from the scenario's initial state.
- Agent_thought / coach_message / error events: shown in the side panel
  with the board unchanged.
- Action events: the action is re-applied to the state so the board
  visibly updates alongside the description.
- All steps are precomputed as GameState snapshots, so backward
  navigation is as cheap as forward.

Controls (single keypress on a TTY; line-input fallback otherwise):
    Enter or j   advance one step
    k            go back one step
    s            skip forward to the next action event
    q            quit
    Ctrl-C       quit
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path

from rich.console import Console, Group, RenderableType
from rich.panel import Panel
from rich.text import Text

from silicon_pantheon.match.replay_schema import (
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
from silicon_pantheon.renderer.board_view import render_board
from silicon_pantheon.renderer.sidebar import render_header, render_units_table
from silicon_pantheon.server.engine.rules import IllegalAction, apply
from silicon_pantheon.server.engine.scenarios import load_scenario
from silicon_pantheon.server.engine.state import GameState, Team


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
        dmg = payload.get("damage_dealt") or payload.get("damage_to_defender")
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


def _frame(state: GameState, ev: ReplayEvent | None, step: int, total: int) -> RenderableType:
    from rich.layout import Layout

    header = render_header(state)
    board = Panel(render_board(state), title="Board", border_style="dim")
    units = Panel(render_units_table(state), title="Units", border_style="dim")
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
    # Fixed layout: header (1 line), body (map left + units right),
    # step panel at the bottom. Nothing gets pushed off screen.
    root = Layout()
    root.split_column(
        Layout(name="hdr", size=1),
        Layout(name="body"),
        Layout(name="step", size=6),
    )
    root["hdr"].update(header)
    body = Layout()
    body.split_row(
        Layout(name="map", ratio=2),
        Layout(name="units", ratio=1),
    )
    body["map"].update(board)
    body["units"].update(units)
    root["body"].update(body)
    root["step"].update(step_panel)
    return root


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
    """Block on one keypress. Return normalized command tokens.

    When stdin is an interactive POSIX TTY, reads a single character via
    termios cbreak mode — no Enter needed. Otherwise (piped stdin, tests,
    non-POSIX) falls back to line-oriented `input()` so you can still
    drive the replayer by piping commands.

    Return values:
      ""   Enter was pressed (advance)
      "q"  quit, EOF, or Ctrl-C
      other single lowercase character: the key that was pressed
    """
    # Non-TTY fallback: line input. Keeps tests and pipe-driven usage working.
    if not sys.stdin.isatty():
        try:
            return input().strip().lower()
        except (EOFError, KeyboardInterrupt):
            return "q"

    try:
        import termios
        import tty
    except ImportError:
        # Not a POSIX terminal (e.g. Windows without WSL) — line input.
        try:
            return input().strip().lower()
        except (EOFError, KeyboardInterrupt):
            return "q"

    fd = sys.stdin.fileno()
    try:
        old_settings = termios.tcgetattr(fd)
    except termios.error:
        try:
            return input().strip().lower()
        except (EOFError, KeyboardInterrupt):
            return "q"

    try:
        # cbreak disables line buffering and echo but preserves signal
        # handling, so Ctrl-C still raises KeyboardInterrupt.
        tty.setcbreak(fd)
        try:
            ch = sys.stdin.read(1)
        except KeyboardInterrupt:
            return "q"
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)

    if not ch:  # EOF
        return "q"
    if ch in ("\r", "\n"):
        return ""  # Enter → advance
    if ch == "\x03":  # defensive: Ctrl-C without signal delivery
        return "q"
    return ch.lower()


def _build_snapshots(
    initial_state: GameState,
    timeline: list[ReplayEvent],
    console: Console,
) -> list[GameState]:
    """Precompute per-step GameState snapshots.

    `snapshots[0]` is the initial state (before any event). `snapshots[i]`
    for i>=1 is the state AFTER event timeline[i-1] has been applied.
    Non-action events don't mutate state, so their snapshot equals the
    previous one (cheap deepcopy either way; we do ~100-300 per match).

    Building all snapshots upfront makes backward navigation (j key) O(1)
    instead of having to re-replay from the start each time.
    """
    snapshots: list[GameState] = [copy.deepcopy(initial_state)]
    state = copy.deepcopy(initial_state)
    for ev in timeline:
        if ev.kind == "action":
            _apply_action_event(state, ev, console)
        snapshots.append(copy.deepcopy(state))
    return snapshots


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
        initial_state = load_scenario(meta.scenario)
    except Exception as e:
        print(f"failed to load scenario {meta.scenario!r}: {e}", file=sys.stderr)
        return 2
    if meta.max_turns:
        initial_state.max_turns = meta.max_turns

    # Skip the match_start event itself; the user has already "seen" it
    # implicitly via the initial state.
    timeline = [ev for ev in events if ev.kind != "match_start"]
    total = len(timeline)

    console = Console()

    # Precompute snapshots so the user can jump backward and forward.
    snapshots = _build_snapshots(initial_state, timeline, console)

    def _render(step: int) -> None:
        """Render the state after `step` events have been applied.

        step==0 is the pre-match initial board; step>=1 is the state AFTER
        timeline[step-1]. The event shown in the step panel is the one
        that produced this state (None for step 0).
        """
        state = snapshots[step]
        ev = timeline[step - 1] if step >= 1 else None
        console.clear()
        console.print(_frame(state, ev, step, total))
        console.print(
            Text(
                "[Enter/j] next   [k] previous   [s] skip to next action   [q] quit",
                style="dim",
            )
        )

    step = 0
    _render(step)

    while True:
        cmd = _read_command()
        if cmd == "q":
            break
        if cmd in ("", "j"):
            # Advance one step (or stay put at the end).
            if step < total:
                step += 1
            _render(step)
            continue
        if cmd == "k":
            # Go back one step (or stay put at the start).
            if step > 0:
                step -= 1
            _render(step)
            continue
        if cmd == "s":
            # Skip forward until the new `step` lands on an action event.
            next_step = step + 1
            while next_step <= total and timeline[next_step - 1].kind != "action":
                next_step += 1
            step = min(next_step, total)
            _render(step)
            continue
        # Unknown command — redraw without changing position.
        _render(step)

    console.print(Text("\n(end of replay)", style="bold green"))
    return 0


def _resolve_replay_path(user_path: Path) -> Path | None:
    """DWIM resolver: the positional arg can be either a jsonl file or
    a run directory.

    Two formats exist in the wild:

      1. Offline runs written by `silicon-match` / `run_match.py`:
         `runs/<ts>_<scenario>/replay.jsonl` — the conventional
         one-dir-per-run layout.
      2. Networked matches downloaded from the server by the TUI's
         post-match screen: flat files at
         `~/.silicon-pantheon/replays/<room_id>.jsonl`.

    Accept both. A file → use verbatim. A directory → look for
    replay.jsonl inside. Returns None if nothing usable is found.
    """
    if user_path.is_file():
        return user_path
    if user_path.is_dir():
        candidate = user_path / "replay.jsonl"
        if candidate.is_file():
            return candidate
    return None


def main() -> int:
    import warnings
    warnings.warn(
        "silicon-play is deprecated. Use silicon-join → lobby → w (replay) "
        "for the full-featured TUI replay with unit cards, combat highlights, "
        "and scrollable reasoning.",
        DeprecationWarning,
        stacklevel=2,
    )
    print(
        "\033[33m⚠ silicon-play is deprecated. "
        "Use silicon-join → lobby → w for the full TUI replay.\033[0m\n",
        file=sys.stderr,
    )
    p = argparse.ArgumentParser(
        description=(
            "DEPRECATED: use silicon-join → lobby → w instead. "
            "Interactive step-through replayer. "
            "Keys: Enter/j=next, k=prev, s=skip to next action, q=quit."
        )
    )
    p.add_argument(
        "path",
        nargs="?",
        type=Path,
        default=None,
        help=(
            "replay.jsonl file OR a run directory containing one. "
            "Accepts both offline runs (runs/<ts>_<scenario>/) and "
            "downloaded networked replays "
            "(~/.silicon-pantheon/replays/<room_id>.jsonl)."
        ),
    )
    p.add_argument(
        "--replay",
        type=Path,
        default=None,
        help="explicit path to replay.jsonl (overrides the positional arg)",
    )
    args = p.parse_args()
    if args.replay is not None:
        replay_path: Path | None = args.replay if args.replay.is_file() else None
        if replay_path is None:
            print(f"replay file not found: {args.replay}", file=sys.stderr)
            return 2
    elif args.path is not None:
        replay_path = _resolve_replay_path(args.path)
        if replay_path is None:
            print(
                f"no replay found at {args.path} — expected either a "
                "replay.jsonl file or a directory containing one",
                file=sys.stderr,
            )
            return 2
    else:
        p.error("provide a path to a replay.jsonl file or run directory")
        return 2
    return interactive_replay(replay_path)


if __name__ == "__main__":
    raise SystemExit(main())
