"""Shared helpers used by read-only, mutation, and coach tool modules."""

from __future__ import annotations

import logging

from ..engine.state import GameState, Team, UnitStatus
from ..session import Session
from ...shared.viewer_filter import ViewerContext, currently_visible

_log = logging.getLogger("silicon.fog")


class ToolError(Exception):
    """Raised when a tool call cannot be fulfilled. The error message is
    returned to the agent so it can self-correct.
    """


def _require_active(session: Session, viewer: Team) -> None:
    if session.state.active_player is not viewer:
        raise ToolError(
            f"not your turn (active: {session.state.active_player.value}, you: {viewer.value})"
        )


def _require_own_unit(state: GameState, unit_id: str, viewer: Team) -> None:
    u = state.units.get(unit_id)
    if u is None or not u.alive:
        raise ToolError(f"unit {unit_id} does not exist or is dead")
    if u.owner is not viewer:
        raise ToolError(f"unit {unit_id} is not yours (owner={u.owner.value})")


def _visible_enemies(session: Session, viewer: Team) -> list:
    """Enemy units visible to `viewer` under the session's fog mode.

    Under fog=none this is every alive enemy. Under classic /
    line_of_sight it's filtered to enemies standing on currently-
    visible tiles, matching the fog contract the state-serializer
    uses at filter_state.

    Callers that generate agent-visible hints MUST use this instead
    of state.units_of(enemy) directly -- otherwise the hint leaks
    enemy positions the agent shouldn't be able to see.
    """
    enemy = viewer.other()
    enemies = [u for u in session.state.units_of(enemy) if u.alive]
    if session.fog_of_war == "none":
        return enemies
    ctx = ViewerContext(
        team=viewer,
        fog_mode=session.fog_of_war,  # type: ignore[arg-type]
        ever_seen=session.ever_seen.get(viewer, frozenset()),
    )
    visible = currently_visible(session.state, ctx)
    return [u for u in enemies if u.pos in visible]


def _require_target_visible(
    session: Session, viewer: Team, target_id: str
) -> None:
    """Raise ToolError if the target enemy is currently hidden by fog.

    Safety-in-depth: ``filter_state`` hides enemy units from the
    agent's view under fog, but scenario prompts, historical
    replays, and the initial declaration of units mean an agent
    can still KNOW an enemy's ID even when it's invisible. Without
    this check, an agent could attack a currently-hidden enemy by
    ID alone, turning fog into a one-way information filter that
    offense bypasses.

    Own-team units and dead enemies are always OK — this check
    only fires on alive enemy units under classic / line_of_sight
    fog. Under fog=none it's a no-op.

    Emits a structured log line on every call so we can trace
    fog-boundary targeting attempts even when they succeed — gives
    us the audit trail to debug "how did the agent know this ID"
    reports.
    """
    if session.fog_of_war == "none":
        return
    target = session.state.units.get(target_id)
    if target is None:
        # The engine will raise its own "does not exist" error;
        # we don't want to leak existence by rejecting first.
        return
    if target.owner is viewer:
        return
    if not target.alive:
        # Dead enemies are known history — no fog leak.
        return
    visible = _visible_enemies(session, viewer)
    is_visible = target in visible
    # Always log, even on the happy path. Under fog this fires
    # once per attack attempt — negligible volume, invaluable
    # for debugging fog bugs.
    _log.info(
        "fog_target_check: viewer=%s fog=%s target=%s target_pos=(%d,%d) "
        "visible=%s visible_enemy_ids=%s",
        viewer.value,
        session.fog_of_war,
        target_id,
        target.pos.x,
        target.pos.y,
        is_visible,
        sorted(u.id for u in visible),
    )
    if not is_visible:
        raise ToolError(
            f"target {target_id} is not visible to your team under "
            f"fog of war. You can only target enemies currently in "
            f"sight."
        )


def audit_response_for_fog_leaks(
    result: object, session: Session, viewer: Team, tool_name: str
) -> None:
    """Scan a tool response for enemy unit IDs that are currently hidden.

    Diagnostic only — does NOT modify the response or raise. Logs a
    WARNING with the tool name and the field path(s) where the
    hidden ID appears, so we can chase down any place that forgets
    to apply the fog filter.

    Called by ``game_tools._dispatch`` AFTER ``_apply_filter`` on
    every tool response when fog is enabled. If this ever fires,
    there's a real leak — track it down in the tool that produced
    the response.
    """
    if session.fog_of_war == "none":
        return
    visible_ids = {u.id for u in _visible_enemies(session, viewer)}
    # Add own-team ids — those are always OK to appear.
    for u in session.state.units_of(viewer):
        visible_ids.add(u.id)
    # Dead enemies are OK too (known history).
    for u in session.state.units.values():
        if not u.alive:
            visible_ids.add(u.id)
    enemy_team_initial = viewer.other().value[0]
    # Walk the result. Collect any string that looks like an enemy
    # unit ID (prefix "u_{enemy_initial}_") and isn't in the
    # allowlist.
    leaked: list[tuple[str, str]] = []

    def _walk(obj: object, path: str) -> None:
        if isinstance(obj, dict):
            for k, v in obj.items():
                _walk(v, f"{path}.{k}" if path else str(k))
        elif isinstance(obj, list):
            for i, item in enumerate(obj):
                _walk(item, f"{path}[{i}]")
        elif isinstance(obj, str):
            if obj.startswith(f"u_{enemy_team_initial}_"):
                # It's an enemy-looking ID. Check if it's in the
                # allowlist (visible, own-team, or dead).
                if obj not in visible_ids:
                    # Only real if the unit actually exists in state.
                    if obj in session.state.units:
                        leaked.append((path, obj))

    _walk(result, "")
    if leaked:
        _log.warning(
            "fog_leak_suspect: tool=%s viewer=%s fog=%s leaks=%s "
            "(hidden enemy IDs appeared in response)",
            tool_name, viewer.value, session.fog_of_war, leaked,
        )
