"""Detect tool-response shapes that mean "stop acting this turn."

When the server rejects a tool call with an error that indicates
"the situation has changed out from under you and no further action
makes sense right now", a weak LLM will often apologise and retry
the same tool repeatedly, burning provider tokens and holding the
worker hung until the 45-min turn deadline. grok-3-mini has been
observed in production doing this for:

  - ``game is already over`` (match finished between the adapter's
    get_state and its next end_turn)

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
})


def is_terminal_tool_error(result: Any) -> bool:
    """True iff a tool result means "stop the current play_turn loop."

    Accepts either the raw server envelope ``{"ok": false, "error":
    {...}}`` or the bridge-unwrapped ``{"error": {...}}`` — both shapes
    appear in practice because _dispatch_tool rewraps the envelope
    before returning to the adapter.

    Detects match-ended terminals (``game is already over`` /
    ``GAME_ALREADY_OVER``) and state-loss terminals
    (``GAME_NOT_STARTED`` / ``TOOL_NOT_AVAILABLE_IN_STATE`` /
    ``NOT_REGISTERED`` / ``NOT_IN_ROOM``). From the caller's
    perspective both mean "exit the adapter's iteration loop; the
    host worker's outer loop will re-fetch state and decide what's
    next."

    Notably NOT terminal: ``not your turn`` / ``NOT_YOUR_TURN``.
    The server returns this routinely whenever a tool call lands
    while the opponent is active (e.g. ``get_legal_actions``
    issued reflexively at the start of a polling iteration, or a
    mutating call that races a server-side turn-flip). Treating
    those as terminal makes the openai loop bail out of its
    current iteration on every routine off-turn query, throwing
    away in-progress reasoning and forcing a fresh re-prompt the
    next poll. Verified in the 2026-04-23 system test: 65 false
    ``loop exit (terminal)`` events across 12 agents, none of
    which corresponded to a real match end. The worker's outer
    poll already gates on ``state.active_player == viewer`` to
    avoid sending tool calls on the wrong turn, so a transient
    ``not your turn`` is just noise to recover from in-place.
    """
    if not isinstance(result, dict):
        return False
    # Both envelope shapes — raw ``{"ok": false, "error": {...}}`` and
    # bridge-unwrapped ``{"error": {...}}`` — keep the error dict at
    # the top-level ``error`` key, so a single lookup covers both.
    err = result.get("error")
    if not isinstance(err, dict):
        return False
    code = str(err.get("code") or "").lower()
    if code in _TERMINAL_CODES:
        return True
    msg = str(err.get("message") or "").lower()
    return any(m in msg for m in _TERMINAL_MESSAGE_MARKERS)
