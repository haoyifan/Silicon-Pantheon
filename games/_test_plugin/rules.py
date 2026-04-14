"""Test plugin. `always_blue_wins` fires on every end_turn so the
plugin-loader test can assert that scenario-local Python is wired in.
"""

from __future__ import annotations


def always_blue_wins(state, hook, **_):
    if hook != "end_turn":
        return None
    return {"winner": "blue", "reason": "test_plugin"}


def some_helper(x: int) -> int:
    """Public name (no underscore) — should show up in the namespace."""
    return x * 2


def _private_helper(x: int) -> int:
    """Underscore-prefixed — should NOT be exposed."""
    return x
