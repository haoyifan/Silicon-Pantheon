# Implementation task breakdown

Companion to [FLEXIBILITY_PROPOSAL.md](FLEXIBILITY_PROPOSAL.md).
Granular task list for the two features, ordered by dependency. One
row per commit; sizes in hours (XS ≤ 1h, S ≤ 3h, M ≤ 6h, L ≥ 1 day).

Rule: every task ends with the test suite green. No task depends on
work in a later task within the same part, so parts can be
parallelized if needed.

## Parts

- **A — Versioning foundation** (unblocks everything)
- **B — Custom unit classes** (smallest useful Feature 2 slice)
- **C — Provider refactor + OpenAI** (Feature 1)
- **D — Custom terrain types**
- **E — Declarative win conditions**
- **F — Plugin rules + scripted narrative**
- **G — Journey to the West scenario**
- **H — Client-side full preview** (Phase 2e polish)

Total: **~60 tasks**, ~25 landable PR-size commits.

---

## Part A — Versioning foundation

Trivial but unblocks the rest.

| # | Commit | Scope | Test | Size |
|---|---|---|---|---|
| A.1 ✅ | Add `PROTOCOL_VERSION` constant | New `PROTOCOL_VERSION = 1` in `shared/protocol.py`. No behavior change. | existing tests pass | XS |
| A.2 ✅ | Server reports version in `set_player_metadata` response | Response includes `server_protocol_version`. | new test: whoami returns the version | XS |
| A.3 ✅ | Client sends its version in `set_player_metadata` args | Transport wrapper passes `client_protocol_version`. | new test: server sees the client version in logs | XS |
| A.4 ✅ | Server refuses mismatched versions | `set_player_metadata` returns `VERSION_MISMATCH` with human-readable message when client != server. | test: older client gets the error | S |
| A.5 ✅ | Add `schema_version` to scenario YAML loader | Default 1 if missing. Refuse to load anything `> 1`. | test: YAML with `schema_version: 99` raises on load | S |
| A.6 ✅ | Add `ErrorCode.VERSION_MISMATCH` to shared enum + docs note | Enum + one-line change to `docs/USAGE.md`. | existing tests | XS |

**Acceptance**: a server and client both declare protocol v1 and
connect cleanly; fabricated v0/v2 clients get rejected with a clear
"upgrade" message.

---

## Part B — Custom unit classes (Feature 2 Phase 2a)

Smallest Feature 2 slice. Backward compatible: no existing scenarios
break.

| # | Commit | Scope | Test | Size |
|---|---|---|---|---|
| B.1 ✅ | Add reserved fields to `UnitStats` | New optional fields: `sight` (already there), `tags: list[str]`, `mp_max: int = 0`, `mp_per_turn: int = 0`, `abilities: list[str] = []`, `default_inventory: list[str] = []`, `damage_profile: dict[str, int] = {}`, `defense_profile: dict[str, int] = {}`, `bonus_vs_tags: list[dict] = []`, `vulnerability_to_tags: list[dict] = []`. Engine ignores everything new. | existing 136 tests | S |
| B.2 ✅ | Scenario loader accepts `unit_classes:` block | Parse + override / extend the built-in `CLASS_STATS` table per-scenario. Unknown class referenced in `armies:` → friendly error. | test: YAML with custom class round-trips through load_scenario + get_state | S |
| B.3 ✅ | `state_to_dict` exposes tags + mp + abilities | So clients can see them even though v1 engine doesn't act. | test: serialized unit includes reserved fields when set | XS |
| B.4 | `describe_scenario`-preview helper returns unit class table | Helper used by future Phase 2e tool; exposes the resolved class table. | unit test against `01_tiny_skirmish` | S |
| B.5 ✅ | Add reserved-field round-trip test | One test loading a synthetic scenario with custom unit classes + reserved fields, plays a few turns, asserts all fields present on get_state. | — | XS |

**Acceptance**: `Journey to the West`'s unit-class block parses; a
player playing with a custom class observes correct stats in the
units table; today's 4-class scenarios are unchanged.

---

## Part C — Provider refactor + OpenAI adapter (Feature 1)

