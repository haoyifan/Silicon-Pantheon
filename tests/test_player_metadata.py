"""Round-trip and validation for PlayerMetadata."""

from __future__ import annotations

import pytest

from clash_of_robots.shared.player_metadata import PlayerMetadata


def test_roundtrip() -> None:
    meta = PlayerMetadata(
        display_name="pringles-claude",
        kind="ai",
        provider="anthropic",
        model="claude-opus-4-6",
    )
    d = meta.to_dict()
    assert PlayerMetadata.from_dict(d) == meta


def test_minimal_dict() -> None:
    meta = PlayerMetadata.from_dict({"display_name": "bob", "kind": "human"})
    assert meta.display_name == "bob"
    assert meta.kind == "human"
    assert meta.provider is None
    assert meta.version == "1"


def test_rejects_missing_display_name() -> None:
    with pytest.raises(ValueError):
        PlayerMetadata.from_dict({"display_name": "  ", "kind": "ai"})


def test_rejects_bad_kind() -> None:
    with pytest.raises(ValueError):
        PlayerMetadata.from_dict({"display_name": "a", "kind": "robot"})
