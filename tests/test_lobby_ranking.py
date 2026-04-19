"""Tests for the lobby ranking view — Tab cycling, j/k navigation,
disclaimer banner — and the drill-down model details screen."""

from __future__ import annotations

import asyncio

from rich.console import Console

from silicon_pantheon.client.tui.app import SharedState
from silicon_pantheon.client.tui.screens.lobby import LobbyScreen
from silicon_pantheon.client.tui.screens.model_details import ModelDetailsScreen


class _FakeApp:
    def __init__(self):
        self.state = SharedState()
        self.client = None
        self.exited = False

    def exit(self) -> None:
        self.exited = True


def _render(screen, width: int = 200) -> str:
    console = Console(record=True, width=width)
    console.print(screen.render())
    return console.export_text()


def test_lobby_tab_cycles_views():
    app = _FakeApp()
    app.state.display_name = "alice"
    screen = LobbyScreen(app)
    assert screen._active_view == "rooms"
    asyncio.run(screen.handle_key("\t"))
    assert screen._active_view == "ranking"
    asyncio.run(screen.handle_key("\t"))
    assert screen._active_view == "rooms"


def test_lobby_ranking_jk_moves_selection_not_rooms():
    app = _FakeApp()
    app.state.display_name = "alice"
    app.state.last_rooms = [{"room_id": "r1"}, {"room_id": "r2"}, {"room_id": "r3"}]
    app.state.last_leaderboard = [
        {"model": "m1", "provider": "p", "games": 10, "wins": 5},
        {"model": "m2", "provider": "p", "games": 10, "wins": 3},
    ]
    screen = LobbyScreen(app)
    # Rooms view: j moves _selected.
    asyncio.run(screen.handle_key("j"))
    assert screen._selected == 1
    assert screen._ranking_selected == 0
    # Switch to ranking view.
    asyncio.run(screen.handle_key("\t"))
    asyncio.run(screen.handle_key("j"))
    assert screen._ranking_selected == 1
    assert screen._selected == 1  # unchanged
    asyncio.run(screen.handle_key("k"))
    assert screen._ranking_selected == 0


def test_lobby_ranking_disclaimer_rendered():
    app = _FakeApp()
    app.state.display_name = "alice"
    app.state.last_leaderboard = [
        {"model": "m1", "provider": "p", "games": 1, "wins": 1},
    ]
    screen = LobbyScreen(app)
    out = _render(screen)
    # Disclaimer text (or its prefix) should appear in rendered output.
    assert "Rankings" in out or "true capability" in out


def test_lobby_ranking_selection_marker_visible_when_focused():
    app = _FakeApp()
    app.state.display_name = "alice"
    app.state.last_leaderboard = [
        {"model": "first", "provider": "p", "games": 5, "wins": 3},
        {"model": "second", "provider": "p", "games": 5, "wins": 2},
    ]
    screen = LobbyScreen(app)
    # Focus ranking, select 2nd entry.
    asyncio.run(screen.handle_key("\t"))
    asyncio.run(screen.handle_key("j"))
    out = _render(screen)
    assert "first" in out
    assert "second" in out
    # The marker "➤" should appear on the selected row.
    assert "➤" in out


def test_model_details_handles_missing_client():
    app = _FakeApp()
    app.client = None
    screen = ModelDetailsScreen(app, model="m1", provider="p")
    asyncio.run(screen._fetch())
    assert screen._loaded is True
    assert screen._error


def test_model_details_esc_returns_to_lobby():
    app = _FakeApp()
    screen = ModelDetailsScreen(app, model="m1", provider="p")
    next_screen = asyncio.run(screen.handle_key("esc"))
    assert next_screen is not None
    assert next_screen.__class__.__name__ == "LobbyScreen"


def test_model_details_renders_empty_state():
    app = _FakeApp()
    screen = ModelDetailsScreen(app, model="m1", provider="p")
    screen._loaded = True
    screen._details = {}
    screen._h2h = []
    screen._scenarios = []
    out = _render(screen)
    assert "m1" in out
    assert "Head-to-Head" in out or "Opponent" in out


def test_lobby_ranking_cursor_stays_visible_when_scrolling():
    """Regression: LEADERBOARD_VISIBLE_ROWS must match what actually
    fits in RANKING_BAND_HEIGHT — otherwise Rich silently truncates
    the last rows of the table and the cursor disappears when it
    reaches the bottom of the viewport."""
    from silicon_pantheon.client.tui.screens.lobby import LEADERBOARD_VISIBLE_ROWS
    app = _FakeApp()
    app.state.display_name = "alice"
    app.state.last_leaderboard = [
        {"model": f"mdl{i:02d}", "provider": "p", "games": 5, "wins": 3,
         "losses": 2, "draws": 0, "avg_think_time_s": 1.0}
        for i in range(LEADERBOARD_VISIBLE_ROWS * 3)
    ]
    screen = LobbyScreen(app)
    asyncio.run(screen.handle_key("\t"))
    for step in range(len(app.state.last_leaderboard)):
        screen._ranking_selected = step
        out = _render(screen, width=160)
        expected = f"mdl{step:02d}"
        cursor_on_sel = any(
            "➤" in line and expected in line for line in out.splitlines()
        )
        assert cursor_on_sel, (
            f"cursor missing from selected row at step={step}, "
            f"scroll={screen._ranking_scroll}"
        )