| # | Commit | Scope | Test | Size |
|---|---|---|---|---|
| C.1 ✅ | Add `ProviderAdapter` Protocol + `ToolSpec` dataclass | `client/providers/base.py`. No behavior change yet. | mypy/pyright passes | XS |
| C.2 ✅ | Extract Anthropic adapter | `client/providers/anthropic.py` containing today's `ClaudeSDKClient` code behind the Protocol. `NetworkedAgent` calls it. | existing Anthropic smoke test unchanged | M |
| C.3 ✅ | Add `ProviderSpec` / `ModelSpec` catalog | `shared/providers.py` with Anthropic entries. No wiring yet. | test: catalog enumerates expected models | XS |
| C.4 ✅ | Credentials store module | `client/credentials.py` that reads `~/.clash-of-odin/credentials.json`, resolves `env:` / `keyring:` refs. | unit test with tmp home | S |
| C.5 ✅ | Make `keyring` an optional dependency | `pyproject.toml` extras. Graceful degrade when missing. | test: credentials module imports without keyring | XS |
| C.6 | Login-screen → provider picker transition | New `ProviderAuthScreen` between Login and Lobby; reads from credentials file; if empty, runs the first-run flow. | stub smoke test using fake credentials | M |
| C.7 | API-key auth subscreen | Input-box subscreen for api-key providers; offers env-var detection and keyring save. | stub smoke test | M |
| C.8 | Subscription-CLI auth subscreen | Detect `claude` CLI presence; friendly error if missing. | stub smoke test with mocked `shutil.which` | S |
| C.9 | Token-cost warning banner | Warning text on provider picker; copy per auth mode. | render test via `rich.Console(record=True)` | XS |
| C.10 | Model picker subscreen | List provider's models with cost hints; persist choice. | stub smoke test | S |
| C.11 | Second-run "resume" prompt | If credentials file has a default pair, skip picker with a one-line confirm. | stub smoke test | S |
| C.12 ✅ | OpenAI catalog entry + model list | Add to `shared/providers.py`. | catalog test | XS |
| C.13 ✅ | OpenAI adapter | `client/providers/openai.py`. Uses Responses + function-calling. Transcript-persistent (Conversations or manual replay). Converts MCP tool schemas to OpenAI function format. | in-process smoke: stub OpenAI client, run a 2-turn match end-to-end | L |
| C.14 ✅ | Add OpenAI dependency | `pyproject.toml` adds `openai>=1.0`. | install test | XS |
| C.15 ✅ | Error classifier | `client/providers/errors.py` mapping exceptions → `ProviderError(reason=...)`. Both adapters raise it. | unit test per reason | S |
| C.16 ✅ | Force-concede on `auth` / `billing` | Catch `ProviderError` with those reasons inside `play_turn`; call `concede` tool + transition to PostMatchScreen. | fault-injection test with a fake adapter that raises | S |
| C.17 ✅ | Rate-limit backoff + banner | Exponential backoff with jitter; status banner during retry. | test that backoff respects time budget | S |
| C.18 | Update `docs/USAGE.md` for multi-provider | New "Picking a provider" section; update onboarding flow. | — | XS |
| C.19 | Remove `--provider`/`--model` flag defaults | Flags still accepted but override, not required — defaults come from credentials. | CLI help snapshot test | XS |

**Acceptance**:
- Fresh install with only `OPENAI_API_KEY` env var lands in OpenAI provider path and plays a match.
- Revoking the key mid-match triggers `auth` → force-concede.
- Second run skips straight to "Using openai / gpt-5 — Enter".
- Existing Anthropic path unchanged.

---

## Part D — Custom terrain types (Feature 2 Phase 2b)

Depends on Part A (schema_version gate) and Part B (so scenarios with
both customizations parse).

| # | Commit | Scope | Test | Size |
|---|---|---|---|---|
| D.1 | Extend `Tile` with configurable effect fields | `move_cost`, `defense_bonus`, `magic_bonus`, `heals`, `blocks_sight`, `class_overrides`. Keep existing built-in types working. | engine tests unchanged | M |
| D.2 | Scenario loader accepts `terrain_types:` block | Per-scenario terrain type table; built-ins are defaults. | test: YAML with custom type round-trips | S |
| D.3 | Movement uses configurable `move_cost` | Replace hardcoded cost table in `Tile.move_cost` with the field. | test: scenario with cost=3 terrain observes slower movement | S |
| D.4 | Combat uses configurable defense/magic bonuses | Replace hardcoded forest+2/fort+3 with per-tile fields. | combat test with custom bonus | S |
| D.5 | End-of-turn heal/damage uses configurable `heals` | Positive heal (existing fort behavior) + negative damage (new). | test: unit on `heals: -5` tile loses HP | S |
| D.6 | Line-of-sight uses configurable `blocks_sight` | Fog/sight module consults the flag. | test: custom blocking terrain masks tiles | S |
| D.7 | `class_overrides` respected on movement | Cavalry-cannot-enter-forest becomes data-driven. | test: overridden class is rejected by a legality check | S |
| D.8 | TUI render honors custom glyph + color | Pull from terrain type table when available. | render snapshot test | XS |
| D.9 | Update documentation for custom terrain | — | — | XS |

