"""End-to-end: random vs random match plays to completion."""

from __future__ import annotations

from clash_of_robots.harness.providers import make_provider
from clash_of_robots.match.run_match import run_match


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


def test_multiple_seeds():
    outcomes = []
    for seed in range(5):
        blue = make_provider("random", seed=seed)
        red = make_provider("random", seed=seed + 100)
        result = run_match(game="01_tiny_skirmish", blue=blue, red=red, max_turns=25, verbose=False)
        outcomes.append(result["winner"])
    # Sanity: across 5 seeds we should see at least one non-None outcome
    assert any(o is not None for o in outcomes)
