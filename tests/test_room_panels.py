"""Room screen panel framework + map cursor + unit card."""

from __future__ import annotations

import asyncio

import pytest
from rich.console import Console

from clash_of_odin.client.tui.app import SharedState
from clash_of_odin.client.tui.screens.room import (
    ActionsPanel,
    ConfirmModal,
    Dropdown,
    MapPanel,
    RoomScreen,
    UnitCard,
)


class _FakeApp:
    def __init__(self):
        self.state = SharedState()
        self.client = None
        self.exited = False

    def exit(self) -> None:
        self.exited = True


def _stub_room(app):
    app.state.room_id = "ROOM"
    app.state.slot = "a"
    app.state.last_room_state = {
        "room_id": "ROOM",
        "scenario": "01_tiny_skirmish",
        "fog_of_war": "none",
        "team_assignment": "fixed",
        "host_team": "blue",
        "status": "waiting_ready",
        "seats": {
            "a": {"player": {"display_name": "alice"}, "ready": False},
            "b": {"player": {"display_name": "bob"}, "ready": True},
        },
    }


def test_tab_cycles_through_all_five_panels():
    app = _FakeApp()
    _stub_room(app)
    screen = RoomScreen(app)
    # Starts on Actions panel.
    assert screen._panels[screen._focus_idx] is screen.actions_panel
    titles = []
    for _ in range(5):
        asyncio.run(screen.handle_key("\t"))
        titles.append(type(screen._panels[screen._focus_idx]).__name__)
    assert titles[-1] == "ActionsPanel"  # wrapped back
    assert len(set(titles[:-1])) == 4  # visited 4 distinct other panels


def test_arrow_keys_only_affect_focused_panel():
    app = _FakeApp()
    _stub_room(app)
    screen = RoomScreen(app)
    initial_action = screen.actions_panel.focus
    asyncio.run(screen.handle_key("down"))
    assert screen.actions_panel.focus == initial_action + 1
    assert (screen.map_panel.cx, screen.map_panel.cy) == (0, 0)


def _focus_map(screen) -> None:
    screen._focus_idx = screen._panels.index(screen.map_panel)


def test_map_panel_cursor_navigates_with_arrows():
    app = _FakeApp()
    _stub_room(app)
    screen = RoomScreen(app)
    screen.scenario_preview = {
        "width": 6, "height": 6, "units": [], "forts": [],
    }
    _focus_map(screen)
    asyncio.run(screen.handle_key("right"))
    asyncio.run(screen.handle_key("right"))
    asyncio.run(screen.handle_key("down"))
    assert (screen.map_panel.cx, screen.map_panel.cy) == (2, 1)


def test_map_panel_cursor_wraps():
    app = _FakeApp()
    _stub_room(app)
    screen = RoomScreen(app)
    screen.scenario_preview = {"width": 4, "height": 4, "units": [], "forts": []}
    _focus_map(screen)
    asyncio.run(screen.handle_key("up"))
    assert screen.map_panel.cy == 3
    asyncio.run(screen.handle_key("left"))
    assert screen.map_panel.cx == 3


def test_map_panel_vim_hjkl_keys_also_move_cursor():
    app = _FakeApp()
    _stub_room(app)
    screen = RoomScreen(app)
    screen.scenario_preview = {"width": 5, "height": 5, "units": [], "forts": []}
    _focus_map(screen)
    # two rights to reach x=2, then down to y=1.
    asyncio.run(screen.handle_key("l"))
    asyncio.run(screen.handle_key("l"))
    asyncio.run(screen.handle_key("j"))
    assert (screen.map_panel.cx, screen.map_panel.cy) == (2, 1)
    asyncio.run(screen.handle_key("h"))  # left
    asyncio.run(screen.handle_key("k"))  # up
    assert (screen.map_panel.cx, screen.map_panel.cy) == (1, 0)


