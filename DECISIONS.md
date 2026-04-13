# Decisions Log

Running log of design and implementation calls made by Claude during build-out
of Clash Of Robots. Each entry: what was decided, why, and any reversal cost if
we later disagree.

Format:
```
## YYYY-MM-DD — Short title
**Decision:** ...
**Why:** ...
**Reversal cost:** low / medium / high — what would change if we undo this.
```

---

## 2026-04-12 — Project & tooling baseline
**Decision:** Project named "Clash Of Robots", Python package `clash_of_robots`,
Python 3.12, `uv` for env management, `ruff` for lint+format, `pyright` (basic
mode) for type checking enforced from Phase 1, `pytest` for tests, src layout
(`src/clash_of_robots/...`).
**Why:** User explicitly chose name, Python 3.12, ruff, and asked for early type
checking. src layout is the modern Python default and prevents the common
"importing from project root" foot-gun.
**Reversal cost:** Low for tooling swaps; medium for package name (touches
imports everywhere).

## 2026-04-12 — Game scenarios as YAML in `games/<name>/config.yaml`
**Decision:** Scenarios live under `games/<scenario_name>/config.yaml`, each
folder self-contained with terrain + armies + starting positions + win rules +
optional README. Replaces the earlier `maps/*.json` plan.
**Why:** User asked for flexibility to iterate from simple to complex scenarios
for backtesting. YAML is more hand-editable than JSON. Folder per scenario lets
each ship with its own README explaining what it tests.
**Reversal cost:** Low — scenario loader is a single module.

## 2026-04-12 — Orchestrator-driven turn handoff (Option C) for MVP
**Decision:** Phase 3-7 use the orchestrator to invoke each harness's
`play_turn()` synchronously. MCP server-initiated notifications (Option B) are
deferred to Phase 8 when remote/server play matters.
**Why:** Same process tree, simplest mental model, ~10 lines of code. User
confirmed "local first, but eventually a server."
**Reversal cost:** Medium — Phase 8 will need to add an MCP notification
listener to the harness loop.

## 2026-04-12 — Claude Agent SDK as primary harness (Phases 5-6)
**Decision:** Use Claude Agent SDK with the user's existing Claude Max
subscription auth. No `ANTHROPIC_API_KEY` required. Add provider-specific
clients (OpenAI, etc.) only in Phase 7 for cross-model matches.
**Why:** User has Max subscription; SDK reuses Claude Code auth; bonus that the
SDK provides agent loop / tool calling / compaction primitives for free.
**Reversal cost:** Low — provider abstraction (`harness/providers/base.py`)
isolates the choice. Can swap to direct Anthropic SDK or roll-your-own later.

## 2026-04-12 — Default game rules (gaps the user accepted with "sounds good")
**Decision:** Recorded in `GAME_DESIGN.md`:
- Counter range = defender's `RNG`; doubling applies on counters.
- Mage cannot self-heal; heals adjacent (Manhattan 1) ally only; counts as action.
- No stacking; one unit per tile.
- Action order within a turn: any unit order, but each unit's move-then-act is
  contiguous; once `done`, locked for the turn.
- Blue goes first; tournament code swaps colors across rounds.
- Home forts owned at start; mid-map forts neutral.
**Why:** User confirmed all defaults. Captured here so Phase 1 implementation
has a single source of truth and any future disagreement is easy to spot.
**Reversal cost:** Low for any individual rule (engine code is small); medium
if multiple change at once (test suite needs updates).

## 2026-04-12 — Tool layer is callable directly; MCP stdio is a thin wrapper
**Decision:** The game tools (`get_state`, `move`, `attack`, ...) are plain
Python functions living in `server/tools/` with a shared tool registry. They
operate on a `Session` object that bundles `GameState` + coach message queues +
replay writer. The MCP stdio server (`server/main.py`) is a thin wrapper that
exposes these same tools over the MCP protocol for remote/future use. For
phases 3-7 the orchestrator calls the tool registry **directly in-process** —
no subprocess, no stdio round-trip. This avoids the fundamental stdio-is-1:1
constraint (two agents cannot share one stdio MCP server) while keeping a real
MCP server available for Phase 8 remote play.
**Why:** Stdio MCP only supports one client per server process. Two agents
playing the same match would require either HTTP/SSE transport (Phase 8 work)
or running two server instances with synced state (complex, fragile). The
orchestrator-driven turn model (decision above) already serializes actions, so
in-process tool calls are sufficient for MVP; the protocol layer becomes
valuable only when agents are remote.
**Reversal cost:** Low — because tools live on a registry with a uniform
interface, swapping "direct call" for "call via MCP client" is localized.

## 2026-04-12 — Tools live together in `server/tools/__init__.py`
**Decision:** All tool functions in one module file rather than one file per
tool (as originally sketched in PLAN.md).
**Why:** Each tool is ~10-30 lines; splitting into 8+ files adds import noise
and makes cross-tool coordination harder for no real benefit. The registry
pattern already provides discoverability.
**Reversal cost:** Trivial — move functions into per-tool files if one ever
grows large.

## 2026-04-12 — Agents are fresh-per-turn, not persistent sessions
**Decision:** `AnthropicProvider.decide_turn` calls the SDK's one-shot `query()`
each turn. No conversation history carries between turns — the agent is
re-spawned fresh every turn with the same system prompt (rules + strategy) and
a new per-turn prompt containing a current state snapshot.

Cross-turn continuity is provided by the **server-side state**, not the
agent's context: the `GameState`, coach message queue, and action history
(retrievable via `get_history`) persist across turns. What does *not* persist
is the agent's chain-of-thought, multi-turn plans, or internal reasoning.

**Why:**
- No context bloat — a 30-turn match stays ~20-30k tokens per turn instead of
  ballooning to ~450k by turn 30.
- No compaction required — summarizing tactical reasoning is hard and easy to
  get wrong.
- Failure isolation — a bad turn (e.g., tool-call loop) doesn't poison
  subsequent turns.
- Board state often *is* the plan's memory: a cavalry 2 tiles from the enemy
  fort telegraphs its own intent.

**Reversal cost:** Low-medium. Swap `query()` → `ClaudeSDKClient` (persistent),
instantiate once per match in `__init__`, send a "your turn" message per
`decide_turn`. Add compaction when context nears 80% full. ~50 lines in
`anthropic.py`.

**TODO for future evaluation:** Once real agent matches have been played,
decide whether tactical incoherence across turns justifies the switch. If
agents seem to "forget what they were doing," flip to persistent sessions. If
they play coherently, stay fresh-per-turn.

## 2026-04-12 — Python 3.13 in practice (pyproject requires ≥3.12)
**Decision:** uv selected Python 3.13.5 on this machine; pyproject pins ≥3.12.
No code changes needed; 3.12-only features (match, generic syntax) all work.
**Why:** ≥3.12 constraint is satisfied by 3.13; uv picks the best available.
**Reversal cost:** None.
