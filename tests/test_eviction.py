"""Tests for shared.eviction — classifier, AlertModal, and the
TUIApp routing that surfaces a modal when a screen detects it has
been kicked out of the state it assumed.

Background
----------
Before this code shipped, screens displayed eviction-class errors as
a one-line red string at the bottom (e.g. "game tools require
state=in_game (current: in_lobby)"). The user could keep pressing
keys into a dead screen with no idea what to do. The fix is a modal
overlay with an OK button that escorts the user to lobby (state
mismatch) or login (full session loss). These tests pin down each
layer:

  - Server-error / provider-error / transport-exception classifier
    returns the right destination + message
  - AlertModal renders a single OK affordance and dismisses on every
    reasonable key
  - TUIApp.show_eviction_alert installs the alert + post-dismiss
    factory; the dispatcher routes keys to the alert before the
    screen handler; on dismiss the staged factory transitions to
    the right screen
"""

from __future__ import annotations

import asyncio

import pytest


# ---- classifier ----


def test_classify_server_error_in_lobby_message() -> None:
    """Server-side ``current: in_lobby`` after the heartbeat-evict
    auto-concede means the connection is back in lobby. Escort
    there, not to login."""
    from silicon_pantheon.shared.eviction import classify_server_error

    info = classify_server_error(
        {
            "code": "tool_not_available_in_state",
            "message": "game tools require state=in_game (current: in_lobby)",
        },
        on_screen="game",
    )
    assert info is not None
    assert info.destination == "lobby"
    assert "Removed from game" in info.title


def test_classify_server_error_set_player_metadata_first() -> None:
    """``call set_player_metadata first`` after a hard eviction means
    the server has forgotten our session entirely. Login is the only
    route that rebuilds it."""
    from silicon_pantheon.shared.eviction import classify_server_error

    info = classify_server_error(
        {
            "code": "not_registered",
            "message": "call set_player_metadata first",
        },
        on_screen="lobby",
    )
    assert info is not None
    assert info.destination == "login"
    assert "Network disconnected" in info.title


def test_classify_server_error_routine_validation_is_not_eviction() -> None:
    """Garden-variety per-action errors (move out of range, etc.)
    must NOT trigger an eviction modal — the screen still wants to
    show its inline red footer for those."""
    from silicon_pantheon.shared.eviction import classify_server_error

    assert classify_server_error(
        {"code": "bad_input", "message": "move out of range"},
        on_screen="game",
    ) is None
    assert classify_server_error(
        {"code": "bad_input", "message": "u_b_henry_v_1 has already acted this turn"},
        on_screen="game",
    ) is None
    assert classify_server_error(None, on_screen="game") is None


def test_classify_server_error_message_phrasing_per_screen() -> None:
    """Same eviction code surfaces different titles based on which
    screen detected it — context-sensitive prose helps the user
    understand what just happened."""
    from silicon_pantheon.shared.eviction import classify_server_error

    err = {
        "code": "tool_not_available_in_state",
        "message": "anything (current: in_lobby)",
    }
    game_info = classify_server_error(err, on_screen="game")
    room_info = classify_server_error(err, on_screen="room")
    assert game_info is not None and room_info is not None
    assert "game" in game_info.title.lower()
    assert "room" in room_info.title.lower()


def test_classify_provider_error_terminal_routes_to_lobby() -> None:
    """Auth / billing / model-not-found are terminal and need user
    action — escort to lobby so they can re-auth or pick another
    model."""
    from silicon_pantheon.shared.eviction import classify_provider_error

    for reason in ("auth", "auth_permanent", "billing", "model_not_found"):
        info = classify_provider_error(reason, "detail goes here")
        assert info is not None, f"reason {reason!r} must classify as terminal"
        assert info.destination == "lobby"
        assert info.title == "Model provider error"
        assert "detail goes here" in info.message


def test_classify_provider_error_transient_is_not_eviction() -> None:
    """Rate-limited / overloaded / timed-out are retried in place;
    no need for a modal."""
    from silicon_pantheon.shared.eviction import classify_provider_error

    for reason in ("rate_limit", "overloaded", "timeout", "unknown", None):
        assert classify_provider_error(reason) is None, (
            f"reason {reason!r} must NOT classify as eviction"
        )


def test_classify_transport_exception_network_dead_routes_to_login() -> None:
    """A transport-layer exception that names a closed stream means
    the connection itself is gone — login is the only recovery."""
    from anyio import ClosedResourceError

    from silicon_pantheon.shared.eviction import classify_transport_exception

    info = classify_transport_exception(ClosedResourceError())
    assert info is not None
    assert info.destination == "login"
    assert "Network disconnected" in info.title


def test_classify_transport_exception_random_runtimeerror_is_none() -> None:
    """Don't escalate every RuntimeError — only the network-dead
    pattern. Keep classification specific so generic exceptions
    keep flowing through their existing handlers."""
    from silicon_pantheon.shared.eviction import classify_transport_exception

    assert classify_transport_exception(
        RuntimeError("something went wrong unrelated")
    ) is None
    assert classify_transport_exception(None) is None


def test_classify_any_first_signal_wins() -> None:
    """When multiple sources fire, server_error is checked first,
    then provider_error, then transport_exc — first match wins so
    the user sees the most actionable framing."""
    from silicon_pantheon.shared.eviction import classify_any

    info = classify_any(
        server_error={"code": "not_registered"},
        provider_reason="auth",
    )
    assert info is not None and info.destination == "login"

    # No server signal, only provider terminal: lobby alert.
    info = classify_any(provider_reason="billing")
    assert info is not None and info.destination == "lobby"

    # Nothing at all.
    assert classify_any() is None


# ---- AlertModal ----