def test_lobby_ranking_scrolls_when_selection_goes_past_viewport():
    """Selecting row N (N > viewport size) should advance the scroll
    offset so the row stays visible when render slices the window."""
    from silicon_pantheon.client.tui.screens.lobby import LEADERBOARD_VISIBLE_ROWS
    app = _FakeApp()
    app.state.display_name = "alice"
    app.state.last_leaderboard = [
        {"model": f"m{i:02d}", "provider": "p", "games": 5, "wins": 2,
         "losses": 3, "draws": 0, "avg_think_time_s": 1.0}
        for i in range(LEADERBOARD_VISIBLE_ROWS + 5)
    ]
    screen = LobbyScreen(app)
    asyncio.run(screen.handle_key("\t"))
    for _ in range(LEADERBOARD_VISIBLE_ROWS + 3):
        asyncio.run(screen.handle_key("j"))
    _render(screen)  # triggers scroll adjustment
    selected = screen._ranking_selected
    scroll = screen._ranking_scroll
    assert scroll > 0, "scroll should have advanced past initial 0"
    assert scroll <= selected < scroll + LEADERBOARD_VISIBLE_ROWS, (
        f"selected row {selected} must lie in viewport [{scroll}, "
        f"{scroll + LEADERBOARD_VISIBLE_ROWS})"
    )


def test_lobby_layout_scoreboard_above_rooms():
    """Ranking scoreboard is a compact widget at the TOP; rooms
    panel takes the rest of the screen below it."""
    app = _FakeApp()
    app.state.display_name = "alice"
    app.state.last_leaderboard = [
        {"model": "m1", "provider": "p", "games": 1, "wins": 1,
         "losses": 0, "draws": 0, "avg_think_time_s": 1.0}
    ]
    out = _render(LobbyScreen(app))
    rooms_idx = out.find("Lobby — alice")
    lb_idx = out.find("Leaderboard")
    assert 0 <= lb_idx < rooms_idx, "scoreboard must render above rooms panel"


def test_leaderboard_table_has_row_dividers():
    """Regression guard: show_lines=True must be preserved so each
    model row is visually separated."""
    app = _FakeApp()
    app.state.display_name = "alice"
    app.state.last_leaderboard = [
        {"model": f"m{i}", "provider": "p", "games": 5, "wins": 3,
         "losses": 2, "draws": 0, "avg_think_time_s": 1.0}
        for i in range(4)
    ]
    out = _render(LobbyScreen(app))
    # DOUBLE_EDGE box with show_lines=True emits `╟──┼──┼──╢` row
    # separators inside a double-line outer. Any of these cross/edge
    # characters confirms rows are separated.
    assert any(c in out for c in "├┼╟"), (
        "leaderboard rows should be separated by dividers"
    )


def test_model_details_renders_with_data():
    app = _FakeApp()
    screen = ModelDetailsScreen(app, model="claude-opus-4-7", provider="anthropic")
    screen._loaded = True
    screen._details = {
        "model": "claude-opus-4-7",
        "provider": "anthropic",
        "games": 10,
        "wins": 6,
        "losses": 3,
        "draws": 1,
        "win_pct": 60.0,
        "total_tokens": 1_200_000,
        "tokens_per_win": 200_000,
        "total_tool_calls": 400,
        "total_errors": 8,
        "error_rate_pct": 2.0,
        "avg_think_time_s": 4.3,
        "max_think_time_s": 42.1,
        "avg_units_killed": 3.2,
        "avg_units_lost": 2.1,
        "avg_match_duration_s": 180.0,
    }
    screen._h2h = [
        {"opponent": "gpt-4o", "provider": "openai", "games": 5, "wins": 3, "losses": 2, "draws": 0, "avg_turns": 14.2},
    ]
    screen._scenarios = [
        {"scenario": "03_thermopylae", "games": 4, "wins": 3, "losses": 1, "draws": 0},
        {"scenario": "04_cannae", "games": 6, "wins": 3, "losses": 2, "draws": 1},
    ]
    out = _render(screen)
    assert "claude-opus-4-7" in out
    assert "gpt-4o" in out
    assert "thermopylae" in out or "03_thermopylae" in out
    # Aggregate stats show up
    assert "60.0%" in out or "60%" in out
