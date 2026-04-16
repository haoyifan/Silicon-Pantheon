# TODO — deferred work

Features and polish that have been explicitly **designed but not
built yet**. Entries here are more than "notes"; each should have
enough context that a future contributor (including me, next week)
can pick it up.

Grouped by area. Priority is rough — the `design decided` ones are
ready to implement in any order; `design open` ones need a round
of design review first.

---

## Provider adapters (beyond Anthropic + OpenAI)

Status: **design decided** in
[`docs/FLEXIBILITY_PROPOSAL.md`](docs/FLEXIBILITY_PROPOSAL.md). Each
is a drop-in `ProviderAdapter` implementation plus catalog entry.

- [ ] **Google Gemini** — use `google-generativeai` SDK. Persistent
      session via the Chat API. Function-call schema differs:
      Gemini rejects several JSON Schema keywords (`minLength`,
      `pattern`) that Anthropic / OpenAI accept; the adapter needs a
      schema-normalizer pass. Estimate: 1–2 days.
- [ ] **Ollama** (local) — no auth beyond a base URL. Use
      `ollama` Python SDK or raw `httpx`. No native persistent-
      session concept — we re-send the transcript manually each turn.
      Most local models don't support tool-use well; the adapter
      should gate on the model declaration. Estimate: 1 day for the
      adapter + 1 day fighting tool-use reliability.
- [ ] **xAI Grok** — OpenAI-compatible API; can largely reuse the
      OpenAI adapter with a different base URL once the refactor is
      clean. Estimate: 0.5 day.
- [ ] **AWS Bedrock** — different SDK (`boto3`), distinct auth chain
      (AWS SDK credentials, no single `BEDROCK_API_KEY`). Tool-use
      varies by underlying model (Claude-via-Bedrock vs.
      Titan-via-Bedrock are completely different formats). Probably
      ship only for Bedrock-Anthropic initially. Estimate: 2 days.
- [ ] **Groq / Together / Perplexity / DeepSeek** — mostly
      OpenAI-compatible endpoints; add catalog entries and let
      the OpenAI adapter drive them. Estimate: a few hours each once
      the catalog is extensible.

---

## Scenario engine v2 — real combat extensions

Status: **schema ready in v1**, runtime deferred. These add
behavior to fields that already exist in the schema as no-ops.

- [ ] **Damage types + tag-aware defense** — implement the
      `damage_profile` / `defense_profile` / `bonus_vs_tags` /
      `vulnerability_to_tags` matrix in `engine/combat.py`. Legacy
      stats (`atk`, `defense`, `res`) keep working for scenarios
      that don't opt in. Estimate: 2–3 days incl. tests.
- [ ] **Weapon triangle as a `tag_cycles` shortcut** — one-line
      shortcut to `bonus_vs_tags` on every weapon. Trivial once
      damage types land. Estimate: half a day.
- [ ] **MP + abilities** — add `mp` resource on Unit, new MCP
      tool `use_ability(ability_id, target)`, new action subtype
      in the engine. Default `mp_per_turn = 0` (no recharge); set
      per-scenario. Estimate: 3–4 days incl. tool, legality, combat
      resolution, TUI rendering.
- [ ] **Inventory + trade** — `trade_item(src_unit, dst_unit, item_id)`
      and `use_item(unit, item_id)` tools. Adjacency check, same-team
      constraint. Combat hooks for equipped items. Estimate: 3 days.
- [ ] **Treasure terrain** — terrain that yields an item when a unit
      ends turn on it. Depends on inventory. Estimate: 0.5 day on
      top of inventory.
- [ ] **Status effects** — buffs/debuffs with duration (e.g.
      "poisoned for 3 turns, -2 HP per end_turn"). Touches combat
      resolution + end_turn flow. Estimate: 2 days.

---

## TUI / client polish

- [ ] **Client-side full scenario preview** (Phase 2e in the design
      doc) — new `describe_scenario` server tool + TUI tooltips in
      the room preview showing unit stats, tags, abilities on
      hover. Estimate: 1 day server + 2 days client.
- [ ] **Post-match lesson browser** — a screen to browse the lessons
      the agent has written across runs, with search / filter by
      scenario. Estimate: 1 day.