**Acceptance**: `sand` with cost 2 + cavalry exclusion, `river` with
cavalry-can't-enter + per-class override, `temple` with heal 5 all
work in synthetic test scenarios.

---

## Part E — Declarative win conditions (Feature 2 Phase 2c)

| # | Commit | Scope | Test | Size |
|---|---|---|---|---|
| E.1 | `WinCondition` base class + `WinResult` | `engine/win_conditions/base.py`. Hook interface `check(state, event) -> WinResult | None`. | — | S |
| E.2 | `SeizeEnemyFortRule` | Port today's seize logic into the new framework. Default scenarios get this rule auto-added. | existing seize tests migrate | S |
| E.3 | `EliminateAllEnemyUnitsRule` | Ditto for elimination. | existing elimination tests migrate | S |
| E.4 | `MaxTurnsDrawRule` | Ditto. | existing max-turns test migrates | S |
| E.5 | Wire rule list into `_apply_end_turn` | Replaces hardcoded checks. Built-in rule list for scenarios missing `win_conditions:`. | all engine tests pass unchanged | S |
| E.6 | Scenario loader accepts `win_conditions:` | Parse list; map `type` → rule class. | load test | S |
| E.7 | `ProtectUnitRule` | Fires on `on_unit_killed` when the protected unit dies. | test | S |
| E.8 | `ReachTileRule` | Fires on `on_action_applied` when unit lands on target. | test | S |
| E.9 | `HoldTileRule` | Requires tracking consecutive-turns counter on Session. | test | M |
| E.10 | `ReachGoalLineRule` | Any-team-unit crosses a row or column. | test | S |
| E.11 | Win-condition precedence test | Rules fire in YAML order; first match wins. | test with ambiguous end-state scenario | S |
| E.12 | `describe_scenario` preview of win conditions | The condition list appears in the room preview as human-readable strings. | render test | S |
| E.13 | Update documentation with the DSL | `docs/USAGE.md` gets a "Authoring scenarios" section. | — | S |

**Acceptance**: a scenario with `protect_unit` + `reach_tile` +
`eliminate_all_enemy_units` + `max_turns_draw` plays end-to-end and
declares the right winner under several tested finish paths; older
scenarios work unchanged.

---

## Part F — Plugin rules + scripted narrative (Feature 2 Phase 2d)

Operator-trusted execution of scenario-local Python. No sandbox.

| # | Commit | Scope | Test | Size |
|---|---|---|---|---|
| F.1 | Scenario plugin loader | Reads `<scenario>/rules.py`, exposes declared callables by name. Loaded at scenario registration, not match-start, so bad scenarios fail fast. | test with a synthetic plugin | M |
| F.2 | `PluginWinRule` (type: `plugin`) | Delegates `check()` to a scenario-provided callable. | test with a synthetic rule that always returns win | S |
| F.3 | Terrain `effects_plugin` hook | Tile-enter and end_turn effects via plugin call. | test: plugin-damages-unit scenario | S |
| F.4 | Scenario `narrative:` block parser | `title`, `description`, `intro`, `events: [...]`. Absent → defaults. | load test | S |
| F.5 | Narrative-event engine hook | Fires `on_turn_start` / `on_unit_killed` / `on_plugin` at the right moments. | test: turn-5 intro text fires at turn 5 | S |
| F.6 | Narrative events in `replay.jsonl` | New `narrative_event` entry kind, rendered by `clash-play`. | round-trip test | S |
| F.7 | TUI renders narrative events | Game screen gains a small "story" line that updates as events fire. Auto-clears after N seconds. | render test | S |
| F.8 | Plugin reinforcement example | `spawn_reinforcements` plugin adds units mid-match; test it against a synthetic scenario. | integration test | M |
| F.9 | Docs: authoring plugins + narrative | Security / trust-model paragraph up front. | — | S |

**Acceptance**: a scenario with a plugin `rules.py` that spawns
reinforcements on turn 5 works end-to-end; scenarios without any
narrative section don't regress.

---

