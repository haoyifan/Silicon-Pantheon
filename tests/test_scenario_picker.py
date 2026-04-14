"""Scenario picker smoke tests — navigation + preview render."""

from __future__ import annotations

import asyncio

from rich.console import Console

from clash_of_odin.client.tui.screens.scenario_picker import ScenarioPicker


class _FakeClient:
    """Returns a canned describe_scenario payload for known names."""

    def __init__(self, payloads: dict[str, dict]) -> None:
        self.payloads = payloads
        self.calls: list[tuple[str, dict]] = []

    async def call(self, tool: str, **kwargs) -> dict:
        self.calls.append((tool, kwargs))
        if tool == "describe_scenario":
            name = kwargs.get("name")
            if name in self.payloads:
                return {"ok": True, **self.payloads[name]}
        return {"ok": False, "error": {"message": "unknown"}}


def _sample_payload(name: str, w: int = 4, h: int = 4) -> dict:
    return {
        "name": name.replace("_", " ").title(),
        "description": f"Stub description for {name}.",
        "board": {
            "width": w, "height": h,
            "terrain": [{"x": 1, "y": 1, "type": "forest"}],
            "forts": [{"x": 0, "y": 0, "owner": "blue"}],
        },
        "armies": {
            "blue": [{"class": "knight", "pos": {"x": 0, "y": 1}}],
            "red":  [{"class": "archer", "pos": {"x": 3, "y": 2}}],
        },
        "rules": {"max_turns": 20},
        "unit_classes": {
            "knight": {"hp_max": 30, "atk": 8, "defense": 7, "res": 2,
                       "spd": 3, "move": 3, "rng_min": 1, "rng_max": 1,
                       "tags": ["melee"]},
            "archer": {"hp_max": 18, "atk": 9, "defense": 3, "res": 3,
                       "spd": 5, "move": 4, "rng_min": 2, "rng_max": 3,
                       "tags": ["ranged"]},
        },
        "terrain_types": {
            "plain": {}, "forest": {}, "mountain": {}, "fort": {},
        },
        "win_conditions": [{"type": "seize_enemy_fort"}],
    }


def test_picker_renders_preview_of_current_scenario():
    client = _FakeClient({
        "alpha": _sample_payload("alpha"),
        "beta":  _sample_payload("beta", w=6, h=6),
    })
    picker = ScenarioPicker(
        scenarios=["alpha", "beta"],
        current="alpha",
        client=client,
        on_confirm=lambda v: None,  # type: ignore[arg-type]
    )
    asyncio.run(picker.prefetch_current())
    console = Console(record=True, width=100)
    console.print(picker.render())
    out = console.export_text()
    assert "alpha" in out.lower() or "Alpha" in out
    assert "beta" in out.lower() or "Beta" in out
    # Description + win conditions shown for the highlighted one.
    assert "Stub description for alpha" in out
    assert "seize" in out.lower()


def test_picker_arrows_cycle_scenarios():
    client = _FakeClient({n: _sample_payload(n) for n in ["a", "b", "c"]})
    picker = ScenarioPicker(
        scenarios=["a", "b", "c"],
        current="a",
        client=client,
        on_confirm=lambda v: None,  # type: ignore[arg-type]
    )
    asyncio.run(picker.prefetch_current())
    asyncio.run(picker.handle_key("down"))
    assert picker._current_name() == "b"
    asyncio.run(picker.handle_key("down"))
    assert picker._current_name() == "c"
    asyncio.run(picker.handle_key("up"))
    assert picker._current_name() == "b"


def test_picker_tab_toggles_list_vs_map_focus():
    client = _FakeClient({"a": _sample_payload("a")})
    picker = ScenarioPicker(
        scenarios=["a"],
        current="a",
        client=client,
        on_confirm=lambda v: None,  # type: ignore[arg-type]
    )
    asyncio.run(picker.prefetch_current())
    assert picker._list_focused is True
    asyncio.run(picker.handle_key("\t"))
    assert picker._list_focused is False
    # Now arrows move the map cursor, not the list selection.
    cx, cy = picker.cursor
    asyncio.run(picker.handle_key("right"))
    assert picker.cursor != (cx, cy)


def test_picker_enter_in_list_confirms_selection():
    picked: list[str] = []

    async def on_confirm(name: str) -> None:
        picked.append(name)

    client = _FakeClient({"a": _sample_payload("a"), "b": _sample_payload("b")})
    picker = ScenarioPicker(
        scenarios=["a", "b"],
        current="a",
        client=client,
        on_confirm=on_confirm,
    )
    asyncio.run(picker.prefetch_current())
    asyncio.run(picker.handle_key("down"))  # → b
    close = asyncio.run(picker.handle_key("enter"))
    assert close is True
    assert picked == ["b"]


def test_picker_esc_cancels_without_selecting():
    picked: list[str] = []

    async def on_confirm(name: str) -> None:
        picked.append(name)

    client = _FakeClient({"a": _sample_payload("a")})
    picker = ScenarioPicker(
        scenarios=["a"],
        current="a",
        client=client,
        on_confirm=on_confirm,
    )
    asyncio.run(picker.prefetch_current())
    close = asyncio.run(picker.handle_key("esc"))
    assert close is True
    assert picked == []
