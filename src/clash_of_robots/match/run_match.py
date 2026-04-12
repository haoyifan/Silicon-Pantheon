"""Match orchestrator: spin up a session, two providers, and run the game.

Orchestrator-driven turn handoff: after each agent's `decide_turn` returns, the
state's `active_player` has already flipped (because decide_turn ends by calling
the `end_turn` tool). We loop until `status` is GAME_OVER.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from clash_of_robots.harness.providers import Provider, make_provider
from clash_of_robots.server.engine.scenarios import load_scenario
from clash_of_robots.server.engine.state import GameStatus, Team
from clash_of_robots.server.session import new_session
from clash_of_robots.server.tools import call_tool


def run_match(
    game: str,
    blue: Provider,
    red: Provider,
    *,
    max_turns: int | None = None,
    replay_path: Path | None = None,
    render: bool = False,
    verbose: bool = True,
) -> dict:
    state = load_scenario(game)
    if max_turns is not None:
        state.max_turns = max_turns
    session = new_session(state, replay_path=replay_path)

    blue.on_match_start(session, Team.BLUE)
    red.on_match_start(session, Team.RED)

    tui = None
    if render:
        try:
            from clash_of_robots.renderer.tui import TUIRenderer
        except ImportError:
            tui = None
        else:
            tui = TUIRenderer(session)
            tui.start()

    start = time.time()
    safety_counter = 0
    try:
        while session.state.status is GameStatus.IN_PROGRESS:
            active = blue if session.state.active_player is Team.BLUE else red
            viewer = session.state.active_player
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
            if verbose:
                print(
                    f"[T{session.state.turn}] {viewer.value} done in {time.time() - t0:.2f}s "
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

    result = {
        "winner": session.state.winner.value if session.state.winner else None,
        "turns": session.state.turn,
        "duration_s": time.time() - start,
        "blue_survivors": len(session.state.units_of(Team.BLUE)),
        "red_survivors": len(session.state.units_of(Team.RED)),
    }
    if verbose:
        print(f"\n=== match result: {result}")
    return result


def main() -> int:
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
    args = p.parse_args()

    blue = make_provider(args.blue, seed=args.seed, strategy_path=args.blue_strategy)
    red = make_provider(args.red, seed=args.seed, strategy_path=args.red_strategy)

    run_match(
        game=args.game,
        blue=blue,
        red=red,
        max_turns=args.max_turns,
        replay_path=args.replay,
        render=args.render,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
