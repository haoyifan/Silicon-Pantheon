"""Random-action bot that drives a match over the MCP transport.

Purpose-built for the system-test framework: plays a full match by
picking uniformly random legal actions each turn, never calls any
LLM provider, costs nothing per move. Matches exercise the MCP
transport, server game rules, fog filtering, and lobby/room
lifecycle end-to-end.

Mirrors the in-process RandomProvider
(``harness/providers/random.py``) but replaces the in-process
``call_tool`` path with ``ServerClient.call(...)`` so it can play
against a real silicon-serve over the network. The action-selection
logic is identical: shuffle own units, bias toward attacks that
kill, fall back to heal, then random move + wait, finally end_turn.

Interface matches the subset of ``NetworkedAgent`` that
``BotWorker._play_game`` uses:

  - ``_fetch_state()``        → ``dict``  (get_state tool)
  - ``play_turn(viewer, max_turns=)`` → ``None``  (plays one turn)
  - ``summarize_match(viewer)`` → ``None``  (no-op)
  - ``close()``                → ``None``  (no-op)
  - ``adapter_elapsed_s()``    → ``None``  (never waiting on LLM)

So the worker can use a ``RandomNetworkAgent`` anywhere it'd use a
``NetworkedAgent``.
"""

from __future__ import annotations

import logging
import random
from typing import Any

from silicon_pantheon.client.transport import ServerClient
from silicon_pantheon.server.engine.state import Team

log = logging.getLogger("silicon.client.random_agent")


