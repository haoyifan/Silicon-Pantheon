# Phase 1 design — networked clash-of-robots

Detailed plan for the backend/client split agreed in
[NETWORKED_ARCHITECTURE_REVIEW.md](NETWORKED_ARCHITECTURE_REVIEW.md)
and [NETWORKED_ARCHITECTURE_REVIEW2.md](NETWORKED_ARCHITECTURE_REVIEW2.md).

**Phase 1 goal:** two friends on different machines can host, browse,
join, ready up, play through, and download the replay of a fog-of-war
match against each other, using any tool-using LLM or a human sitting
behind the TUI. No database, no public deployment, no spectators.

**Explicitly out of scope for Phase 1:**

- Spectator live-viewing (can be added in Phase 2; replay download
  covers the "watch someone else's game" need for now).
- JWT / OAuth / attestation auth. Opaque per-session tokens only.
- Durable persistence. Games live in memory; server restart = all
  in-flight matches lost.
- Matchmaking, ladders, reputation.
- Public-scale abuse prevention beyond turn/heartbeat timers.
- Horizontal scaling across processes.

Status: **proposal, not implemented.** Review before any refactor.

---

## Table of contents

1. [Sub-phases](#sub-phases)
2. [Repo layout after the split](#repo-layout-after-the-split)
3. [Protocol: lobby + game MCP tools](#protocol-lobby--game-mcp-tools)
4. [Token lifecycle](#token-lifecycle)
5. [Heartbeat + disconnect state machine](#heartbeat--disconnect-state-machine)
6. [Ready → auto-start countdown](#ready--auto-start-countdown)
7. [Fog of war](#fog-of-war)
8. [Viewer filter design](#viewer-filter-design)
9. [Scenario config additions](#scenario-config-additions)
10. [Client TUI flow](#client-tui-flow)
11. [Replay download](#replay-download)
12. [Scaling seams preserved for Phase 2](#scaling-seams-preserved-for-phase-2)
13. [Phase 1 done-definition checklist](#phase-1-done-definition-checklist)

---

## Sub-phases

Phase 1 is big. Break it into shippable increments so we can validate
the protocol before we dress it up:

- **1a — Protocol core.** `clash-serve` accepts MCP+SSE; `clash-join`
  authenticates, creates/joins a single hard-coded room, and runs one
  game end-to-end between two CLI-driven clients (random bots).
  *Defines the contract.*
- **1b — Lobby.** Full room lifecycle: list/create/preview/join/leave/
  ready/auto-start. Still CLI-driven clients.
- **1c — Fog of war.** Viewer filter layer, `sight` stats, classic
  fog mode. Scenario config field.
- **1d — TUI client.** Login → lobby → room preview → game → post-
  match screens. Real rendering.
- **1e — Disconnect + replay download.** Heartbeat, timers,
  reconnect, `download_replay` tool, polish.

Each sub-phase is independently testable. Ship 1a→1e in order.

---

## Repo layout after the split

Keep the existing `harness/` and `match/` so `clash-match` and
`clash-play` still work locally — they're the fastest iteration loop
for engine changes.

```
src/clash_of_robots/
├── engine/                  # unchanged, pure game logic
│   ├── board.py
│   ├── combat.py
│   ├── rules.py
│   ├── state.py
│   └── scenarios.py
├── shared/                  # NEW — types used by both sides
│   ├── __init__.py
│   ├── protocol.py          # MCP tool schemas, payload dataclasses
│   ├── fog.py               # pure visibility computation
│   ├── viewer_filter.py     # pure state→view redaction
│   ├── replay_schema.py     # MOVED from match/replay_schema.py
│   └── player_metadata.py   # PlayerMetadata dataclass
├── server/                  # authoritative backend
│   ├── app.py               # MCP+SSE server wiring (FastMCP HTTP)
│   ├── auth.py              # token issue/validate
│   ├── rooms.py             # Room, RoomRegistry
│   ├── game_runner.py       # per-match asyncio task
│   ├── tools/
│   │   ├── lobby_tools.py   # list/create/preview/join/leave/ready
│   │   └── game_tools.py    # the 13 existing tools, remote-safe
│   ├── heartbeat.py         # timers + disconnect state machine
│   ├── main.py              # `clash-serve` CLI entry
│   └── engine/              # (existing, still here)
├── client/                  # NEW — remote-agent + TUI client
│   ├── transport.py         # MCP+SSE client wrapper
│   ├── agent_bridge.py      # connects local LLM to remote MCP tools
│   ├── app.py               # TUI application (rich/textual)
│   ├── screens/
│   │   ├── login.py
│   │   ├── lobby.py
│   │   ├── room.py
│   │   ├── game.py
│   │   └── post_match.py
│   └── main.py              # `clash-join` CLI entry
├── harness/                 # unchanged — local bot drivers
├── match/                   # unchanged — `clash-match`, `clash-play`
└── lessons.py               # unchanged
```

`shared/` is the load-bearing addition: protocol types + fog/filter
logic have to be pure so both server (authoritative) and client
(rendering) can import them.

**New CLI entry points** (`pyproject.toml`):

- `clash-serve` → `server.main:main`
- `clash-join`  → `client.main:main`

---

## Protocol: lobby + game MCP tools

One MCP+SSE endpoint. Tool availability is scoped by the connection's
current **auth state**: `{anonymous, in_lobby, in_room, in_game}`. An
ill-scoped call returns an MCP error with code `tool_not_available_in_state`.

### Always available

| Tool | Input | Returns |
|---|---|---|
| `set_player_metadata` | `{display_name, kind, provider?, model?, version?}` | `{ok}` |
| `heartbeat` | `{}` | `{server_time}` |
| `whoami` | `{}` | current state + metadata |

### In `anonymous` state

`set_player_metadata` transitions to `in_lobby`.

### In `in_lobby` state

| Tool | Input | Returns |
|---|---|---|
| `list_rooms` | `{}` | `[RoomSummary]` |
| `preview_room` | `{room_id}` | `RoomPreview` (scenario map + seats + settings) |
| `create_room` | `{config: RoomConfig}` | `{room_id, slot, token}` |
| `join_room` | `{room_id}` | `{slot, token}` |

`RoomConfig`:

```json
{
  "scenario": "02_basic_mirror",
  "max_turns": 20,
  "team_assignment": "fixed" | "random",
  "host_team": "blue" | "red",        // only used when team_assignment="fixed"
  "fog_of_war": "classic" | "line_of_sight" | "none",
  "turn_time_limit_s": 180
}
```

Tokens returned from `create_room` / `join_room` transition the
connection to `in_room`.

### In `in_room` state

| Tool | Input | Returns |
|---|---|---|
| `set_ready` | `{ready: bool}` | `{ok, autostart_in_s?}` |
| `leave_room` | `{}` | `{ok}` |
| `get_room_state` | `{}` | `RoomState` (seats, ready flags, autostart timer) |

When both seats are filled and both `ready=true`, the server starts a
**10 s auto-start countdown** (see
[Ready → auto-start](#ready--auto-start-countdown)). On expiry the
state transitions to `in_game` and the team assignment is finalized.

### In `in_game` state

The existing 13 game tools — `get_state`, `get_unit`,
`get_legal_actions`, `simulate_attack`, `get_threat_map`,
`get_history`, `get_coach_messages`, `move`, `attack`, `heal`, `wait`,
`end_turn`, `send_to_agent` — **all available, all filtered through
the viewer filter**.

Plus:

| Tool | Input | Returns |
|---|---|---|
| `concede` | `{}` | `{ok}` — player resigns |
| `download_replay` | `{}` | full replay JSONL (available after game_over) |

All game tools return `tool_not_available_in_state` if called from
`anonymous`, `in_lobby`, or `in_room`.

---

## Token lifecycle

**No persistent identities in Phase 1.** Tokens are session-scoped.

1. **Client connects to MCP+SSE.** Server assigns a `connection_id`.
2. **Client calls `set_player_metadata`.** Connection transitions
   `anonymous → in_lobby`. Metadata stored on connection object.
3. **Client creates or joins a room.** Server issues a token
   `(connection_id, room_id, slot)` and stores it in an in-memory
   `TokenRegistry`. Returned in the response. All subsequent tool
   calls must carry the token as an MCP auth header; middleware
   resolves it to `(room_id, slot)` and injects that into the handler
   context.
4. **Token invalidated when:**
   - `leave_room` is called.
   - Game ends (but `download_replay` stays available briefly — see
     below).
   - Hard-disconnect timeout fires.
   - Connection closes.

The auth middleware exposes a single interface:

```python
def resolve_token(token: str) -> tuple[RoomId, SlotId] | None: ...
```

Phase 2 swaps the implementation for JWT without touching handlers.

After `game_over`, the token stays valid for **60 s** so the client
can call `download_replay`; then it's purged. Clients should download
replays eagerly.

---

## Heartbeat + disconnect state machine

Client sends `heartbeat` every **10 s**. Server tracks
`last_heartbeat_at` per connection. A single asyncio task per server
sweeps connections once a second and fires transitions.

Timer budgets:

| Context | Trigger | Effect |
|---|---|---|
| Anywhere | `now - last_heartbeat > 30 s` | connection → `soft_disconnect` |
| `in_lobby` | 30 s in `soft_disconnect` | connection dropped (no seat to vacate) |
| `in_room` | 30 s in `soft_disconnect` | seat auto-vacated; room returned to "waiting" |
| `in_game`, playing | 60 s in `soft_disconnect` | opponent notified via a `disconnect_notice` event in their event stream |
| `in_game`, playing | 120 s in `soft_disconnect` | hard disconnect → auto-concede; opponent wins by `disconnect_forfeit`; game_over |
| Ready countdown active | 0 s in `soft_disconnect` | countdown reset, player auto-unreadied |

Reconnect path:

1. Client reconnects with a new SSE connection.
2. Client presents the old token via `resume_session(token)` tool.
3. If token is still in registry → connection adopted, state replayed
   via an event stream (see below). Heartbeats resume.
4. If purged → client gets `token_expired`; must rejoin from scratch.

### Event stream for state replay

On game start, the server assigns each connection an SSE channel of
**viewer-filtered events** (essentially a live version of the
replay.jsonl entries for that viewer). On reconnect, the server
replays the stream from event index 0 so the client can rebuild its
local view without divergence.

This is cheap — one asyncio queue per connection — and it's the same
machinery used for the normal live play.

---

## Ready → auto-start countdown

State tuple per room: `(seats: {slot_a, slot_b}, ready: {a: bool, b: bool}, countdown_task: asyncio.Task | None)`.

```
            both seats filled && both ready=true
                              │
                              ▼
  idle ──────────────────► counting (10 s) ──────────► in_game
                              │   ▲
         any player unreadies │   │ set_ready(true)
         or disconnects       │   │ (both ready again)
                              ▼   │
                            idle ─┘
```

Implementation sketch:

```python
async def _countdown(room: Room):
    try:
        await asyncio.sleep(10.0)
        await room.start_game()
    except asyncio.CancelledError:
        pass
```

Countdown is cancelled and a fresh one started whenever either player
toggles `set_ready`, or on disconnect.

At game start the server:

1. Finalizes team assignment. `team_assignment="fixed"` uses
   `host_team`; `"random"` does a coin flip. Record the result in the
   replay `match_start` event.
2. Transitions both connections to `in_game`.
3. Creates the engine Session and the per-viewer event streams.
4. Broadcasts the first turn prompt to the active player.

---

## Fog of war

### `sight` stat

Add `sight` to each unit class in `state.py`'s stat table
(default proposal — tune in playtesting):

| Class   | sight |
|---------|-------|
| Knight  | 2     |
| Archer  | 4     |
| Cavalry | 3     |
| Mage    | 3     |

### Algorithm

Per team, compute visible tile set:

```python
def visible_tiles(state: GameState, team: Team) -> set[Pos]:
    visible = set()
    for u in state.units_of(team):
        visible.update(_sight_cone(state.board, u.pos, u.stats.sight))
    return visible
```

`_sight_cone` walks every tile within Chebyshev distance `sight`, but
uses a simple **line-of-sight check** that treats FOREST and MOUNTAIN
as **opaque** — they are visible themselves, but tiles *beyond* them
along the line from the viewer are not visible unless the viewer is
adjacent to them.

Line-of-sight impl: Bresenham's line from viewer to candidate tile;
if any intermediate tile is opaque, the candidate is hidden.

### Modes

Scenario config `fog_of_war` drives which viewer-filter is applied:

- `"none"` — filter is identity; both teams see everything (today's
  behavior).
- `"classic"` — tile terrain is revealed permanently once seen;
  enemy units only shown while currently visible.
- `"line_of_sight"` — both terrain and units visible only while
  currently in sight.

Per-team state needed for `classic`:
`ever_seen: dict[Team, set[Pos]]` — updated at the end of every
half-turn with the union of current `visible_tiles(team)`.

### Visible enemy granularity

When an enemy is in the current visible set, the viewer sees
**full stats** (position, class, HP, status). No partial information
layer. If not visible: the enemy is entirely absent from the viewer's
`get_state` / `get_unit` response.

---

## Viewer filter design

Centralized, pure, one audit surface. Every tool handler that returns
state information calls this once before sending.

```python
# shared/viewer_filter.py

@dataclass
class ViewerContext:
    team: Team
    fog_mode: FogMode           # "none" | "classic" | "line_of_sight"
    ever_seen: frozenset[Pos]   # accumulated for classic mode

def filter_state(state: GameState, ctx: ViewerContext) -> dict:
    """Return a dict-shaped view of state restricted to what `ctx.team`
    can legally see, under the given fog mode."""

def filter_unit(unit: Unit, state: GameState, ctx: ViewerContext) -> dict | None:
    """Return a filtered unit view, or None if the unit is invisible."""

def filter_threat_map(state: GameState, ctx: ViewerContext) -> dict:
    """Threats from visible enemies only."""
```

Every tool that touches state goes through the appropriate helper.
Tools that never leak info (e.g. `wait`, `end_turn`) skip the filter.
There's no "sometimes filter, sometimes not" escape hatch — filtering
is either applied or the tool output is structurally safe by construction.

**Testing**: the filter has its own test file. A `fog_of_war_invariants`
test runs N random game states through both teams' filters and asserts:

- No enemy unit appears in a view if its position isn't in that team's
  `visible_tiles`.
- Own-team units always appear.
- Terrain under `classic` is a superset of `line_of_sight`.
- `none` mode returns state unchanged.

---

## Scenario config additions

Add two fields to scenario YAML:

```yaml
rules:
  max_turns: 20
  fog_of_war: classic           # "none" | "classic" | "line_of_sight"
  first_player: blue
```

And `sight` in the unit class table (engine-level, not scenario-level).

Existing scenarios default to `fog_of_war: none` unless migrated,
keeping `clash-match` local play unchanged.

---

## Client TUI flow

Built on `rich` (already a dep) or `textual` (richer widgets, more
deps — decide during 1d). Five screens:

### Screen 1 — Login

- Prompt: display name.
- Prompt: kind (ai / human / hybrid).
- Optional: provider, model strings.
- Prompt: backend URL (defaults to `http://localhost:8080`).
- On submit: connect, `set_player_metadata`, transition to lobby.

### Screen 2 — Lobby

- Table of open rooms: id, host name, scenario, team mode, fog mode,
  seats filled (1/2), age.
- Keybindings: `r` refresh, `c` create room, `Enter` preview, `q` quit.
- Auto-refreshes every 5 s.

### Screen 3 — Room / preview

- Shows the scenario map (same renderer as `clash-play`) plus unit
  composition, fog mode, team assignment mode.
- If host: config editor before anyone joins.
- Seat list with ready flags.
- Keybindings: `Enter` toggle ready, `l` leave, `q` quit.
- When both ready: 10 s countdown overlay ticks down.

### Screen 4 — Game

- Same layout as today's `--render` TUI: header, board, units table,
  thoughts panel, last action, coach bar.
- Filtered by fog mode — tiles the viewer can't see render as `?`
  (or whatever glyph we pick).
- Keybindings: `c` toggle coach input, `q` concede (with confirm).

### Screen 5 — Post-match

- Winner banner.
- Match summary: turns, reason, lessons written (count only if client
  hides body).
- Keybindings: `d` download replay, `Enter` back to lobby, `q` quit.

---

## Replay download

`download_replay` returns the server's authoritative `replay.jsonl`
contents as a single text payload. Client writes it locally to
`~/.clash-of-robots/replays/<server-match-id>.jsonl` and can feed it
straight into `clash-play`.

Available for 60 s after game-over, then the token is purged. Client
TUI prompts once on the post-match screen.

---

## Scaling seams preserved for Phase 2

Choices made now that make the Phase 2 "ready for public" work
tractable:

1. **No global mutable state.** `RoomRegistry`, `TokenRegistry`, etc.
   are instance members of a single `App` object — easy to shard by
   `room_id` across processes later.
2. **Structured JSON logs to stderr** from day 1 (`logging` with a
   `JsonFormatter`). Log fields: `connection_id`, `room_id`, `slot`,
   `event`, `latency_ms`. Every tool call, every state transition,
   every timer firing.
3. **Auth is a single middleware.** Swappable for JWT later.
4. **All tool handlers are pure wrt state** — they read the engine
   Session and the auth context, return a response. No hidden
   globals → easy to unit-test and later easy to move to a shard.
5. **Tool-level rate limits are a middleware hook**, even if the
   implementation is `return True` in Phase 1.
6. **Replay is the source of truth.** Phase 2 can persist replays to
   S3 for audit without changing the game loop.

---

## Phase 1 done-definition checklist

### 1a — Protocol core
- [ ] `clash-serve` starts an MCP+SSE server on a configurable port.
- [ ] `clash-join` connects, does `set_player_metadata`, and calls a
      tool roundtrip.
- [ ] `TokenRegistry` issues + resolves + expires tokens.
- [ ] Two `clash-join` processes play a hard-coded 1v1 match to
      completion (random bots, no fog).

### 1b — Lobby
- [ ] Full lobby tool set implemented and state-gated.
- [ ] Two terminals: one hosts, one joins, both ready, game auto-starts.
- [ ] `get_room_state` returns correct countdown on `set_ready`.

### 1c — Fog of war
- [ ] `sight` stat defined per unit class.
- [ ] Three fog modes implemented (`none`, `classic`, `line_of_sight`).
- [ ] `ViewerFilter` tests cover invariants listed above.
- [ ] Scenario YAML `fog_of_war` field parsed and honored.

### 1d — TUI client
- [ ] All five screens render and transition cleanly.
- [ ] Game screen reuses the existing board renderer.
- [ ] Coach toggle sends messages via `send_to_agent` / the
      in-game coach channel.

### 1e — Disconnect + replay
- [ ] Heartbeat loop on the client; timer sweep on the server.
- [ ] All four disconnect transitions in the table above trigger
      correctly under fault injection tests.
- [ ] `resume_session` works within the soft window.
- [ ] `download_replay` delivers a valid JSONL the client can feed to
      `clash-play`.

### Non-goals tagged for Phase 2
- Spectator slot kind.
- Durable persistence.
- JWT / OAuth.
- Anti-abuse beyond timers.
- Cross-process scaling.
- Matchmaking.

---

## Recommended next step

Review this doc. When ready, pick one of:

1. **Start 1a** — I scaffold `shared/`, `server/app.py`,
   `server/auth.py`, and a minimal `client/transport.py`; we verify
   the connection + tool roundtrip end-to-end with zero game logic on
   the server side yet.
2. **Prototype the fog filter first (1c)** against today's local match
   harness, to de-risk the most subtle logic before we're committed to
   the network refactor.

(1) is the safer path — protocol-first tends to surface problems
earlier than logic-first for multiplayer systems.
