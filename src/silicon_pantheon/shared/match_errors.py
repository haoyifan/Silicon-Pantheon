"""Detect tool-response shapes that mean "stop acting this turn."

When the server rejects a tool call with an error that indicates
"the situation has changed out from under you and no further action
makes sense right now", a weak LLM will often apologise and retry
the same tool repeatedly, burning provider tokens and holding the
worker hung until the 45-min turn deadline. grok-3-mini has been
observed in production doing this for both:

  - ``game is already over`` (match finished between the adapter's
    get_state and its next end_turn)
  - ``not your turn`` (turn was force-ended server-side while the
    adapter was still iterating)

Plus state-loss error codes that indicate the session itself is gone
(GAME_NOT_STARTED / TOOL_NOT_AVAILABLE_IN_STATE / NOT_REGISTERED /
NOT_IN_ROOM) — nothing the agent does this turn will do anything
useful, so we should exit the loop.

This module is the single source of truth for that detection so every
adapter and the bridge's dispatcher apply the same rule. If the server
grows a new signal that means "stop for this turn", add it here and
every adapter picks it up for free.
"""

from __future__ import annotations

from typing import Any


# Substring markers — matched against error.message, case-insensitive.
# Covers engine-level IllegalAction messages that route through
# ErrorCode.BAD_INPUT on the wire (so they can't be distinguished by
# code alone).
_TERMINAL_MESSAGE_MARKERS = (
    "game is already over",
    "game is over",
    "not your turn",
)

# Error codes that unambiguously mean the session / room / game is
# gone. Matched against error.code. (BAD_INPUT is deliberately NOT in
# this list — it's overloaded for every validation failure, only the
# specific messages above count.)
_TERMINAL_CODES = frozenset({
    "game_already_over",
    "game_not_started",
    "tool_not_available_in_state",
    "not_registered",
    "not_in_room",
    "not_your_turn",
})


def is_terminal_tool_error(result: Any) -> bool:
    """True iff a tool result means "stop the current play_turn loop."

    Accepts either the raw server envelope ``{"ok": false, "error":
    {...}}`` or the bridge-unwrapped ``{"error": {...}}`` — both shapes
    appear in practice because _dispatch_tool rewraps the envelope
    before returning to the adapter.

    A match-ended terminal is detected; so is a ``not your turn`` /
    state-loss terminal. From the caller's perspective both mean
    "exit the adapter's iteration loop; the host worker's outer loop
    will re-fetch state and decide what's next."
    """
    if not isinstance(result, dict):
        return False
    err = result.get("error")
    # Server-native shape: {"ok": false, "error": {...}}
    if err is None and result.get("ok") is False:
        err = result.get("error")
    if not isinstance(err, dict):
        return False
    code = str(err.get("code") or "").lower()
    if code in _TERMINAL_CODES:
        return True
    msg = str(err.get("message") or "").lower()
    return any(m in msg for m in _TERMINAL_MESSAGE_MARKERS)
