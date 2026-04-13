"""Match orchestrator: spin up a session, two providers, and run the game.

Orchestrator-driven turn handoff: after each agent's `decide_turn` returns, the
state's `active_player` has already flipped (because decide_turn ends by calling
the `end_turn` tool). We loop until `status` is GAME_OVER.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import logging
import re
import sys
import time
from pathlib import Path

from clash_of_robots.harness.providers import Provider, make_provider
from clash_of_robots.lessons import LessonStore
from clash_of_robots.server.engine.scenarios import load_scenario
from clash_of_robots.server.engine.state import GameStatus, Team
from clash_of_robots.server.session import new_session
from clash_of_robots.server.tools import call_tool

_FS_SAFE = re.compile(r"[^A-Za-z0-9_.-]+")


def _make_run_dir(parent: Path, scenario: str) -> Path:
    """Create and return a new per-match run directory under `parent`.

    Naming: {timestamp}_{scenario}. Timestamp is a filesystem-safe local
    timestamp (YYYYMMDDTHHMMSS). If the directory already exists (two
    matches started in the same second), appends -2, -3, ... until free.
    """
    ts = _dt.datetime.now().strftime("%Y%m%dT%H%M%S")
    safe_scenario = _FS_SAFE.sub("-", scenario).strip("-") or "match"
    base = parent / f"{ts}_{safe_scenario}"
    candidate = base
    i = 2
    while candidate.exists():
        candidate = parent / f"{ts}_{safe_scenario}-{i}"
        i += 1
    candidate.mkdir(parents=True, exist_ok=False)
    return candidate


def run_match(
    game: str,
    blue: Provider,
    red: Provider,
    *,
    max_turns: int | None = None,
    replay_path: Path | None = None,
    render: bool = False,
    verbose: bool = True,
    coach_file_blue: Path | None = None,
    coach_file_red: Path | None = None,
    lessons_dir: Path | None = Path("lessons"),
    run_dir: Path | None = None,
    thoughts_height: int | None = None,
) -> dict:
    state = load_scenario(game)
    if max_turns is not None:
        state.max_turns = max_turns
    # If a run directory is provided and the caller didn't pick an explicit
    # replay path, default the replay into that directory so all artifacts
    # from one match live together.
    if run_dir is not None and replay_path is None:
        replay_path = run_dir / "replay.jsonl"
    thoughts_log_path = run_dir / "thoughts.log" if run_dir is not None else None
    session = new_session(
        state,
        replay_path=replay_path,
        scenario=game,
        thoughts_log_path=thoughts_log_path,
    )

    blue.on_match_start(session, Team.BLUE)
    red.on_match_start(session, Team.RED)

    # Coach file watchers (optional).
    coaches = []
    if coach_file_blue is not None:
        from clash_of_robots.renderer.coach_input import CoachFileWatcher

        coaches.append(CoachFileWatcher(coach_file_blue, Team.BLUE))
    if coach_file_red is not None:
        from clash_of_robots.renderer.coach_input import CoachFileWatcher

        coaches.append(CoachFileWatcher(coach_file_red, Team.RED))

    tui = None
    if render:
        try:
            from clash_of_robots.renderer.tui import TUIRenderer
        except ImportError:
            tui = None
        else:
            tui = TUIRenderer(session, thoughts_height=thoughts_height)
            tui.start()
            # Real-time updates: refresh after each action as the agent calls tools.
            session.action_hooks.append(lambda _s, _r: tui.refresh())

    start = time.time()
    safety_counter = 0
    try:
        while session.state.status is GameStatus.IN_PROGRESS:
            # Poll coach files before each turn.
            for coach in coaches:
                msgs = coach.poll(session)
                if msgs and verbose:
                    print(f"[coach->{coach.team.value}] {len(msgs)} message(s)")

            active = blue if session.state.active_player is Team.BLUE else red
            viewer = session.state.active_player
            # Snapshot turn number BEFORE decide_turn runs. decide_turn ends
            # by calling end_turn, which may bump state.turn (when the second
            # player finishes a round), so reading state.turn afterwards is
            # off-by-one for that player.
            turn_at_start = session.state.turn
            t0 = time.time()
            try:
                active.decide_turn(session, viewer)
            except Exception as e:
                if verbose:
                    print(f"[{viewer.value}] provider error: {e}", file=sys.stderr)
                # Force-end the turn to keep the match moving.
                try:
                    for u in session.state.units_of(viewer):
                        if u.status.value == "moved":
                            call_tool(session, viewer, "wait", {"unit_id": u.id})
                    call_tool(session, viewer, "end_turn", {})
                except Exception:
                    break
            # Per-turn summary prints are redundant with (and visually fight)
            # the TUI's header + units table, so skip them in render mode.
            if verbose and not render:
                print(
                    f"[T{turn_at_start} {viewer.value} half] done in "
                    f"{time.time() - t0:.2f}s "
                    f"(units: B={len(session.state.units_of(Team.BLUE))} "
                    f"R={len(session.state.units_of(Team.RED))})"
                )
            if tui is not None:
                tui.refresh()
            safety_counter += 1
            if safety_counter > 2000:
                # defensive: no game should ever need this many half-turns
                print("safety cap reached; aborting", file=sys.stderr)
                break
    finally:
        if tui is not None:
            tui.stop()

    blue.on_match_end(session, Team.BLUE)
    red.on_match_end(session, Team.RED)

    if session.replay is not None:
        session.replay.close()
    if session.thoughts_log is not None:
        session.thoughts_log.close()

    # Post-match reflections: ask each provider for a lesson, persist via
    # LessonStore. Providers that don't implement summarize_match return
    # None and are silently skipped.
    lesson_paths: list[str] = []
    if lessons_dir is not None:
        store = LessonStore(lessons_dir)
        for provider, team in ((blue, Team.BLUE), (red, Team.RED)):
            try:
                lesson = provider.summarize_match(session, team, scenario=game)
            except Exception as e:
                if verbose:
                    print(
                        f"[{team.value}] summarize_match error: {e}", file=sys.stderr
                    )
                lesson = None
            if lesson is None:
                continue
            path = store.save(lesson)
            lesson_paths.append(str(path))
            if verbose:
                print(f"[{team.value}] lesson saved: {path}")

    result = {
        "winner": session.state.winner.value if session.state.winner else None,
        "turns": session.state.turn,
        "duration_s": time.time() - start,
        "blue_survivors": len(session.state.units_of(Team.BLUE)),
        "red_survivors": len(session.state.units_of(Team.RED)),
        "lessons": lesson_paths,
        "run_dir": str(run_dir) if run_dir is not None else None,
    }
    if verbose:
        print(f"\n=== match result: {result}")
    return result


def main() -> int:
    # Silence asyncio's "Loop ... that handles pid N is closed" warnings.
    # They come from the child-process watcher when we open a fresh event
    # loop per agent turn (via asyncio.run); the preceding turn's pid is
    # still tracked by the prior loop. Harmless, but noisy on the TUI.
    logging.getLogger("asyncio").setLevel(logging.ERROR)

    p = argparse.ArgumentParser(description="Run one Clash Of Robots match")
    p.add_argument("--game", default="01_tiny_skirmish")
    p.add_argument(
        "--blue", default="random", help="provider spec (e.g. random, claude-sonnet-4-6)"
    )
    p.add_argument("--red", default="random")
    p.add_argument("--blue-strategy", default=None, help="path to strategy.md")
    p.add_argument("--red-strategy", default=None)
    p.add_argument("--max-turns", type=int, default=None)
    p.add_argument("--replay", type=Path, default=None)
    p.add_argument("--render", action="store_true")
    p.add_argument("--seed", type=int, default=None, help="seed for random providers")
    p.add_argument(
        "--thoughts-height",
        type=int,
        default=None,
        help="rows to reserve for the agent-reasoning panel in --render mode "
        "(default: 12)",
    )
    p.add_argument(
        "--coach-file-blue",
        type=Path,
        default=None,
        help="path to a text file; append lines during the match to advise blue",
    )
    p.add_argument("--coach-file-red", type=Path, default=None)
    p.add_argument(
        "--runs-dir",
        type=Path,
        default=Path("runs"),
        help="parent directory under which a per-match folder is auto-created "
        "(default: ./runs). The folder gathers replay.jsonl + thoughts.log + "
        "future artifacts from one match.",
    )
    p.add_argument(
        "--no-run-dir",
        action="store_true",
        help="skip creating the per-match run folder",
    )
    p.add_argument(
        "--lessons-dir",
        type=Path,
        default=Path("lessons"),
        help="directory for post-match lesson files (default: ./lessons)",
    )
    p.add_argument(
        "--no-lessons",
        action="store_true",
        help="skip writing lessons AND injecting prior ones into the system prompt",
    )
    args = p.parse_args()

    # --no-lessons disables both the producer (run_match) and the consumer
    # (provider-side prompt injection) by threading None through both.
    effective_lessons_dir = None if args.no_lessons else args.lessons_dir

    # Auto-create a per-match run folder unless the user opted out.
    run_dir: Path | None = None
    if not args.no_run_dir:
        run_dir = _make_run_dir(args.runs_dir, args.game)
        print(f"run directory: {run_dir}")
        print(f"  tail thoughts with: less +F {run_dir / 'thoughts.log'}")

    blue = make_provider(
        args.blue,
        seed=args.seed,
        strategy_path=args.blue_strategy,
        lessons_dir=effective_lessons_dir,
    )
    red = make_provider(
        args.red,
        seed=args.seed,
        strategy_path=args.red_strategy,
        lessons_dir=effective_lessons_dir,
    )

    run_match(
        game=args.game,
        blue=blue,
        red=red,
        max_turns=args.max_turns,
        replay_path=args.replay,
        render=args.render,
        coach_file_blue=args.coach_file_blue,
        coach_file_red=args.coach_file_red,
        lessons_dir=effective_lessons_dir,
        run_dir=run_dir,
        thoughts_height=args.thoughts_height,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
