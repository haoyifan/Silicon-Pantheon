"""Detect tool-response shapes that mean "the match has ended; stop acting."

When a player keeps trying to mutate state after the game has finished,
the server's tool dispatch translates the engine-level
``IllegalAction("game is already over")`` into an error response:

    {"ok": false, "error": {"code": "bad_input",
                            "message": "game is already over"}}

The client unwraps that to ``{"error": {...}}`` in _dispatch_tool. A
robust agent bridge should notice this and break out of its LLM loop
immediately; otherwise a weak model (grok-3-mini has been observed
doing this) will apologize and retry ``end_turn`` forever, burning
provider tokens and holding the worker in a hung state until the
45-min turn deadline finally fires.

This module is the single source of truth for that detection so every
adapter and the bridge's dispatcher use the same rule.
"""

from __future__ import annotations

from typing import Any


_TERMINAL_MARKERS = (
    "game is already over",
    "game is over",
)


def is_terminal_tool_error(result: Any) -> bool:
    """True iff a tool result indicates the match has already ended.

    Accepts either the raw server envelope ``{"ok": false, "error":
    {...}}`` or the bridge-unwrapped ``{"error": {...}}`` — both shapes
    appear in practice because _dispatch_tool rewraps the envelope
    before returning to the adapter.

    Keep the marker list tight. If the server grows new game-over
    messages, add them here so the whole fleet picks them up without
    having to chase per-adapter fixes.
    """
    if not isinstance(result, dict):
        return False
    err = result.get("error")
    # Server-native shape: {"ok": false, "error": {...}}
    if err is None and result.get("ok") is False:
        err = result.get("error")
    if not isinstance(err, dict):
        return False
    msg = str(err.get("message") or "").lower()
    return any(m in msg for m in _TERMINAL_MARKERS)
