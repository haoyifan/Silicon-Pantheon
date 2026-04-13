"""Protocol-level constants shared between server and clients.

Connection states, error codes, tool-namespace prefix. Kept free of
server- or client-specific imports so both layers can depend on it.
"""

from __future__ import annotations

from enum import Enum


class ConnectionState(str, Enum):
    """Lifecycle state of one client connection.

    Tool availability is gated by this:
      - ANONYMOUS: only set_player_metadata, heartbeat, whoami.
      - IN_LOBBY:  lobby tools (list/create/join/preview_room).
      - IN_ROOM:   room tools (set_ready, leave_room, get_room_state).
      - IN_GAME:   the full game tool set + download_replay, concede.
    """

    ANONYMOUS = "anonymous"
    IN_LOBBY = "in_lobby"
    IN_ROOM = "in_room"
    IN_GAME = "in_game"


class ErrorCode(str, Enum):
    """Structured error codes returned in tool responses when a call fails."""

    # Auth / state errors
    TOKEN_MISSING = "token_missing"
    TOKEN_INVALID = "token_invalid"
    TOKEN_EXPIRED = "token_expired"
    TOOL_NOT_AVAILABLE_IN_STATE = "tool_not_available_in_state"

    # Validation
    BAD_INPUT = "bad_input"

    # Lobby / rooms
    ROOM_NOT_FOUND = "room_not_found"
    ROOM_FULL = "room_full"
    ALREADY_IN_ROOM = "already_in_room"
    NOT_IN_ROOM = "not_in_room"

    # Game
    NOT_YOUR_TURN = "not_your_turn"
    GAME_NOT_STARTED = "game_not_started"
    GAME_ALREADY_OVER = "game_already_over"

    # Internal
    INTERNAL = "internal"


# The MCP tool-namespace prefix used by the server when registering tools.
TOOL_NAMESPACE = "clash"
