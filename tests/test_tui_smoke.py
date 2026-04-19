"""Non-interactive smoke tests for TUI screens.

The TUIApp main loop requires a real TTY and an SSE backend — out of
scope for unit tests. What we *can* cover is that each Screen's
render() returns a renderable and handle_key() routes predictable
tokens to the right transitions, using a mock TUIApp without any
transport attached.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from rich.console import Console

from silicon_pantheon.client.tui.app import SharedState
from silicon_pantheon.client.tui.screens.login import LoginScreen
from silicon_pantheon.client.tui.screens.lobby import LobbyScreen
from silicon_pantheon.client.tui.screens.post_match import PostMatchScreen
from silicon_pantheon.client.tui.screens.room import RoomScreen


class _FakeApp:
    """Stand-in for TUIApp — just the attributes screens read/write."""

    def __init__(self):
        self.state = SharedState()
        self.client = None
        self.exited = False

    def exit(self) -> None:
        self.exited = True


def _render(screen) -> str:
    console = Console(record=True, width=100)
    console.print(screen.render())
    return console.export_text()


def test_login_screen_renders() -> None:
    app = _FakeApp()
    out = _render(LoginScreen(app))
    assert "login" in out
    assert "server URL" in out
    assert "display name" in out


def test_login_screen_field_navigation() -> None:
    app = _FakeApp()
    screen = LoginScreen(app)
    assert screen._active == 0
    asyncio.run(screen.handle_key("down"))
    assert screen._active == 1
    asyncio.run(screen.handle_key("up"))
    assert screen._active == 0


def test_login_screen_text_entry() -> None:
    app = _FakeApp()
    screen = LoginScreen(app)
    # Field 1 = display name
    screen._active = 1
    for ch in "alice":
        asyncio.run(screen.handle_key(ch))
    assert screen._fields[1].value == "alice"
    # Backspace drops one char.
    asyncio.run(screen.handle_key("backspace"))
    assert screen._fields[1].value == "alic"


def test_login_quit_shows_confirm() -> None:
    app = _FakeApp()
    screen = LoginScreen(app)
    asyncio.run(screen.handle_key("q"))
    # q now shows a confirmation modal instead of exiting directly.
    assert screen._confirm is not None
    assert app.exited is False


def test_login_submit_without_name_sets_error() -> None:
    app = _FakeApp()
    screen = LoginScreen(app)
    # No display name entered → submit should reject.
    asyncio.run(screen.handle_key("enter"))
    assert "display name" in app.state.error_message.lower()


def test_lobby_screen_renders_empty() -> None:
    app = _FakeApp()
    app.state.display_name = "alice"
    out = _render(LobbyScreen(app))
    assert "Lobby" in out
    assert "alice" in out
    assert "crick" in out or "rooms" in out  # playful text may be word-wrapped


def test_room_screen_renders_with_stub_state() -> None:
    app = _FakeApp()
    app.state.room_id = "ROOM123"
    app.state.slot = "a"
    app.state.last_room_state = {
        "room_id": "ROOM123",
        "scenario": "01_tiny_skirmish",
        "fog_of_war": "classic",
        "team_assignment": "fixed",
        "status": "waiting_ready",
        "seats": {
            "a": {"occupied": True, "player": {"display_name": "alice"}, "ready": False},
            "b": {"occupied": True, "player": {"display_name": "bob"}, "ready": True},
        },
    }
    out = _render(RoomScreen(app))
    assert "ROOM123" in out
    assert "alice" in out and "bob" in out
    assert "01_tiny_skirmish" in out


def test_post_match_screen_renders_winner() -> None:
    app = _FakeApp()
    app.state.last_game_state = {
        "winner": "blue",
        "turn": 12,
        "max_turns": 20,
        "you": "blue",  # server emits just the team string, not a dict
        "last_action": {"reason": "seize"},
        "units": [{"owner": "blue"}, {"owner": "red"}],
    }
    out = _render(PostMatchScreen(app))
    assert "victory" in out.lower() or "won" in out.lower()
    assert "Blue" in out
    assert "seize" in out
