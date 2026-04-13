"""Deprecated: use `clash_of_robots.shared.replay_schema` instead.

Kept as a re-export so existing callers keep working.
"""

from clash_of_robots.shared.replay_schema import *  # noqa: F401,F403
from clash_of_robots.shared.replay_schema import (  # noqa: F401
    AgentThought,
    CoachMessage,
    ErrorPayload,
    ForcedEndTurn,
    MatchStart,
    ReplayEvent,
    UnreconstructibleAction,
    action_from_payload,
    parse_event,
)