## Part G — Journey to the West (shipped scenario)

Content work, lots of balance iteration. Size estimates assume no
major engine fixes fall out.

| # | Commit | Scope | Test | Size |
|---|---|---|---|---|
| G.1 | Author unit classes (pilgrims + monsters) | `games/journey_to_the_west/config.yaml` with all ~14 classes. | scenario loads | M |
| G.2 | Author terrain + map | Mountain passes, river, temple, placement of pilgrims and monsters. | scenario loads | M |
| G.3 | Author win conditions | `protect_unit` on Tang Monk, `reach_tile` for temple, elimination + draw. | scenario loads | S |
| G.4 | Author narrative (intro + turn events) | Intro flavor, turn-5 / turn-10 events. | scenario loads | S |
| G.5 | Balance pass 1: random vs random smoke test | Does the match finish? Does each win-condition fire in at least one seed? | multi-seed smoke test | L |
| G.6 | Balance pass 2: Claude vs Claude playthrough | Human watches a match, notes flagrant imbalances (Sun Wukong too strong, river impassable too restrictive, etc.). Adjust numbers. | hand-verified | L |
| G.7 | Ship scenario + docs mention | Add to the README + USAGE scenarios list. | — | XS |

**Acceptance**: two Claude agents play *Journey to the West* end-to-end
without hitting errors, with at least two seeds producing each of
the win conditions observed in practice.

---

## Part H — Client-side full preview (Phase 2e polish)

| # | Commit | Scope | Test | Size |
|---|---|---|---|---|
| H.1 | `describe_scenario` server tool | Returns the full scenario bundle: unit classes, terrain types, items (future), abilities (future), win conditions, armies, board. | unit test | S |
| H.2 | Client caches scenario description on room enter | New field on `SharedState`. | — | XS |
| H.3 | Room preview: unit class legend | Next to the mini-map, a compact table of each class's stats (HP/ATK/DEF/spd/move) and tags. | render test | S |
| H.4 | Room preview: terrain legend | Glyph / color / properties. | render test | S |
| H.5 | Room preview: win conditions as prose | "Blue wins if Tang Monk reaches (14,5). Blue loses if Tang Monk dies." | render test | S |
| H.6 | Game-screen tooltip on unit glyph | Highlighted cell shows full unit stats + tags + abilities. | render test | M |

**Acceptance**: hovering a unit in the game screen shows full stats,
tags, abilities (even if empty lists); room preview lists all win
conditions verbatim from YAML.

---

## Cross-cutting / documentation

| # | Commit | Scope | Test | Size |
|---|---|---|---|---|
| X.1 | Update `docs/USAGE.md` with scenario authoring guide | — | — | M |
| X.2 | Update `DECISIONS.md` with the multi-provider + schema-versioning decisions | — | — | S |
| X.3 | README mentions OpenAI support + scenario authoring | — | — | XS |
| X.4 | `TODO.md` updated after each phase to remove shipped items | — | — | ongoing |

---

## Suggested execution order

Critical-path ordering — each item unblocks the next when ticked.

1. **A** (foundation) — do all of A first, no value in waiting.
2. **B** (custom unit classes) — small, lets scenario authoring start.
3. **C.1–C.3** (provider refactor + catalog) — zero behavior change
   refactor, lands cleanly without needing auth UI.
4. **C.4–C.11** (credentials + auth screens) — the bulk of the
   onboarding work; can be tested against Anthropic only.
5. **C.12–C.17** (OpenAI adapter + error classifier) — second
   provider comes online. Feature 1 done at this point.
6. **D** (terrain) — scenario author can now describe custom maps.
7. **E** (win conditions) — scenario author can describe richer
   objectives.
8. **F** (plugin rules + narrative) — escape hatch + flavor.
9. **G** (Journey to the West) — flagship scenario ships.
10. **H** (client preview polish) — nice-to-have; the game already
    works without it.

Total calendar time at a day of focused work per L-task:
~**3–4 weeks** for one person running serial. Parts B/C/D/E can be
partially parallelized since they touch different files (~2 weeks
with parallelism).

---

## Task table fields — legend

- **Commit**: imperative short name.
- **Scope**: which files change, what behavior flips.
- **Test**: what test exists (or must be added) to prove it.
- **Size**: XS (<1h), S (<3h), M (<6h), L (1+ day).

All tasks end with the test suite green. If a task is blocked by a
surprise, stop and raise the surprise — don't pile on follow-up
tasks into the same commit.
