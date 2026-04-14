"""Tests for the scenario schema_version gate."""

from __future__ import annotations

import pytest

from clash_of_odin.server.engine.scenarios import (
    SUPPORTED_SCHEMA_VERSION,
    UnsupportedSchemaVersion,
    build_state,
)


def _minimal_cfg(**overrides) -> dict:
    base = {
        "board": {
            "width": 3,
            "height": 3,
            "terrain": [],
            "forts": [],
        },
        "armies": {
            "blue": [{"class": "knight", "pos": {"x": 0, "y": 0}}],
            "red": [{"class": "knight", "pos": {"x": 2, "y": 2}}],
        },
        "rules": {"max_turns": 10, "first_player": "blue"},
    }
    base.update(overrides)
    return base


def test_missing_schema_version_loads_as_v1() -> None:
    state = build_state(_minimal_cfg())
    assert state.turn == 1


def test_explicit_v1_loads() -> None:
    state = build_state(_minimal_cfg(schema_version=1))
    assert state.turn == 1


def test_future_version_refuses() -> None:
    with pytest.raises(UnsupportedSchemaVersion) as excinfo:
        build_state(_minimal_cfg(schema_version=SUPPORTED_SCHEMA_VERSION + 1))
    assert "Upgrade the server" in str(excinfo.value)
