"""ASCII art frames per unit class — discovery, validation, animation.

Loader auto-discovers art/<class>/*.txt files at scenario load,
attaches them to UnitStats.art_frames; the TUI UnitCard cycles
through them at ART_FRAME_SECONDS per frame using a monotonic clock.
"""

from __future__ import annotations

import textwrap
import time

import pytest
from rich.console import Console

from clash_of_odin.client.tui.screens.room import ART_FRAME_SECONDS, UnitCard
from clash_of_odin.server.engine.scenarios import (
    DEFAULT_ART_MAX_COLS,
    _validate_art_frame,
    load_scenario,
)


def test_art_validation_accepts_in_bounds_frame():
    _validate_art_frame("hello\nworld", "test", 10, 5)


def test_art_validation_rejects_too_wide():
    with pytest.raises(ValueError, match="exceeds max_cols"):
        _validate_art_frame("x" * 100, "test", 80, 30)


def test_art_validation_rejects_too_tall():
    with pytest.raises(ValueError, match="exceeds max_rows"):
        _validate_art_frame("\n" * 50, "test", 80, 30)


def test_jttw_art_loads_for_all_units():
    """Every JTTW unit class should have its frames attached after
    load_scenario."""
    state = load_scenario("journey_to_the_west")
    expected = {
        "tang_monk", "sun_wukong", "zhu_bajie", "sha_wujing",
        "bai_long_ma", "demon_king", "bull_demon", "white_bone_demon",
        "spider_spirit", "skeleton",
    }
    seen_with_art = {
        u.class_ for u in state.units.values() if u.stats.art_frames
    }
    missing = expected - seen_with_art
    assert not missing, f"missing art for: {missing}"
    # Each shipped unit has at least one frame and ≤ default max cols.
    for u in state.units.values():
        for frame in u.stats.art_frames:
            for line in frame.split("\n"):
                assert len(line) <= DEFAULT_ART_MAX_COLS


def test_unit_card_renders_art_when_present():
    card = UnitCard(
        unit={"id": "u_b_x_1", "owner": "blue", "class": "x"},
        class_spec={
            "display_name": "Demo",
            "art_frames": ["A R T\n=====\n=====", "B R T\n=====\n====="],
        },
    )
    console = Console(record=True, width=60)
    console.print(card.render())
    out = console.export_text()
    # First frame is selected on the first render (elapsed ≈ 0).
    assert "A R T" in out
    assert "B R T" not in out


def test_unit_card_advances_to_next_frame_after_window():
    card = UnitCard(
        unit={"id": "u_b_x_1", "owner": "blue", "class": "x"},
        class_spec={"art_frames": ["FRAME0", "FRAME1"]},
    )
    console = Console(record=True, width=40)
    console.print(card.render())  # primes _opened_at
    # Force the clock backward by sliding _opened_at into the past.
    card._opened_at = time.monotonic() - (ART_FRAME_SECONDS + 0.1)
    console = Console(record=True, width=40)
    console.print(card.render())
    out = console.export_text()
    assert "FRAME1" in out