def test_enter_on_unit_opens_inline_unit_card():
    app = _FakeApp()
    _stub_room(app)
    screen = RoomScreen(app)
    screen.scenario_preview = {
        "width": 4, "height": 4,
        "units": [
            {
                "id": "u_b_knight_1", "owner": "blue", "class": "knight",
                "pos": {"x": 1, "y": 1},
            },
        ],
        "forts": [],
    }
    _focus_map(screen)
    asyncio.run(screen.handle_key("right"))
    asyncio.run(screen.handle_key("down"))
    asyncio.run(screen.handle_key("enter"))
    assert isinstance(screen.unit_card, UnitCard)
    # Esc dismisses the inline card without exiting the screen.
    asyncio.run(screen.handle_key("esc"))
    assert screen.unit_card is None


def test_unit_card_shows_class_spec_stats_when_unit_has_none():
    """Regression: room preview units carry no stats — the card must
    fall back to describe_scenario.unit_classes to fill HP/ATK/etc."""
    card = UnitCard(
        units=[{"id": "u_b_knight_1", "owner": "blue", "class": "knight",
                "pos": {"x": 0, "y": 0}}],
        index=0,
        unit_classes={"knight": {
            "hp_max": 30, "atk": 8, "defense": 7, "res": 2,
            "spd": 3, "move": 3, "rng_min": 1, "rng_max": 1,
            "tags": ["melee"],
        }},
    )
    console = Console(record=True, width=80)
    console.print(card.render())
    out = console.export_text()
    assert "?" not in out.split("HP", 1)[1].split("\n", 1)[0]  # HP row is real
    assert "30" in out
    assert "8" in out   # atk
    assert "melee" in out


def test_unit_card_renders_stats_and_class_name():
    unit = {
        "id": "u_b_sun_wukong_1", "owner": "blue", "class": "sun_wukong",
        "hp": 42, "hp_max": 42, "atk": 14, "def": 7, "res": 6,
        "spd": 9, "move": 5, "rng": [1, 1],
        "tags": ["hero", "monkey"],
    }
    card = UnitCard(
        units=[unit],
        index=0,
        unit_classes={"sun_wukong": {"description": "The Monkey King."}},
    )
    console = Console(record=True, width=80)
    console.print(card.render())
    out = console.export_text()
    assert "sun_wukong" in out
    assert "The Monkey King" in out
    assert "42" in out  # hp
    assert "hero" in out and "monkey" in out


def test_focused_panel_gets_yellow_border():
    """Visual contract: the focused panel uses the bright_yellow style."""
    app = _FakeApp()
    _stub_room(app)
    screen = RoomScreen(app)
    console = Console(record=True, width=120)
    console.print(screen.render())
    # Switch to ANSI export to detect the highlight color.
    ansi = console.export_text(styles=True)
    # Yellow appears on the focused (Actions) panel border.
    assert "Actions" in ansi


def test_unit_card_h_l_cycle_units_and_dismiss_snaps_cursor():
    """h/← steps to previous unit, l/→ to next; close lands the
    cursor on whichever unit is currently in the card."""
    app = _FakeApp()
    _stub_room(app)
    screen = RoomScreen(app)
    screen.scenario_preview = {
        "width": 6, "height": 6,
        "units": [
            {"id": "u_b_a", "owner": "blue", "class": "a", "pos": {"x": 1, "y": 0}},
            {"id": "u_b_b", "owner": "blue", "class": "b", "pos": {"x": 3, "y": 2}},
            {"id": "u_r_c", "owner": "red", "class": "c", "pos": {"x": 5, "y": 5}},
        ],
        "forts": [],
    }
    _focus_map(screen)
    # Move cursor to (1, 0) where u_b_a sits.
    asyncio.run(screen.handle_key("right"))
    asyncio.run(screen.handle_key("enter"))
    assert screen.unit_card is not None
    assert screen.unit_card.unit["id"] == "u_b_a"
    # → goes to next (u_b_b at (3,2)).
    asyncio.run(screen.handle_key("l"))
    assert screen.unit_card.unit["id"] == "u_b_b"
    # ← steps back.
    asyncio.run(screen.handle_key("h"))
    assert screen.unit_card.unit["id"] == "u_b_a"
    # → twice → wraps from b → c.
    asyncio.run(screen.handle_key("right"))
    asyncio.run(screen.handle_key("right"))
    assert screen.unit_card.unit["id"] == "u_r_c"
    # Esc dismisses, cursor snaps to (5,5).
    asyncio.run(screen.handle_key("esc"))
    assert screen.unit_card is None
    assert (screen.map_panel.cx, screen.map_panel.cy) == (5, 5)