class RandomNetworkAgent:
    """MCP-based random-action player. No LLM, no prompts, no cost."""

    def __init__(
        self,
        client: ServerClient,
        *,
        seed: int | None = None,
    ):
        self.client = client
        self._rng = random.Random(seed)
        # Public-ish attributes the worker expects; unused here but
        # referenced by status-line / runner code.
        self._match_terminated: bool = False

    async def _fetch_state(self) -> dict:
        """Shape matches NetworkedAgent._fetch_state for worker polling."""
        r = await self.client.call("get_state")
        if not r.get("ok"):
            err = (r.get("error") or {}).get("message", str(r))
            if "already" in err and "over" in err:
                self._match_terminated = True
            raise RuntimeError(f"get_state failed: {err}")
        return r.get("result", {})

    async def play_turn(self, viewer: Team, *, max_turns: int) -> None:
        """Play our turn: act with each ready unit, then end_turn.

        ``max_turns`` is accepted for signature compatibility with
        NetworkedAgent but unused — random play doesn't reason about
        the turn budget.
        """
        # Pull own unit IDs from a fresh state snapshot. Fog rules
        # apply to enemies; own units are always fully visible, so
        # ``state.units`` under ``units_of(viewer)`` is reliable.
        state = await self._fetch_state()
        if state.get("status") == "game_over":
            self._match_terminated = True
            return

        my_units = [
            u["id"] for u in state.get("units", []).values() if isinstance(state.get("units"), dict)
        ] if isinstance(state.get("units"), dict) else [
            u["id"] for u in state.get("units", []) if u.get("owner") == viewer.value
        ]
        # ``state.units`` may be serialized as either a dict or a list
        # depending on fog mode + serializer — handle both shapes.
        units_field = state.get("units", [])
        if isinstance(units_field, dict):
            my_units = [
                u["id"] for u in units_field.values()
                if u.get("owner") == viewer.value and u.get("alive", True)
            ]
        else:
            my_units = [
                u["id"] for u in units_field
                if u.get("owner") == viewer.value and u.get("alive", True)
            ]
        self._rng.shuffle(my_units)

        for uid in my_units:
            # State might have changed during this turn (our own
            # actions); skip dead/done units.
            try:
                await self._act_with_unit(viewer, uid)
            except Exception:
                log.exception(
                    "random agent: exception acting with %s; moving on", uid
                )

        # End the turn. If end_turn fails because some unit is stuck
        # in MOVED state (acted but not resolved), force-wait them
        # and retry — same recovery path as the in-process version.
        r = await self.client.call("end_turn")
        if not r.get("ok"):
            err = (r.get("error") or {}).get("message", "")
            if "not your turn" in err or "already over" in err:
                self._match_terminated = True
                return
            # Try to resolve pending units and retry.
            await self._force_wait_all_pending(viewer)
            await self.client.call("end_turn")

    async def _act_with_unit(self, viewer: Team, unit_id: str) -> None:
        """Pick a random legal action for one unit."""
        la_resp = await self.client.call("get_legal_actions", unit_id=unit_id)
        if not la_resp.get("ok"):
            return
        la = la_resp.get("result", la_resp)
        attacks = la.get("attacks", [])
        heals = la.get("heals", [])
        moves = la.get("moves", [])

        # Bias toward attacks that kill without counter-kill. Same
        # rule as the in-process RandomProvider.
        if attacks:
            lethal = [
                a for a in attacks
                if a.get("kills") and not a.get("counter_kills")
            ]
            pool = lethal or attacks
            choice = self._rng.choice(pool)
            from_pos = choice.get("from")
            if from_pos is not None:
                await self._maybe_move(unit_id, from_pos)
            target_id = choice.get("target_id")
            if target_id is None:
                return
            r = await self.client.call(
                "attack", unit_id=unit_id, target_id=target_id,
            )
            if not r.get("ok"):
                await self._wait_unit(unit_id)
            return

        if heals:
            choice = self._rng.choice(heals)
            from_pos = choice.get("from")
            if from_pos is not None:
                await self._maybe_move(unit_id, from_pos)
            target_id = choice.get("target_id")
            if target_id is None:
                return
            r = await self.client.call(
                "heal", healer_id=unit_id, target_id=target_id,
            )
            if not r.get("ok"):
                await self._wait_unit(unit_id)
            return

        # No attacks or heals: random move with 50% probability, then wait.
        if moves and self._rng.random() < 0.5:
            choice = self._rng.choice(moves)
            dest = choice.get("dest")
            if dest is not None:
                r = await self.client.call(
                    "move", unit_id=unit_id, dest=dest,
                )
                if not r.get("ok"):
                    # Move failed; proceed to wait.
                    pass
        await self._wait_unit(unit_id)

    async def _maybe_move(self, unit_id: str, dest: Any) -> None:
        """Move to ``dest`` if different from current position.

        The legal-actions payload's ``from`` is either the unit's
        current tile (no move needed) or a tile the unit could move
        to before attacking/healing. We can't introspect current
        position cheaply from a fresh get_state, so we always try
        the move and swallow the "already there" error.
        """
        try:
            await self.client.call("move", unit_id=unit_id, dest=dest)
        except Exception:
            pass

    async def _wait_unit(self, unit_id: str) -> None:
        try:
            await self.client.call("wait", unit_id=unit_id)
        except Exception:
            pass

    async def _force_wait_all_pending(self, viewer: Team) -> None:
        """Called if end_turn rejects because a MOVED unit hasn't acted."""
        try:
            state = await self._fetch_state()
        except Exception:
            return
        units_field = state.get("units", {})
        if isinstance(units_field, dict):
            iterable = units_field.values()
        else:
            iterable = units_field
        for u in iterable:
            if (
                u.get("owner") == viewer.value
                and u.get("status") == "moved"
                and u.get("alive", True)
            ):
                await self._wait_unit(u["id"])

    # ---- no-op lifecycle hooks to match NetworkedAgent interface ----

    async def summarize_match(self, viewer: Team) -> None:
        """No summary for random play — not worth the server round-trip."""
        return None

    async def close(self) -> None:
        return None

    def adapter_elapsed_s(self) -> float | None:
        """Runner status-line hook. Random play never waits on an LLM."""
        return None
