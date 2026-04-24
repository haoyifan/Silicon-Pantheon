"""Classify server / provider errors that indicate the client is no
longer in the state its current screen assumes.

Background
----------
The TUI is screen-driven: GameScreen assumes ``state == in_game``,
RoomScreen assumes ``state == in_room``, LobbyScreen assumes
``state == in_lobby``. The server's view of our connection can drift
out from under any of them:

  - The heartbeat sweeper auto-concedes a 45-s-idle in-game session
    (``IN_GAME -> FINISHED``), then vacates us back to ``in_lobby``.
    The next game tool we send returns
    ``game tools require state=in_game (current: in_lobby)``.
  - A network blip closes the SSE stream long enough for the
    server to evict us entirely — subsequent calls return
    ``call set_player_metadata first`` (NOT_REGISTERED).
  - The model provider fails terminally (auth revoked, billing
    dead, model removed): the agent_task can't continue and
    conceding is the only forward path.

Before this module the TUI surfaced those errors as a one-line red
string at the bottom of the screen. The user could keep typing into
a screen that was secretly dead — observed in the wild as "I went
AFK, came back, tapped a key, got a cryptic red string". The fix:
detect those error shapes here, return an EvictionInfo, and let the
TUI render a modal alert with an OK button that escorts the user
to the right screen (lobby for state-mismatch / room-loss / provider
issues; login for full session loss).

This module is the single source of truth for the classification so
every screen / background task applies the same rule.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

# Where to send the user after they dismiss the alert.
#
#   "lobby" — connection is alive but our seat / room is gone, OR
#             the server thinks we're in a different state. Lobby
#             can re-poll list_rooms and recover.
#
#   "login" — connection is gone or the server has forgotten our
#             metadata. We need to re-establish the session before
#             anything else works.
Destination = Literal["lobby", "login"]


@dataclass(frozen=True)
class EvictionInfo:
    """Result of classifying an error as an eviction-class signal."""

    title: str          # Short modal title, e.g. "Network disconnected"
    message: str        # One-paragraph explanation for the user
    destination: Destination


# Substring markers (matched against error.message, case-insensitive)
# that mean "your seat / room is gone, return to the lobby and try
# again."
_LOBBY_MESSAGE_MARKERS = (
    "current: in_lobby",
    "current: in_room",
    "no active game",
    "not in any room",
)

# Error codes that put the connection itself in doubt — only a fresh
# login can recover.
_LOGIN_CODES = frozenset({
    "not_registered",
    # Server protocol mismatch: we can't talk to this server with
    # the current build. Re-login lets the user pick a different
    # server URL or upgrade prompts kick in.
    "incompatible_protocol_version",
})

# Error codes that mean "you're no longer where you thought you
# were, but the connection is fine — go back to the lobby."
_LOBBY_CODES = frozenset({
    "tool_not_available_in_state",
    "not_in_room",
    "game_not_started",
    "game_already_over",
})


def _extract_message(err: Any) -> str:
    if isinstance(err, dict):
        m = err.get("message")
        if isinstance(m, str):
            return m
    if isinstance(err, str):
        return err
    return ""


def _extract_code(err: Any) -> str:
    if isinstance(err, dict):
        c = err.get("code")
        if isinstance(c, str):
            return c.lower()
    return ""


def classify_server_error(
    err: Any,
    *,
    on_screen: Literal["game", "room", "lobby", "post_match"],
) -> EvictionInfo | None:
    """Decide whether a server error envelope means "you've been
    evicted from your current screen."

    ``err`` is the ``error`` field of a server response — typically
    a ``{"code": "...", "message": "..."}`` dict but tolerated as
    a bare string or None.

    ``on_screen`` is the caller's screen role so the message can be
    phrased in context ("Removed from game" vs "Removed from room"
    vs "Server forgot us"). Returns None for routine errors like
    "move out of range" so the caller falls back to its existing
    inline error display.
    """
    if err is None:
        return None
    code = _extract_code(err)
    msg = _extract_message(err).lower()

    # Full session loss — login screen is the only place that can
    # rebuild the connection cleanly.
    if code in _LOGIN_CODES or "set_player_metadata" in msg:
        return EvictionInfo(
            title="Network disconnected",
            message=(
                "The server no longer has your session. This usually "
                "means a network blip closed the connection long "
                "enough for the server to evict you. Sign in again "
                "to continue."
            ),
            destination="login",
        )

    if code in _LOBBY_CODES or any(m in msg for m in _LOBBY_MESSAGE_MARKERS):
        if on_screen == "game":
            title = "Removed from game"
            body = (
                "The match ended without you (or the server timed out "
                "your seat after a period of inactivity). Returning "
                "to the lobby."
            )
        elif on_screen == "room":
            title = "Room closed"
            body = (
                "The room is no longer available — the host left, "
                "the match started without you, or the server timed "
                "out your seat. Returning to the lobby."
            )
        else:
            title = "Session changed"
            body = (
                "The server's view of your session changed. "
                "Returning to the lobby to resync."
            )
        return EvictionInfo(
            title=title, message=body, destination="lobby",
        )

    return None


# Match the openai / anthropic provider-error shapes (see
# silicon_pantheon.client.providers.errors.ProviderErrorReason). We
# don't import the enum here to keep this module dependency-free
# for shared use; we accept the reason as a string.
_TERMINAL_PROVIDER_REASONS = frozenset({
    "auth", "auth_permanent", "billing", "model_not_found",
})


def classify_provider_error(
    reason: str | None,
    detail: str | None = None,
) -> EvictionInfo | None:
    """Decide whether a provider failure is unrecoverable enough to
    eject the user back to the lobby.

    Transient reasons (rate_limit, overloaded, timeout) return
    None — the agent retry loop already handles them and the
    user only needs the inline "retrying" footer. Terminal reasons
    (auth, billing, model_not_found) need the user to re-auth or
    pick a different model, which both happen back in the lobby /
    login flow.
    """
    if reason is None:
        return None
    r = reason.lower()
    if r not in _TERMINAL_PROVIDER_REASONS:
        return None
    pretty = {
        "auth": "API key rejected",
        "auth_permanent": "API key revoked",
        "billing": "Provider account out of credit",
        "model_not_found": "Model not available",
    }.get(r, "Model provider error")
    body = (
        f"{pretty}. The agent can't continue this match. "
        f"Returning to the lobby — re-authenticate or pick a "
        f"different model from your profile before starting a new "
        f"match."
    )
    if detail:
        body += f"\n\nDetail: {detail.strip()[:200]}"
    return EvictionInfo(
        title="Model provider error",
        message=body,
        destination="lobby",
    )


# Network failure — caught when a tool call raises (transport
# exception, not a server error envelope). If the underlying client
# can't talk to the server at all, recovery starts at login.
_NETWORK_EXC_HINTS = (
    "closedresourceerror",
    "connectionreseterror",
    "connectionrefusederror",
    "connection refused",
    "broken pipe",
    "transport dead",
    "client is not connected",
    "name or service not known",
    "name resolution",
    "no route to host",
)


def classify_transport_exception(exc: BaseException | None) -> EvictionInfo | None:
    """Decide whether a transport-layer exception means the connection
    to the server is gone and the user needs to re-login.

    Best-effort: matches against the exception type name and string
    body. Returns None for anything that looks like a transient
    timeout (those should be retried in place by the caller, not
    surfaced as eviction)."""
    if exc is None:
        return None
    needle = f"{type(exc).__name__} {exc}".lower()
    if any(h in needle for h in _NETWORK_EXC_HINTS):
        return EvictionInfo(
            title="Network disconnected",
            message=(
                "Lost the connection to the server. Sign in again to "
                "reconnect."
            ),
            destination="login",
        )
    return None


# Convenience: classify whichever of {server_error, provider_error,
# transport_exc} is present. Callers that have all three sources
# (e.g. the agent_task error handler) can collapse the decision into
# one call.
def classify_any(
    *,
    server_error: Any = None,
    provider_reason: str | None = None,
    provider_detail: str | None = None,
    transport_exc: BaseException | None = None,
    on_screen: Literal["game", "room", "lobby", "post_match"] = "lobby",
) -> EvictionInfo | None:
    """Classify the first eviction-class signal among the three
    error sources. None of them set → None."""
    info = classify_server_error(server_error, on_screen=on_screen)
    if info is not None:
        return info
    info = classify_provider_error(provider_reason, provider_detail)
    if info is not None:
        return info
    return classify_transport_exception(transport_exc)


__all__ = [
    "Destination",
    "EvictionInfo",
    "classify_any",
    "classify_provider_error",
    "classify_server_error",
    "classify_transport_exception",
]
