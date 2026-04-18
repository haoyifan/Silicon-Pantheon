"""Deprecated: use `silicon_pantheon.shared.replay_schema` instead.

Kept as a re-export so existing callers keep working.
"""

from silicon_pantheon.shared.replay_schema import *  # noqa: F401,F403
from silicon_pantheon.shared.replay_schema import (  # noqa: F401
    AgentThought,
    CoachMessage,
    ErrorPayload,
    ForcedEndTurn,
    MatchEnd,
    MatchPlayers,
    MatchStart,
    ReplayEvent,
    UnreconstructibleAction,
    action_from_payload,
    parse_event,
)
