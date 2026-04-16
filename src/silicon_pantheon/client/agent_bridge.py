"""NetworkedAgent — provider-agnostic orchestrator for one player.

Composition: one `ProviderAdapter` (picked from the user's
credentials, defaulting to Anthropic) + the remote `ServerClient` +
the local scenario + strategy + lesson context. Every
per-turn interaction — building the turn prompt, dispatching tool
calls back through the server, surfacing reasoning — routes through
this class; the concrete LLM never leaks into the TUI.

Persistent-session default: the adapter opens its session on the
first `play_turn` and reuses it until `close()`, so the agent keeps
its chain-of-thought across turns within a single match.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

log = logging.getLogger("silicon.client.agent_bridge")

from silicon_pantheon.client.providers import (
    ProviderAdapter,
    ThoughtCallback,
    ToolSpec,
)
from silicon_pantheon.client.transport import ServerClient
from silicon_pantheon.harness.prompts import (
    _slim_unit,
    build_system_prompt,
    build_turn_prompt_from_state_dict,
)
from silicon_pantheon.lessons import Lesson, LessonStore
from silicon_pantheon.server.engine.state import Team

# Canonical list of game-side tools exposed to the agent. Each entry:
# (name, description, JSON-schema input). connection_id is injected by
# ServerClient.call so it's not part of the schema the agent sees.
def _slim_tool_response(tool_name: str, payload: dict) -> dict:
    """Trim verbose fields from tool responses the agent consumes.

    The TUI's own `get_state` call path doesn't go through this — it
    uses `ServerClient.call` directly for rendering, so art_frames,
    display_name, etc. stay available. Agent-bound responses get the
    same turn-dynamic unit shape the per-turn prompt uses.

    Slimming targets:
      - get_state.units → strip per-unit static stats (handled
        by _slim_unit) — keeps id/owner/class/pos/hp/status/alive.
      - get_state.board.tiles → DROPPED entirely. The terrain map
        is invariant during a match (plugin mutations are rare and
        scenario-announced) and is already shipped in the system
        prompt's map_grid section. Re-shipping all 180+ tiles on
        every get_state call was the dominant within-turn token
        sink — the agent would call get_state 10+ times mid-turn
        and each call cost ~5KB of redundant terrain. Removing it
        cuts a typical 18×10 board's per-call payload by ~70%.
      - _visible_tiles annotation → DROPPED. Same reason — agent
        already knows the board shape from the system prompt and
        can reason about visibility from unit positions.
      - get_unit / get_legal_actions / simulate_attack /
        get_threat_map — already lean.
      - get_history — entries are small action dicts.
    """
    if not isinstance(payload, dict):
        return payload
    if tool_name == "get_state":
        slim: dict = dict(payload)
        if isinstance(slim.get("units"), list):
            slim["units"] = [_slim_unit(u) for u in slim["units"]]
        # Drop the bulky board.tiles array; keep board dimensions
        # + forts (forts mutate during play; shape doesn't).
        if isinstance(slim.get("board"), dict):
            board = dict(slim["board"])
            board.pop("tiles", None)
            slim["board"] = board
        slim.pop("_visible_tiles", None)
        return slim
    return payload


GAME_TOOLS: list[ToolSpec] = [
    ToolSpec(
        "get_state",
        (
            "Get the current game state. Under fog-of-war modes "
            "(classic / line_of_sight), enemy units outside your sight "
            "are hidden and terrain of un-seen tiles shows as 'unknown'. "
            "Dead units still appear with alive=false (they're history, "
            "not targets). Safe to call any time — always your turn or not."
        ),
        {"type": "object", "properties": {}, "required": []},
    ),
    ToolSpec(
        "get_unit",
        (
            "Get a single LIVE unit's full details by id (hp, pos, status, "
            "stats). Returns error for dead units — check the get_state "
            "`units[*].alive` flag first if unsure."
        ),
        {
            "type": "object",
            "properties": {"unit_id": {"type": "string"}},
            "required": ["unit_id"],
        },
    ),
    ToolSpec(
        "get_legal_actions",
        (
            "Enumerate legal moves / attacks / heals / wait for one of "
            "YOUR units on YOUR turn. Server rejects with 'not your turn' "
            "if called while the opponent is active."
        ),
        {
            "type": "object",
            "properties": {"unit_id": {"type": "string"}},
            "required": ["unit_id"],
        },
    ),
    ToolSpec(
        "simulate_attack",
        (
            "READ-ONLY prediction — does NOT change the board. No HP "
            "is deducted, no unit dies, no status flips. The response "
            "carries `kind: \"prediction\"` and uses `predicted_*` "
            "field names (predicted_damage_to_defender, "
            "predicted_defender_dies, ...) so you can tell it apart "
            "from `attack`'s return. To actually deal the damage, "
            "you MUST call `attack(unit_id, target_id)` after this. "
            "Pass `from_tile` to preview from a hypothetical "
            "post-move position. Safe to call any time."
        ),
        {
            "type": "object",
            "properties": {
                "attacker_id": {"type": "string"},
                "target_id": {"type": "string"},
                "from_tile": {
                    "type": "object",
                    "properties": {"x": {"type": "integer"}, "y": {"type": "integer"}},
                    "required": ["x", "y"],
                },
            },
            "required": ["attacker_id", "target_id"],
        },
    ),
    ToolSpec(
        "get_threat_map",
        (
            "For each tile, which visible enemy units can attack you "
            "there. Only includes enemies you can currently see under "
            "fog-of-war. Use before moving a fragile unit."
        ),
        {"type": "object", "properties": {}, "required": []},
    ),
    ToolSpec(
        "get_tactical_summary",
        (
            "Precomputed digest of 'what's worth doing this turn': "
            "attacks you can execute from current positions (with "
            "predicted damage / counter / kill outcomes), threats "
            "against your units, and any units still in MOVED status "
            "pending action. Equivalent to many simulate_attack + "
            "get_threat_map calls in one shot. The per-turn prompt "
            "already carries this at turn-start; call ad-hoc if state "
            "changed meaningfully (e.g. after moving a unit) and you "
            "want a fresh digest before deciding next actions."
        ),
        {"type": "object", "properties": {}, "required": []},
    ),
    ToolSpec(
        "get_history",
        "Recent action history (move/attack/heal/wait/end_turn events).",
        {
            "type": "object",
            "properties": {"last_n": {"type": "integer", "default": 10}},
            "required": [],
        },
    ),
    ToolSpec(
        "get_coach_messages",
        (
            "Drain unread coach messages for your team. Call once at the "
            "start of each turn — the human coach may have left strategic "
            "advice that supersedes your playbook."
        ),
        {
            "type": "object",
            "properties": {"since_turn": {"type": "integer", "default": 0}},
            "required": [],
        },
    ),
    ToolSpec(
        "move",
        (
            "Move one of your READY units to a destination tile. "
            "Destination must be in the unit's reachable set (BFS over "
            "the board, each tile costs `terrain.move_cost` from the "
            "unit's `move` budget; impassable tiles blocked entirely). "
            "After this call the unit's status flips to MOVED — you must "
            "then issue attack/heal/wait for it before `end_turn`."
        ),
        {
            "type": "object",
            "properties": {
                "unit_id": {"type": "string"},
                "dest": {
                    "type": "object",
                    "properties": {"x": {"type": "integer"}, "y": {"type": "integer"}},
                    "required": ["x", "y"],
                },
            },
            "required": ["unit_id", "dest"],
        },
        mutates=True,
    ),
    ToolSpec(
        "attack",
        (
            "Attack an enemy unit from the attacker's current position. "
            "Attacker must be READY or MOVED (not DONE). Target must be "
            "alive, enemy-owned, and within attack range "
            "(Manhattan distance in [rng_min, rng_max]). Defender counters "
            "if it survives and the attacker is in ITS range. Sets "
            "attacker status to DONE."
        ),
        {
            "type": "object",
            "properties": {
                "unit_id": {"type": "string"},
                "target_id": {"type": "string"},
            },
            "required": ["unit_id", "target_id"],
        },
        mutates=True,
    ),
    ToolSpec(
        "heal",
        (
            "Heal an adjacent friendly unit. Healer must have "
            "`can_heal: true` in its class spec (not limited to mages — "
            "any class-declared healer works). Target must be owned by "
            "you, alive, at Manhattan distance 1, and not the healer "
            "itself. Sets healer status to DONE."
        ),
        {
            "type": "object",
            "properties": {
                "healer_id": {"type": "string"},
                "target_id": {"type": "string"},
            },
            "required": ["healer_id", "target_id"],
        },
        mutates=True,
    ),
    ToolSpec(
        "wait",
        (
            "End this unit's turn without attacking or healing. Sets "
            "status to DONE. Use this to clear a MOVED unit that has "
            "nothing useful to do before calling end_turn."
        ),
        {
            "type": "object",
            "properties": {"unit_id": {"type": "string"}},
            "required": ["unit_id"],
        },
        mutates=True,
    ),
    ToolSpec(
        "end_turn",
        (
            "Pass control to the opponent. REJECTED if any of your "
            "units has status MOVED (moved but not yet acted) — send "
            "attack/heal/wait on each such unit first. Must be called "
            "to advance the game."
        ),
        {"type": "object", "properties": {}, "required": []},
        mutates=True,
    ),
    ToolSpec(
        "describe_class",
        (
            "Look up the invariant stats + description for a unit class "
            "(e.g. 'tang_monk', 'knight'). Use this if you forget what a "
            "class does — values never change during a match."
        ),
        {
            "type": "object",
            "properties": {"class": {"type": "string"}},
            "required": ["class"],
        },
    ),
]


def _build_default_adapter(model: str) -> ProviderAdapter:
    """Factory for the provider adapter selected by the model id.

    Consults the credentials store + provider catalog to pick the
    right SDK. Routing rules (applied in order):
      - claude-* / sonnet-* / opus-*  → Anthropic (subscription CLI)
      - gpt-* / o1-* / o3-* / o4-*    → OpenAI (api key)
      - grok-*                        → xAI (api key, OpenAI-compat)
      - unknown                       → credentials.default_provider
                                        or Anthropic

    API-key resolution walks credentials-file → env var → error.
    """
    from silicon_pantheon.client.credentials import (
        CredentialsError,
        load,
        resolve_key,
    )
    from silicon_pantheon.shared.providers import get_provider

    model_lower = model.lower()
    creds = load()
    provider_id: str
    # Saved default_provider wins so a user who picked "OpenAI
    # (subscription)" once doesn't get routed to the api-key adapter
    # the next time just because the model name starts with "gpt".
    if creds.default_provider:
        provider_id = creds.default_provider
    elif "codex" in model_lower:
        # gpt-5-codex etc. — Codex-namespaced models go through the
        # Codex subscription adapter, not the regular OpenAI API.
        provider_id = "openai-codex"
    elif model_lower.startswith(("claude", "sonnet", "opus")):
        provider_id = "anthropic"
    elif model_lower.startswith(("gpt", "o1", "o3", "o4")):
        provider_id = "openai"
    elif model_lower.startswith("grok"):
        provider_id = "xai"
    else:
        provider_id = "anthropic"

    if provider_id == "anthropic":
        from silicon_pantheon.client.providers.anthropic import AnthropicAdapter

        return AnthropicAdapter(model=model)

    if provider_id == "openai-codex":
        from silicon_pantheon.client.providers.codex import (
            CodexAdapter,
            CodexAuthError,
            load_credentials,
        )

        if load_credentials() is None:
            raise RuntimeError(
                "openai-codex selected but no Codex OAuth credentials "
                "are saved yet. Run the TUI's provider picker to log in, "
                "or call codex.login_interactive() programmatically."
            )
        return CodexAdapter(model=model)

    # OpenAI + xAI both use the OpenAI Chat Completions protocol.
    # The only difference is the base URL (xAI = https://api.x.ai/v1)
    # and which env var / credentials entry holds the key. Reuse the
    # one adapter for both.
    if provider_id in ("openai", "xai"):
        from silicon_pantheon.client.providers.openai import OpenAIAdapter

        spec = get_provider(provider_id)
        api_key = _resolve_api_key(provider_id)
        if not api_key:
            env_name = spec.env_var if spec else f"{provider_id.upper()}_API_KEY"
            raise RuntimeError(
                f"{provider_id} adapter selected but no API key could be "
                f"resolved. Set {env_name} or configure credentials.json."
            )
        base_url = spec.openai_compatible_base_url if spec else None
        return OpenAIAdapter(model=model, api_key=api_key, base_url=base_url)

    # Unknown provider_id → fall back to Anthropic so existing
    # default-flow callers don't crash.
    from silicon_pantheon.client.providers.anthropic import AnthropicAdapter

    return AnthropicAdapter(model=model)


def _resolve_api_key(provider_id: str) -> str | None:
    """Walk credentials file → env var for a given provider. None if
    neither turns one up. Kept out of the factory function so the two
    api-key providers (openai, xai) share the same resolution path."""
    from silicon_pantheon.client.credentials import (
        CredentialsError,
        load,
        resolve_key,
    )
    from silicon_pantheon.shared.providers import get_provider

    creds = load()
    cred = creds.providers.get(provider_id)
    if cred is not None:
        try:
            key = resolve_key(cred)
            if key:
                return key
        except CredentialsError:
            pass
    spec = get_provider(provider_id)
    if spec is not None and spec.env_var:
        import os

        return os.environ.get(spec.env_var)
    return None


class NetworkedAgent:
    """Drives one player's turns against a remote silicon-serve."""

    def __init__(
        self,
        client: ServerClient,
        *,
        model: str,
        scenario: str,
        strategy: str | None = None,
        lessons_dir: Path | None = Path("lessons"),
        thoughts_callback: ThoughtCallback | None = None,
        time_budget_s: float = 1800.0,
        adapter: ProviderAdapter | None = None,
        scenario_description: dict | None = None,
    ):
        self.client = client
        self.model = model
        self.scenario = scenario
        self.strategy = strategy
        self.lessons_dir = lessons_dir
        self.thoughts_callback = thoughts_callback
        self.time_budget_s = time_budget_s
        self.adapter: ProviderAdapter = adapter or _build_default_adapter(model)
        # Scenario invariants (classes / terrain / win conditions /
        # starting map). Lazily fetched on the first play_turn if the
        # caller didn't hand one in; cached for the match lifetime and
        # used both in the system prompt and to serve `describe_class`
        # tool calls without round-tripping the server.
        self._scenario_bundle: dict | None = scenario_description
        # Lazily built once per session.
        self._system_prompt_cached: str | None = None
        self._prompt_log = logging.getLogger("silicon.agent.prompts")
        # Bookkeeping for delta turn prompts: we sent turn 1 as a
        # full snapshot to bootstrap; every subsequent turn should
        # send only "what the opponent did since you last acted +
        # your unit state". The session is persistent so the model
        # already remembers the earlier turns — re-shipping the
        # full state dump every turn is wasted tokens AND was the
        # main driver behind the 351k-token blow-up.
        self._turns_played: int = 0
        self._history_cursor: int = 0
        # No-progress watchdog: if the adapter returns N times in a
        # row without the agent calling end_turn, the model is stuck
        # (often hallucinating tool-call XML, or just paralyzed by
        # the prompt). After MAX_NO_PROGRESS retries we force end_turn
        # server-side so the game advances instead of livelocking with
        # the same delta prompt being re-shipped every poll.
        self._no_progress_retries: int = 0

    async def close(self) -> None:
        try:
            await self.adapter.close()
        except Exception:
            pass

    # ---- tool dispatch ----

    async def _dispatch_tool(self, name: str, args: dict) -> dict:
        """Forward a tool call to the remote server.

        Unwraps the server's {ok, result | error} envelope so the agent
        sees raw game-tool payloads; {ok:false} surfaces as an error dict
        the SDK tool wrapper marks as isError.

        Two client-side tools short-circuit before hitting the server:

          - describe_class: serves from the cached scenario bundle the
            system prompt was built from. No round-trip needed; the
            data doesn't change during a match.
          - describe_scenario: same — returns the cached bundle.
        """
        if name == "describe_class":
            slug = args.get("class") or args.get("name")
            spec = ((self._scenario_bundle or {}).get("unit_classes") or {}).get(slug)
            if spec is None:
                return {"error": {"code": "not_found",
                                  "message": f"unknown class {slug!r}"}}
            # Strip render-only fields from the response. The agent
            # uses describe_class to check stats when it forgets;
            # ASCII portrait frames are ~500 bytes of no-value bulk.
            slim_spec = {
                k: v for k, v in spec.items()
                if k not in ("art_frames", "glyph", "color")
            }
            return {"class": slug, "spec": slim_spec}
        if name == "describe_scenario":
            return self._scenario_bundle or {}
        result = await self.client.call(name, **args)
        if result.get("ok"):
            payload = result.get("result", result)
            return _slim_tool_response(name, payload)
        return {"error": result.get("error", {})}

    # ---- state helpers ----

    async def _fetch_state(self) -> dict:
        r = await self.client.call("get_state")
        if not r.get("ok"):
            return {}
        return r.get("result", {})

    def _load_lessons(self) -> list:
        if self.lessons_dir is None:
            return []
        try:
            return LessonStore(self.lessons_dir).list_for_scenario(
                self.scenario, limit=5
            )
        except Exception:
            return []

    # ---- turn orchestration ----

    async def play_turn(self, viewer: Team, *, max_turns: int) -> dict:
        """Drive one half-turn: fetch state, build prompt, let the
        adapter run until the turn ends."""
        log.info(
            "play_turn ENTER team=%s turns_played=%d "
            "no_progress_retries=%d history_cursor=%d",
            viewer.value, self._turns_played,
            self._no_progress_retries, self._history_cursor,
        )
        state = await self._fetch_state()

        # Defensive re-check of turn ownership. _maybe_trigger_agent
        # in the TUI already gates on `active_player == viewer`, but
        # its decision is based on polled state that can be up to
        # ~1s stale (POLL_INTERVAL_S) and the spawning of this task
        # is decoupled from the next poll. Without this second check
        # we'd build a "It is your turn" prompt from fresh state that
        # actually says active_player=<other>, ship it to the LLM,
        # and watch the model earnestly call move/attack/end_turn
        # only for every call to come back as "not_your_turn".
        # Returning early leaves the agent_task done() so the next
        # poll cycle re-evaluates with fresh truth.
        status = state.get("status")
        if status == "game_over":
            log.info("play_turn: match already game_over; skipping")
            return state
        active = state.get("active_player")
        if active != viewer.value:
            log.warning(
                "play_turn: fresh state says active=%s but we are "
                "%s; skipping turn (likely stale-poll race)",
                active, viewer.value,
            )
            return state
        # Build the per-turn user prompt. First turn is a full
        # bootstrap snapshot; every turn after is a delta — just
        # the opponent's actions since our last turn and a compact
        # friendly-unit status line. The adapter keeps a persistent
        # conversation, so the model already remembers the earlier
        # full snapshot and doesn't need a fresh one each turn.
        new_history: list[dict] = []
        # Only fetch history on a fresh turn (not on a retry — we
        # already shipped opponent actions in the previous play_turn
        # entry for this same game turn, and the retry prompt
        # intentionally skips re-shipping them).
        if self._turns_played > 0 and self._no_progress_retries == 0:
            try:
                r = await self.client.call("get_history", last_n=0)
                full_history = (r.get("result") or {}).get("history") or []
                new_history = full_history[self._history_cursor :]
            except Exception:
                log.exception("get_history failed; delta prompt will omit opponent actions")

        # Fetch the precomputed tactical digest (opportunities +
        # threats + pending-action) so the delta prompt can surface
        # "what's worth doing" before the model has to call
        # simulate_attack / get_threat_map manually. Delta turns only
        # — turn 1 uses the bootstrap template which already ships
        # full state; retries also skip it (continuation framing
        # leans on the prior attempt's transcript).
        tactical_summary: dict | None = None
        if self._turns_played > 0 and self._no_progress_retries == 0:
            try:
                r = await self.client.call("get_tactical_summary")
                if r.get("ok"):
                    tactical_summary = r.get("result") or r
            except Exception:
                log.exception(
                    "get_tactical_summary failed; delta prompt will omit tactical section"
                )

        user_prompt = build_turn_prompt_from_state_dict(
            state,
            viewer,
            is_first_turn=(self._turns_played == 0),
            new_history=new_history,
            # retry_n > 0 frames the prompt as a CONTINUATION of the
            # same turn, not a fresh "start of turn". Prevents the
            # model from restarting its "call get_coach_messages"
            # routine on every retry.
            retry_n=self._no_progress_retries,
            tactical_summary=tactical_summary,
        )
        # Lazy-fetch scenario invariants on the first turn. The
        # Anthropic adapter reuses its ClaudeSDKClient across turns
        # so the system prompt is only consumed on turn 1; no point
        # re-building it each call.
        if self._scenario_bundle is None:
            try:
                r = await self.client.call(
                    "describe_scenario", name=self.scenario
                )
                if r.get("ok"):
                    self._scenario_bundle = r
            except Exception:
                log.exception("describe_scenario failed; system prompt will lack scenario data")
        if self._system_prompt_cached is None:
            self._system_prompt_cached = build_system_prompt(
                team=viewer,
                max_turns=max_turns,
                strategy=self.strategy,
                lessons=self._load_lessons(),
                scenario_description=self._scenario_bundle,
            )
            # Log the system prompt once, in full, so operators can
            # tail the client log and audit what the model sees.
            self._prompt_log.info(
                "system_prompt (team=%s, scenario=%s):\n%s",
                viewer.value, self.scenario, self._system_prompt_cached,
            )
        # Every turn's user prompt gets logged too so the full
        # conversation-by-conversation view is reconstructible.
        self._prompt_log.info(
            "turn_prompt (team=%s, turn=%s):\n%s",
            viewer.value, state.get("turn"), user_prompt,
        )
        system_prompt = self._system_prompt_cached

        await self.adapter.play_turn(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            tools=GAME_TOOLS,
            tool_dispatcher=self._dispatch_tool,
            on_thought=self.thoughts_callback,
            time_budget_s=self.time_budget_s,
        )

        # Only advance the delta bookkeeping if the agent actually
        # ended the turn (active_player has flipped to the opponent).
        # If it's still us on the clock, the adapter hit max_iterations
        # / a time budget / a provider error without calling end_turn,
        # and the TUI will retrigger play_turn on the next poll. In
        # that case we must NOT advance _turns_played (next prompt
        # should still be the same shape) and must NOT advance the
        # history cursor.
        post_state = await self._fetch_state()
        turn_ended = post_state.get("active_player") != viewer.value
        if turn_ended:
            self._turns_played += 1
            self._no_progress_retries = 0
            try:
                r = await self.client.call("get_history", last_n=0)
                self._history_cursor = len(
                    (r.get("result") or {}).get("history") or []
                )
            except Exception:
                log.exception("get_history failed; delta cursor not advanced")
            log.info(
                "play_turn EXIT OK turn_ended=True turns_played=%d "
                "history_cursor=%d",
                self._turns_played, self._history_cursor,
            )
        else:
            # Turn didn't end — the TUI will retry on the next poll
            # with a continuation-framed prompt. No client-side
            # watchdog: if the model truly can't finish, the server's
            # turn_time_limit_s is the ultimate bound (currently 30
            # min default, configurable per room). Model freedom over
            # client-side handholding.
            #
            # The counter is kept only so the retry prompt knows
            # retry_n > 0 (triggers continuation framing) and so
            # operators can see from the log how many attempts a
            # stuck turn is running through.
            self._no_progress_retries += 1
            log.warning(
                "play_turn EXIT WITHOUT end_turn (active still %s); "
                "no_progress_retries=%d — TUI will retry on next poll "
                "with continuation prompt; no client-side cap (server "
                "turn_time_limit_s is the forfeit bound)",
                viewer.value, self._no_progress_retries,
            )

        return post_state

    async def summarize_match(self, viewer: Team) -> Lesson | None:
        """Post-match lesson writer. Saves to LessonStore if configured."""
        final_state = await self._fetch_state()
        history_r = await self.client.call("get_history", last_n=60)
        history = (history_r.get("result") or {}).get("history", [])

        lesson = await self.adapter.summarize_match(
            viewer=viewer,
            scenario=self.scenario,
            final_state=final_state,
            action_history=history,
        )
        if lesson is None:
            return None
        if self.lessons_dir is not None:
            try:
                LessonStore(self.lessons_dir).save(lesson)
            except Exception:
                pass
        return lesson