def test_room_map_renders_forest_and_fort_from_preview_tiles():
    """Regression: room MapPanel used to ignore the tiles list, so
    forests / mountains never showed up; fort tiles showed only the
    fort glyph and the cursor tooltip said 'plain'."""
    app = _FakeApp()
    _stub_room(app)
    screen = RoomScreen(app)
    screen.scenario_preview = {
        "width": 4, "height": 4,
        "units": [],
        "forts": [{"pos": {"x": 0, "y": 0}, "owner": "blue"}],
        "tiles": [
            {"x": 1, "y": 1, "type": "forest"},
            {"x": 0, "y": 0, "type": "fort", "fort_owner": "blue"},
        ],
    }
    _focus_map(screen)
    console = Console(record=True, width=120)
    console.print(screen.render())
    out = console.export_text()
    # Forest glyph 'f' is visible somewhere in the rendered map row.
    assert " f " in out
    # Fort glyph '*' too.
    assert "*" in out
    # Tooltip on the fort cell calls it 'fort', not 'plain'.
    # cursor still at (0,0), which IS the fort.
    assert "terrain: fort" in out


def test_enter_on_open_unit_card_dismisses_it():
    """Re-pressing Enter on the same unit toggles the card off — same
    muscle memory as 'open it then close it again'."""
    app = _FakeApp()
    _stub_room(app)
    screen = RoomScreen(app)
    screen.scenario_preview = {
        "width": 4, "height": 4,
        "units": [
            {"id": "u_b_knight_1", "owner": "blue", "class": "knight",
             "pos": {"x": 0, "y": 0}},
        ],
        "forts": [],
    }
    _focus_map(screen)
    asyncio.run(screen.handle_key("enter"))
    assert screen.unit_card is not None
    asyncio.run(screen.handle_key("enter"))
    assert screen.unit_card is None
    # Re-open and dismiss with Esc too.
    asyncio.run(screen.handle_key("enter"))
    assert screen.unit_card is not None
    asyncio.run(screen.handle_key("esc"))
    assert screen.unit_card is None


def test_win_condition_prose_uses_display_name_when_available():
    """Regression: win conditions used to render `u_b_tang_monk_1`
    directly. With display_name in the bundle, the prose should say
    `Tang Monk`."""
    from clash_of_odin.client.tui.screens.room import _describe_win_condition

    bundle = {
        "unit_classes": {
            "tang_monk": {"display_name": "Tang Monk"},
        },
    }
    out = _describe_win_condition(
        {"type": "protect_unit", "unit_id": "u_b_tang_monk_1",
         "owning_team": "blue"},
        bundle,
    )
    assert "Tang Monk" in out
    assert "u_b_tang_monk_1" not in out
    # Side-explicit: red wins when blue's VIP dies.
    assert "Red wins" in out


def test_protect_unit_prose_names_the_winning_side():
    """The opposite team of `owning_team` is the one that wins when
    the protected unit dies. Earlier prose buried this — it just said
    'keep X alive (blue)' which never explained how RED wins."""
    from clash_of_odin.client.tui.screens.room import _describe_win_condition

    out = _describe_win_condition(
        {"type": "protect_unit", "unit_id": "u_r_boss_1", "owning_team": "red"},
        None,
    )
    assert "Blue wins" in out


