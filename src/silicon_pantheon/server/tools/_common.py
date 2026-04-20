"""Shared helpers used by read-only, mutation, and coach tool modules."""

from __future__ import annotations

from ..engine.state import GameState, Team, UnitStatus
from ..session import Session
from ...shared.viewer_filter import ViewerContext, currently_visible


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
    if target not in visible:
        raise ToolError(
            f"target {target_id} is not visible to your team under "
            f"fog of war. You can only target enemies currently in "
            f"sight."
        )
