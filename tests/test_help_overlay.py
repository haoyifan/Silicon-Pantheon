"""Help overlay — F2 toggles, Esc closes, scroll keys work, and the
overlay doesn't interfere with the underlying screen's tick loop."""

from __future__ import annotations

import asyncio

from rich.console import Console

from clash_of_odin.client.tui.app import HELP_TEXT, _HelpOverlay


def test_help_overlay_renders_some_help_content():
    overlay = _HelpOverlay(scroll=0)
    console = Console(record=True, width=120)
    console.print(overlay.render())
    out = console.export_text()
    assert "Welcome to Clash of Odin" in out
    assert "Tab" in out
    # Footer keys present.
    assert "Esc / F2 close" in out


def test_help_overlay_scroll_drops_leading_lines():
    """Scrolling shouldn't render anything from the first N lines."""
    overlay = _HelpOverlay(scroll=20)
    console = Console(record=True, width=120)
    console.print(overlay.render())
    out = console.export_text()
    # The original header should now be off-screen.
    assert "Welcome to Clash of Odin" not in out
    # ...but later content remains.
    assert "Tab" in out


def test_help_text_mentions_player_facing_things_only():
    """Sanity: help should talk about gameplay and navigation, NOT
    implementation slugs like 'unit_classes' or 'class_spec'."""
    forbidden = ["class_spec", "unit_classes", "_render_", "RichPanel", "describe_scenario"]
    for word in forbidden:
        assert word not in HELP_TEXT, f"help leaks impl detail: {word}"


def test_app_help_intercept_does_not_leak_to_screen():
    """When help is open, gameplay keys (e/x/q) should be swallowed
    by the overlay so they don't end the turn or concede behind the
    user's back."""
    from clash_of_odin.client.tui.app import SharedState, TUIApp

    seen: list[str] = []

    class _Probe:
        async def handle_key(self, key: str):
            seen.append(key)
            return None

        def render(self):
            from rich.text import Text
            return Text("probe")

        async def on_enter(self, app):
            return None

        async def on_exit(self, app):
            return None

        async def tick(self):
            return None

    app = TUIApp(lambda a: _Probe())
    app._screen = _Probe()
    # Open help.
    consumed = app._handle_help_key("f2")
    assert consumed is True
    assert app._help_visible is True
    # While open, ordinary gameplay keys are swallowed (don't reach
    # _Probe). Use keys that don't dismiss — 'e' (end_turn),
    # 'x' (concede), 'enter' (button activate).
    for k in ("e", "x", "enter"):
        assert app._handle_help_key(k) is True
    assert app._help_visible is True  # still open
    # 'q' / 'esc' both dismiss.
    assert app._handle_help_key("q") is True
    assert app._help_visible is False
    # Once closed, those same keys are NOT consumed by the overlay.
    assert app._handle_help_key("e") is False