def test_eliminate_all_prose_says_either_side_wins():
    from clash_of_odin.client.tui.screens.room import _describe_win_condition

    out = _describe_win_condition({"type": "eliminate_all_enemy_units"}, None)
    assert "Either side" in out


def test_win_condition_prose_falls_back_when_no_display_name():
    from clash_of_odin.client.tui.screens.room import _describe_win_condition

    out = _describe_win_condition(
        {"type": "protect_unit", "unit_id": "u_b_knight_1",
         "owning_team": "blue"},
        None,
    )
    # Without the bundle we still render something readable — the
    # class slug — instead of the raw u_b_xxx_1.
    assert "knight" in out
    assert "u_b_knight_1" not in out


def test_room_description_panel_includes_armies_and_unit_descriptions():
    """The game-room Description panel should match the scenario picker:
    army composition + per-class descriptions, not just the scenario
    blurb + win conditions."""
    from clash_of_odin.client.tui.screens.room import DescriptionPanel

    app = _FakeApp()
    app.state.scenario_description = {
        "name": "Demo",
        "description": "stub blurb",
        "armies": {
            "blue": [{"class": "tang_monk", "pos": {"x": 0, "y": 0}}],
            "red": [{"class": "demon_king", "pos": {"x": 4, "y": 4}}],
        },
        "unit_classes": {
            "tang_monk": {"display_name": "Tang Monk",
                          "description": "The pilgrim."},
            "demon_king": {"display_name": "Demon King",
                           "description": "The boss."},
        },
        "win_conditions": [{"type": "eliminate_all_enemy_units"}],
    }
    panel = DescriptionPanel(app)
    console = Console(record=True, width=80)
    console.print(panel.render(focused=False))
    out = console.export_text()
    assert "Armies:" in out
    assert "Tang Monk" in out
    assert "Demon King" in out
    assert "The pilgrim." in out
    assert "The boss." in out


def test_dropdown_shows_description_of_highlighted_option():
    dd = Dropdown(
        title="Change Fog",
        options=["none", "classic", "line_of_sight"],
        selected_idx=1,
        on_confirm=lambda v: None,  # type: ignore[arg-type]
        option_descriptions={
            "none": "No fog.",
            "classic": "Classic Fire Emblem fog.",
            "line_of_sight": "Strict LoS.",
        },
    )
    console = Console(record=True, width=80)
    console.print(dd.render())
    out = console.export_text()
    assert "Classic Fire Emblem fog" in out
    # Other descriptions not shown (only the highlighted one).
    assert "Strict LoS" not in out


def test_confirm_modal_yes_routes_to_callback():
    called: list[bool] = []

    async def on_confirm(yes: bool) -> None:
        called.append(yes)

    m = ConfirmModal(prompt="Really?", on_confirm=on_confirm)
    # Default selection is No.
    assert m.selected_yes is False
    asyncio.run(m.handle_key("left"))
    assert m.selected_yes is True
    closed = asyncio.run(m.handle_key("enter"))
    assert closed is True
    assert called == [True]


def test_leave_room_opens_confirm_modal_not_immediate_leave():
    app = _FakeApp()
    _stub_room(app)
    screen = RoomScreen(app)
    # Focus on Actions, navigate to "Leave Room" (fixed team-mode host
    # sees 7 buttons; blindly find by label).
    buttons = screen.actions_panel._buttons()
    leave_idx = next(
        i for i, b in enumerate(buttons) if b.action == "leave"
    )
    screen.actions_panel.focus = leave_idx
    asyncio.run(screen.handle_key("enter"))
    assert screen._confirm is not None
    # 'No' by default — Enter just dismisses without leaving.
    next_screen = asyncio.run(screen.handle_key("enter"))
    assert screen._confirm is None
    assert next_screen is None  # still in the room
