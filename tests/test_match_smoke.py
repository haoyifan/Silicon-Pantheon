"""End-to-end: random vs random match plays to completion."""

from __future__ import annotations

from pathlib import Path

from clash_of_robots.harness.providers import make_provider
from clash_of_robots.match.run_match import _make_run_dir, run_match


def test_random_vs_random_completes():
    blue = make_provider("random", seed=42)
    red = make_provider("random", seed=42)
    result = run_match(
        game="01_tiny_skirmish",
        blue=blue,
        red=red,
        max_turns=25,
        verbose=False,
    )
    assert result["turns"] <= 25
    # Either someone won, or draw at max turns
    assert result["winner"] in {"blue", "red", None}


def test_make_run_dir_is_unique_and_fs_safe(tmp_path: Path) -> None:
    # Two calls in the same second must not collide.
    d1 = _make_run_dir(tmp_path, "01_tiny_skirmish")
    d2 = _make_run_dir(tmp_path, "01_tiny_skirmish")
    assert d1.exists() and d2.exists()
    assert d1 != d2
    # Scenario name containing unsafe chars should be sanitized.
    d3 = _make_run_dir(tmp_path, "weird/scenario name")
    assert "/" not in d3.name
    assert " " not in d3.name


def test_run_match_writes_replay_into_run_dir(tmp_path: Path) -> None:
    run_dir = _make_run_dir(tmp_path, "01_tiny_skirmish")
    blue = make_provider("random", seed=1)
    red = make_provider("random", seed=2)
    result = run_match(
        game="01_tiny_skirmish",
        blue=blue,
        red=red,
        max_turns=25,
        verbose=False,
        lessons_dir=None,
        run_dir=run_dir,
    )
    assert result["run_dir"] == str(run_dir)
    assert (run_dir / "replay.jsonl").exists()


def test_multiple_seeds():
    outcomes = []
    for seed in range(5):
        blue = make_provider("random", seed=seed)
        red = make_provider("random", seed=seed + 100)
        result = run_match(game="01_tiny_skirmish", blue=blue, red=red, max_turns=25, verbose=False)
        outcomes.append(result["winner"])
    # Sanity: across 5 seeds we should see at least one non-None outcome
    assert any(o is not None for o in outcomes)
