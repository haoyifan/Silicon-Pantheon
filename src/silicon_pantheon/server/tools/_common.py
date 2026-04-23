"""Shared helpers used by read-only, mutation, and coach tool modules."""

from __future__ import annotations

import logging
import os

from ..engine.state import GameState, Team, UnitStatus
from ..session import Session
from ...shared.viewer_filter import ViewerContext, currently_visible

_log = logging.getLogger("silicon.fog")


def _fog_attack_enforce() -> bool:
    """Whether attacks on fog-hidden enemies should be REJECTED.

    When True (the default), `_require_target_visible` raises
    `ToolError` — this is the safety-in-depth behaviour shipped
    for production.

    When False, the check is downgraded to log-only: it still
    emits `fog_target_check` (including `visible=False` lines)
    but lets the attack proceed. Use this to reproduce a reported
    fog leak without altering agent behaviour — the agent sees
    the same responses it would have pre-fix, so the sequence
    that led to the leak repeats cleanly.

    Toggle via env var: `SILICON_FOG_ATTACK_ENFORCE=0` disables
    enforcement. Anything else (or unset) enables it.

    Read on every call so operators can flip the flag live by
    editing their systemd environment and restarting the server.
    """
    return os.environ.get("SILICON_FOG_ATTACK_ENFORCE", "1") != "0"


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


def visible_enemy_ids_snapshot(
    session: Session, viewer: Team
) -> frozenset[str]:
    """Public helper: ids of enemies currently visible to ``viewer``.

    Callers take this snapshot *before* a mutation so the
    post-mutation fog audit can allowlist whatever the agent was
    allowed to know when it invoked the tool. See
    ``audit_response_for_fog_leaks`` for the rationale.
    """
    if session.fog_of_war == "none":
        return frozenset()
    return frozenset(u.id for u in _visible_enemies(session, viewer))


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
    from silicon_pantheon.shared.debug import (
        InvariantViolation,
        is_debug,
    )

    enforce = _fog_attack_enforce()
    debug = is_debug()
    _log.info(
        "fog_target_check: viewer=%s fog=%s target=%s target_pos=(%d,%d) "
        "visible=%s enforce=%s debug=%s visible_enemy_ids=%s",
        viewer.value,
        session.fog_of_war,
        target_id,
        target.pos.x,
        target.pos.y,
        is_visible,
        enforce,
        debug,
        sorted(u.id for u in visible),
    )
    if not is_visible:
        # Debug mode supersedes the enforce-flag: a fog violation is
        # always an invariant bug. Crash loudly so the repro surfaces
        # at the exact attack site, not buried in the log.
        if debug:
            raise InvariantViolation(
                f"fog violation: viewer={viewer.value} attacked "
                f"hidden target {target_id} at ({target.pos.x},"
                f"{target.pos.y}); visible enemies were "
                f"{sorted(u.id for u in visible)}"
            )
        if enforce:
            raise ToolError(
                f"target {target_id} is not visible to your team under "
                f"fog of war. You can only target enemies currently in "
                f"sight."
            )
        # Log-only mode (SILICON_FOG_ATTACK_ENFORCE=0) — emit a LOUD
        # warning so the leak is impossible to miss in the log, but
        # let the attack proceed so the reproduction matches the
        # pre-fix behaviour.
        _log.warning(
            "fog_violation_allowed: enforce=False; letting attack through "
            "(viewer=%s target=%s) — THIS IS REPRO MODE, not production",
            viewer.value,
            target_id,
        )


def audit_response_for_fog_leaks(
    result: object,
    session: Session,
    viewer: Team,
    tool_name: str,
    pre_visible_enemy_ids: frozenset[str] | None = None,
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

    ``pre_visible_enemy_ids`` is the set of enemy ids the viewer
    could legitimately see at the moment the tool was invoked,
    snapshotted *before* the engine mutated state. The caller is
    responsible for taking this snapshot under session.lock — see
    ``game_tools._dispatch_inner``. The audit unions it into the
    allowlist so mutations that shrink line-of-sight (e.g. the
    attacker dies on counter-attack, removing LoS to a target that
    was plainly visible when the agent chose it) don't produce
    false-positive "leaks" on input args the agent itself supplied.
    """
    if session.fog_of_war == "none":
        return
    visible_ids = {u.id for u in _visible_enemies(session, viewer)}
    # Add own-team ids — those are always OK to appear.
    for u in session.state.units_of(viewer):
        visible_ids.add(u.id)
    # Dead enemies are OK too (known history). Dead units have been
    # removed from state.units by _apply_attack / _apply_end_turn
    # (see engine/rules.py) and now live in state.fallen_units with
    # hp=0. Iterating state.units here was dead code — it never
    # found a dead unit, because by the time the audit runs dead
    # units are already gone from the live dict.
    for uid in session.state.fallen_units:
        visible_ids.add(uid)
    # Pre-mutation visible enemies: whatever the viewer was allowed
    # to see when the agent picked its action. If the tool's
    # mutation shrank LoS (attacker died, scout moved off a high
    # tile), the post-mutation recompute above will miss them — but
    # the agent's reference to those ids is legitimate.
    if pre_visible_enemy_ids:
        visible_ids |= pre_visible_enemy_ids
    enemy_team_initial = viewer.other().value[0]
    # Walk the result. Collect any string that looks like an enemy
    # unit ID (prefix "u_{enemy_initial}_") and isn't in the
    # allowlist.
    leaked: list[tuple[str, str]] = []

    # Field paths that intentionally surface enemy IDs the viewer
    # already knew pre-action. These are FEATURES, not leaks:
    #
    #   hidden_enemies / revealed_enemies: `move` emits these to
    #     tell the agent which enemies just entered or left sight
    #     as a result of THIS move. The agent saw them on its
    #     previous state snapshot, so the ID is already known —
    #     we surface it so the agent can reason about the fog
    #     delta without a second get_state call. Observed as a
    #     false-positive in the 08_kadesh match 2026-04-20 where
    #     every move triggered `fog leak in move` in debug mode.
    #
    # Keep the matching permissive (startswith): these fields are
    # lists of dicts, so paths look like `hidden_enemies[0].id`.
    FEATURE_FIELDS = ("hidden_enemies", "revealed_enemies")

    def _is_feature_path(path: str) -> bool:
        return any(path.startswith(f) for f in FEATURE_FIELDS)

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
                if obj not in visible_ids and not _is_feature_path(path):
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
        # In debug mode, a leak IS the bug — crash so the stack
        # trace points at the exact tool whose response contained
        # the hidden ID. The response has already been built; we
        # raise after the log line so operators see BOTH the
        # leak description and the stack.
        from silicon_pantheon.shared.debug import InvariantViolation, is_debug
        if is_debug():
            raise InvariantViolation(
                f"fog leak in {tool_name}: hidden enemy IDs in response: "
                f"{leaked}"
            )