def test_alert_modal_dismiss_keys_fire_callback_once() -> None:
    """Every reasonable acknowledgement key must dismiss exactly
    once and fire on_dismiss exactly once. A second key after
    dismissal must not re-fire the callback."""
    from silicon_pantheon.client.tui.widgets import AlertModal

    for key in ("enter", "esc", "q", " ", "y", "n", "\t"):
        fired = []

        async def cb():
            fired.append(key)

        m = AlertModal(title="t", body="b", on_dismiss=cb)
        closed = asyncio.run(m.handle_key(key))
        assert closed is True, f"key {key!r} should close the modal"
        assert fired == [key], (
            f"key {key!r} should fire on_dismiss exactly once"
        )

        # Second key after close: callback already cleared, must
        # NOT re-fire even if the dispatcher accidentally calls
        # us again.
        closed_again = asyncio.run(m.handle_key("enter"))
        assert closed_again is True
        assert fired == [key], "second key must not re-fire callback"


def test_alert_modal_unknown_keys_keep_modal_open() -> None:
    """Arrow keys, j/k navigation chars, etc. don't make sense
    inside a single-button alert. They should be swallowed without
    closing the modal."""
    from silicon_pantheon.client.tui.widgets import AlertModal

    fired = []

    async def cb():
        fired.append(True)

    m = AlertModal(title="t", body="b", on_dismiss=cb)
    for key in ("up", "down", "left", "right", "j", "k", "h", "l"):
        closed = asyncio.run(m.handle_key(key))
        assert closed is False, f"key {key!r} should not close modal"
    assert fired == [], "no on_dismiss should have fired"


def test_alert_modal_renders_title_and_body() -> None:
    """Smoke-test the renderable: title and body text must appear
    somewhere in the rendered string. We don't pin exact layout."""
    from rich.console import Console

    from silicon_pantheon.client.tui.widgets import AlertModal

    m = AlertModal(
        title="Network disconnected",
        body="The server forgot you. Sign in again.",
    )
    console = Console(width=80, record=True)
    with console.capture() as cap:
        console.print(m.render())
    out = cap.get()
    assert "Network disconnected" in out
    assert "Sign in again" in out
    assert "OK" in out


# ---- TUIApp.show_eviction_alert + dispatcher routing ----


def _make_app():
    """Build a TUIApp with a no-op initial screen factory."""
    from silicon_pantheon.client.tui.app import Screen, TUIApp

    class _NullScreen(Screen):
        def render(self):
            from rich.text import Text
            return Text("null")

    return TUIApp(initial_screen_factory=lambda app: _NullScreen())


def test_show_eviction_alert_installs_modal_and_factory() -> None:
    """show_eviction_alert sets pending_alert + pending_screen_factory
    based on EvictionInfo.destination."""
    from silicon_pantheon.shared.eviction import EvictionInfo

    app = _make_app()
    info = EvictionInfo(
        title="Removed from game",
        message="match ended without you",
        destination="lobby",
    )
    installed = app.show_eviction_alert(info)
    assert installed is True
    assert app.state.pending_alert is not None
    assert app.state.pending_screen_factory is not None


def test_show_eviction_alert_first_write_wins() -> None:
    """A second alert arriving while the first is still pending
    must NOT replace it — the user is still reading the original.
    Avoids dialog-flapping on background poll storms."""
    from silicon_pantheon.shared.eviction import EvictionInfo

    app = _make_app()
    first = EvictionInfo(title="A", message="aaa", destination="lobby")
    second = EvictionInfo(title="B", message="bbb", destination="login")
    assert app.show_eviction_alert(first) is True
    assert app.show_eviction_alert(second) is False
    assert app.state.pending_alert.title == "A"


def test_alert_dispatch_dismiss_clears_alert_and_factory() -> None:
    """Calling _handle_alert_key with a dismiss key must clear both
    pending_alert and pending_screen_factory so the next eviction
    starts from a clean slate."""
    from silicon_pantheon.shared.eviction import EvictionInfo

    app = _make_app()
    info = EvictionInfo(
        title="t", message="m", destination="lobby",
    )

    async def go():
        # Initial screen has to exist before transition can fire.
        await app.transition(app._initial_factory(app))
        app.show_eviction_alert(info)
        # Note: the factory imports LobbyScreen which itself imports
        # TUI machinery; we don't actually want to transition in this
        # test (would need a real ServerClient). Drop the factory so
        # the dispatcher just clears state.
        app.state.pending_screen_factory = None
        consumed = await app._handle_alert_key("enter")
        assert consumed is True
        assert app.state.pending_alert is None
        assert app.state.pending_screen_factory is None

    asyncio.run(go())


def test_alert_dispatch_no_alert_returns_false() -> None:
    """When no alert is pending, _handle_alert_key returns False so
    the dispatcher falls through to help / screen handlers."""
    app = _make_app()

    async def go():
        consumed = await app._handle_alert_key("enter")
        assert consumed is False

    asyncio.run(go())


def test_render_with_overlay_prefers_alert_over_screen() -> None:
    """The eviction alert must paint on top of the current screen
    (and on top of the help overlay). Otherwise the user could miss
    the modal entirely if the screen's render is on top."""
    from silicon_pantheon.client.tui.widgets import AlertModal

    app = _make_app()
    asyncio.run(app.transition(app._initial_factory(app)))
    app.state.pending_alert = AlertModal(
        title="UNIQUE_ALERT_TOKEN",
        body="body",
    )

    from rich.console import Console
    console = Console(width=80, record=True)
    with console.capture() as cap:
        console.print(app._render_with_overlay())
    assert "UNIQUE_ALERT_TOKEN" in cap.get()
    assert "null" not in cap.get(), (
        "alert should fully replace screen render, not stack with it"
    )