- [ ] **In-game pause + re-sync** — some way for a spectator or the
      host to freeze a match to discuss or screenshot. Depends on
      server support. Estimate: 2 days.
- [ ] **Replay viewer inside the TUI** — we already have `silicon-play`
      as a separate CLI; fold a replay-player screen into the main
      TUI so users don't context-switch. Estimate: 1 day.

---

## Tournament mode

Status: **out of scope for v1** per 2026-04-13 review. Design
guidance for when this gets picked up:

- [ ] **Tournament runner** as a separate CLI (`silicon-tourney`) that
      orchestrates a bracket of matches with verified configs.
      Should read a bracket spec (YAML), run N matches sequentially
      or in parallel, collect results into a summary report.
- [ ] **Judge attestation** — tournament runner records
      provider + model choices per player, verifies them against an
      allow-list before each match, and stores a per-match receipt.
- [ ] **Leaderboard / Elo** — track per-agent ratings across matches
      in a persisted file.

---

## Operational / deployment

- [ ] **Public deployment hardening** — currently designed for
      private/trusted use only. For public: JWT auth, abuse
      mitigation, per-user rate limits, structured audit logs, DB
      persistence. (See `docs/PHASE_1_DESIGN.md` scaling-seams
      section for what's already prepped.)
- [ ] **Graceful server upgrade** — today restarting `silicon-serve`
      drops all in-flight matches. A "drain mode" that finishes
      existing matches and refuses new ones would be useful.
- [ ] **Persistent replay archive** — server-side replays
      (`runs-server/...`) accumulate indefinitely. Rotation /
      S3 archival for long-running deployments.

---

## Room / game chat pipeline

Status: **design decided**. Deferred in favor of shipping the room
screen redesign first; the TUI chat panel lands as a placeholder and
gets wired up here.

- [ ] **Server-side chat log per room.** Add `chat: list[ChatMessage]`
      on `Room` (timestamp, author_id, author_display_name, text, team
      if known). New tools: `chat_send(connection_id, text)` posts to
      the current room; echo the full log in `get_room_state` and the
      game state payload so the client can drain and display on each
      refresh.
- [ ] **Agent chat awareness.** Inject the chat log into the agent's
      turn context (like coach messages today). Give the agent an MCP
      tool — `chat_send(text)` — so it can taunt / negotiate. Chat is
      **public** (both teams + both humans see everything) for now;
      per-team scoping can layer on later.
- [ ] **TUI chat panel — real input + scroll.** Replace the
      placeholder in the 5-panel layout with a live input mode (Enter
      to send, scrolling for history) plus a push refresh so incoming
      messages appear without the user having to Tab away and back.
- [ ] **Throttling / length cap.** One-line cap, drop-the-newlines
      sanitization, per-author rate limit (e.g. 1 msg/sec). Agents
      could otherwise flood the channel on every turn.

---

## Audit residue (known-but-not-fixed)

Surfaced during the second-pass deep audit of the multi-provider /
flexible-scenarios work. Each is a real gap; left in place because
no shipping scenario hits it or because the fix is bigger than the
current symptom warrants.

- [ ] **`terrain_types` override of built-ins is partial.** A scenario
      that says `terrain_types: { plain: { passable: false } }` only
      changes tiles explicitly listed in `board.terrain`. Any tile
      not enumerated falls through to `Board.tile()`'s synthetic
      default `Tile(type="plain")`, which uses dataclass defaults
      and ignores the per-scenario table. Workaround: enumerate every
      tile. Real fix: pre-populate `Board.tiles` with the resolved
      terrain spec for every position, or have `Board.tile()` consult
      a state-level terrain-types table.

- [ ] **`describe_scenario` returns inconsistent shapes.** Built-in
      unit classes are serialized to a fixed key set; custom classes
      pass through verbatim (may have extra/missing keys). Built-in
      terrain types come back as `{}` so the UI can't display their
      default `move_cost` / `def_bonus` / `heals`. Either serialize
      both built-ins and custom uniformly, or have the client pull
      defaults from a shared constants module.

- [ ] **`PluginRule.module` field is dead.** PluginRule reads only
      `check_fn`; the `module:` key in YAML is silently ignored.
      Either drop the field (breaking change for any author who put
      it in YAML) or actually scope lookups to a named submodule
      (would matter once a scenario needs more than one plugin file).

- [ ] **Narrative `on_turn_start` event without an explicit `turn:`
      fires only on the first turn.** Consequence of the once-only
      mechanism. Authors writing `{trigger: on_turn_start, text: ...}`
      probably expected "every turn." Either document loudly or add
      an explicit `every: true` opt-in.

- [ ] **`HoldTile.consecutive_turns` counts end_turns, not full game
      turns.** `consecutive_turns: 3` means hold across three
      end_turn events ≈ 1.5 game rounds. Most authors will read
      "turns" as full rounds. Fix: compare to `state.turn` snapshots
      instead of incrementing on every end_turn.

- [ ] **`games/_test_plugin/` ships in the games directory and shows
      up in production `list_scenarios`.** UX noise; an end user
      could try to launch it and get a tiny synthetic 4×4 match.
      Either move test scenarios out of `games/` (a tests-only
      directory the engine knows about) or filter underscore-prefixed
      names from `list_scenarios`.

- [ ] **Narrative events aren't surfaced in real time to the TUI.**
      They land in `state._narrative_log`, get drained into
      `replay.jsonl` after each action, but the TUI only sees them
      via post-match replay download. F.7 in the original plan; the
      data is in hand, the rendering layer needs a small story line
      on the game screen that subscribes to the action hook and reads
      the in-flight log before drain (or the server has to include
      `narrative_events` in the action result).

- [ ] **Built-in terrain `class_overrides` not supported by override
      path either.** When a scenario declares
      `terrain_types: { forest: { ...new fields... } }`, the new
      fields take effect, but existing class_overrides on the
      built-in (none today; future-proofing) wouldn't merge — the
      override wholesale replaces. Document or implement merge.

---

## Smaller polish

- [ ] **Credentials file version field + migration** — add a
      `schema_version: 1` to `credentials.json`; silent migrate on
      read, error on newer version. Consistent with the scenario
      schema story.
- [ ] **Coach message to the opponent's agent** (ops-enabled) — let
      the operator push a message into either team's coach queue
      for testing / debugging / humor. Estimate: 1 hour.
- [ ] **Scenario validator CLI** — `silicon-validate path/to/config.yaml`
      that loads a scenario through the full engine validation
      pipeline and reports errors without starting a match. Great
      for scenario authors. Estimate: 0.5 day.
- [ ] **Keyring dependency marker** — `pyproject.toml` gains
      `keyring` as an optional extra (`pip install silicon-pantheon[keyring]`)
      so users who don't want it don't pay the install cost.
- [ ] **Anthropic adapter — no `disable_parallel_tool_use` knob.** The
      Claude Agent SDK (`ClaudeSDKClient`) doesn't expose it; we'd need
      to switch to raw `anthropic.AsyncAnthropic` calls and pass
      `tool_choice={"type":"auto","disable_parallel_tool_use":true}`.
      Claude doesn't show grok-3-mini's "batch the whole turn" pathology
      in practice, so this is a defense-in-depth item, not a present
      bug. OpenAI / xAI / Codex adapters all enforce one-tool-per-step
      via the API param.

- [ ] **Server-side Layer 3 (force observation between mutating
      actions).** Today both Layer 1 (`parallel_tool_calls=False` on
      the LLM request) and Layer 2 (adapter drops extras + synthesises
      `dropped_parallel_call` errors) live client-side. A non-LLM
      client that bypasses our adapter could still hammer the server
      with mutating calls back-to-back. If that becomes a real concern,
      add a connection-level `requires_observation` flag: every
      mutating tool sets it; reads (`get_state` etc.) clear it; mutating
      calls reject with `OBSERVATION_REQUIRED` while it's set. Cost:
      one bool on the connection + a check at the top of each mutating
      handler. Defer until we have an actual abuse case.

- [ ] **Mid-turn compaction pass.** `_compact_prior_turns` only runs
      at turn boundaries. Within a single turn with many tool calls
      the transcript can still grow unboundedly between compactions —
      a pathological turn today can accumulate 30+ tool results before
      the HARD_TOKEN_LIMIT (180k) force-breaks the loop. A mid-turn
      pass that stubs completed tool results once they're ~2 iterations
      old would bound per-turn growth. Needs care: the model sometimes
      reasons off the exact result of its last call or two, so the
      stubbing window matters. Estimate: half a day + a test that pins
      which tool-results are load-bearing within the current turn.

- [ ] **Cache-friendly compaction cadence.** Every cross-turn
      compaction rewrites prior messages in place, which invalidates
      Anthropic's (and OpenAI's newer-SDKs') prompt cache from the
      first rewritten item onward. We pay one cache miss per turn,
      rebuilding the cached prefix from there. Could compact only at
      coarser thresholds (e.g. every 3 turns, or when est tokens
      cross a watermark), trading some transient bloat for a stable
      prefix that the cache can hold. Measure before optimizing —
      the current scheme is cheap enough at ≤30-turn matches that
      this may not matter in practice.

- [ ] **Additional success-side / error-side agent hints** (continuation
      of the 7b2c88c "server error hints" + B1/C1 work). Tracked here
      because each is small but the queue matters:

      - `wait` error path: when the unit is DONE, list units that can
        still act.
      - `simulate_attack` errors: mirror the `attack` enrichment (dead
        target → alive enemy list; out-of-range → in-range enemy list).
      - "not your turn" errors: quote the start-of-turn message header
        the agent should look for.
      - `attack` success response: add `attacker_status: done` so the
        model stops trying to act on a spent attacker.
      - `end_turn` success response: add compact turn-summary
        `{turn: N → N+1, active: me → other, remaining_turns: X}` so
        the agent doesn't need a get_state + arithmetic.
      - Turn-prompt "last turn outcome" block: auto-render damage
        dealt / taken / kills / losses since the previous turn so
        get_history calls become rare.
      - `get_pending_actions` read tool: same info pending_action now
        surfaces in get_tactical_summary, but standalone for agents
        that don't fetch the full tactical digest.
      - Post-move tactical_summary refresh: `move` response currently
        includes next_actions (attack / heal target IDs). Consider
        embedding a mini tactical delta so the agent sees how the
        move changed opportunities / threats without a follow-up
        get_tactical_summary call.
      - Move-then-attack opportunities in `get_tactical_summary`:
        today only attacks from CURRENT positions are listed. Adding
        "if u moves to X, it can attack target Y for Z damage" would
        save the back-and-forth of simulate_attack(from_tile=...)
        exploration. Bigger search space; worth it for scenarios
        with many reachable attack positions.

- [ ] **Explicit `cache_control` hints.** We benefit from opportunistic
      provider-side prompt caching but don't annotate cache boundaries.
      Adding an `"cache_control": {"type": "ephemeral"}` marker on
      the system prompt block (and maybe on the compacted prior-turns
      block) tells Anthropic "cache everything up to here" and lets
      them hold the prefix more aggressively. Should be a single-line
      addition per adapter once we've verified the SDK version we
      use supports it. Low-priority; measure cache hit rate first.

- [ ] **Ported `silicon-server` (stdio MCP wrapper)** — the legacy
      stdio variant hasn't been touched in a while; confirm it
      still works or deprecate it. Current stance: quietly works but
      unused since silicon-serve (streamable HTTP) replaced it.

---

## Design open — needs another review round

- [ ] **MP recharge model alternatives** — today's design defaults
      to `mp_per_turn = 0` (no recharge). Is it worth supporting
      "recharge N per end_turn" vs "recharge fully at turn start" as
      a scenario knob? Depends on whether players want FE-style
      (fully recharge) or DnD-style (slow recharge). Cheap to
      support both; decide after playtesting v2.
- [ ] **Scenario marketplace** — way for non-operator users to
      submit scenarios for review. PR workflow into the operator's
      repo is the current answer, but a dedicated submission UI + a
      review queue is eventually worth it. Requires sandboxing of
      scenario plugin code before it can be safe.
- [ ] **Reasoning budget per scenario** — scenarios could declare
      `recommended_model: claude-sonnet-4-6` or a complexity
      rating (beginner / intermediate / expert) to help players pick
      an appropriate model. Easy to add to YAML; harder to decide
      what the complexity rating actually means.
- [ ] **Per-unit sight variant: "true sight" / "terrain-modified
      sight"** — currently `sight` is pure Chebyshev distance;
      forest/mountain block. Some scenarios might want "this unit
      sees *through* forests" or "night terrain halves sight". Tag-
      aware? Plugin-based?
