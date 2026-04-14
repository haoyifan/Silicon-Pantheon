"""Game screen panel framework + map cursor + coach panel."""

from __future__ import annotations

import asyncio

from rich.console import Console

from clash_of_odin.client.tui.app import SharedState
from clash_of_odin.client.tui.screens.game import (
    CoachPanel,
    GameMapPanel,
    GameScreen,
    ReasoningPanel,
)
from clash_of_odin.client.tui.screens.room import UnitCard


class _FakeApp:
    def __init__(self):
        self.state = SharedState()
        self.client = None
        self.exited = False

    def exit(self) -> None:
        self.exited = True


def _stub_state(app, *, units=None, board_w=6, board_h=6, my_team="blue"):
    app.state.last_game_state = {
        "turn": 1, "max_turns": 20,
        "active_player": my_team, "you": my_team, "status": "in_progress",
        "board": {"width": board_w, "height": board_h, "tiles": []},
        "units": units or [],
        "rules": {"max_turns": 20},
    }


def test_tab_cycles_through_all_five_panels_in_order():
    app = _FakeApp()
    screen = GameScreen(app)
    _stub_state(app)
    screen.state = app.state.last_game_state
    assert screen._panels[screen._focus_idx] is screen.map_panel
    expected = ["PlayerPanel", "ActionsPanel", "ReasoningPanel", "CoachPanel", "GameMapPanel"]
    for want in expected:
        asyncio.run(screen.handle_key("\t"))
        assert type(screen._panels[screen._focus_idx]).__name__ == want


def test_map_cursor_only_responds_when_map_focused():
    app = _FakeApp()
    screen = GameScreen(app)
    _stub_state(app)
    screen.state = app.state.last_game_state
    # Map is default-focused.
    asyncio.run(screen.handle_key("right"))
    assert screen.map_panel.cx == 1
    # Tab to Actions; arrows now move action focus, not the map cursor.
    asyncio.run(screen.handle_key("\t"))
    map_x_before = screen.map_panel.cx
    asyncio.run(screen.handle_key("right"))
    assert screen.map_panel.cx == map_x_before


def test_enter_on_unit_in_game_opens_inline_unit_card():
    app = _FakeApp()
    screen = GameScreen(app)
    _stub_state(
        app,
        units=[
            {
                "id": "u_b_knight_1", "owner": "blue", "class": "knight",
                "pos": {"x": 2, "y": 2}, "hp": 30, "hp_max": 30, "alive": True,
                "atk": 8, "def": 7, "res": 2, "spd": 3, "move": 3, "rng": [1, 1],
            },
        ],
    )
    screen.state = app.state.last_game_state
    asyncio.run(screen.handle_key("right"))
    asyncio.run(screen.handle_key("right"))
    asyncio.run(screen.handle_key("down"))
    asyncio.run(screen.handle_key("down"))
    asyncio.run(screen.handle_key("enter"))
    assert isinstance(screen.unit_card, UnitCard)
    # Esc dismisses the card without exiting.
    asyncio.run(screen.handle_key("esc"))
    assert screen.unit_card is None


def test_coach_panel_captures_typing_when_focused():
    app = _FakeApp()
    screen = GameScreen(app)
    _stub_state(app)
    screen.state = app.state.last_game_state
    # Tab to coach (Map → Player → Actions → Reasoning → Coach).
    for _ in range(4):
        asyncio.run(screen.handle_key("\t"))
    assert screen._panels[screen._focus_idx] is screen.coach_panel
    for ch in "push the cavalry":
        asyncio.run(screen.handle_key(ch))
    assert screen.coach_panel.buffer == "push the cavalry"
    # 'q' goes into the buffer instead of quitting.
    asyncio.run(screen.handle_key("q"))
    assert screen.coach_panel.buffer.endswith("q")
    assert app.exited is False
    # Esc clears the buffer.
    asyncio.run(screen.handle_key("esc"))
    assert screen.coach_panel.buffer == ""


def test_coach_tab_only_releases_when_buffer_empty():
    """If a user is mid-message, Tab should NOT silently cycle them
    away to a different panel — they could be typing 'tab' as part of
    a longer thought."""
    app = _FakeApp()
    screen = GameScreen(app)
    _stub_state(app)
    screen.state = app.state.last_game_state
    for _ in range(4):
        asyncio.run(screen.handle_key("\t"))
    # Type something, then try to Tab away → no-op while buffer non-empty.
    for ch in "hi":
        asyncio.run(screen.handle_key(ch))
    asyncio.run(screen.handle_key("\t"))
    assert screen._panels[screen._focus_idx] is screen.coach_panel
    # Clear and Tab — now we leave.
    asyncio.run(screen.handle_key("esc"))
    asyncio.run(screen.handle_key("\t"))
    assert screen._panels[screen._focus_idx] is screen.map_panel


def test_reasoning_panel_scroll_is_focus_gated():
    app = _FakeApp()
    screen = GameScreen(app)
    _stub_state(app)
    screen.state = app.state.last_game_state
    app.state.thoughts.extend(
        [(f"00:00:0{i}", "blue", f"thought {i}") for i in range(5)]
    )
    # Map is focused; up arrow moves the map cursor, not reasoning offset.
    before = screen.reasoning_panel.offset
    asyncio.run(screen.handle_key("up"))
    assert screen.reasoning_panel.offset == before
    # Tab Map→Player→Actions→Reasoning.
    for _ in range(3):
        asyncio.run(screen.handle_key("\t"))
    asyncio.run(screen.handle_key("up"))
    assert screen.reasoning_panel.offset == before + 1
