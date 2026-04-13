# Networked-backend architecture — design review

Review of the proposal to split the project into a hosted backend +
pluggable clients, with lobby/room flow, generic-agent support, and
per-client fog of war.

Status: **proposal, not adopted.** Use this to drive further discussion
before any implementation begins.

---

## What already lines up

The project has been building toward this without realizing it:

- **MCP was always the agent contract.** `server/main.py` is a FastMCP
  stdio wrapper. Switching stdio → HTTP+SSE (which MCP already
  supports) keeps the agent-facing interface identical, which means
  Claude, OpenAI function-calling, Bedrock, any tool-using agent can
  talk to it with minimal glue.
- **The engine is pure.** `engine/` has no I/O. `Session` cleanly
  bundles per-match state, coach queues, replay, thoughts. Swapping
  from "one Session per process" to "N Sessions per server" is an
  orchestrator change, not an engine change.
- **`match_start` metadata + the typed replay schema** already give
  enough to reconstruct any match — useful when a client joins late
  (spectating) or reconnects.
- **Replay writer is append-only JSONL**, which is exactly what you
  want for server-authoritative game logs that clients can subscribe
  to.

---

## The decisions that will shape everything

Lock these down before coding. A few aren't obvious:

### 1. Transport: MCP-over-HTTP+SSE vs. WebSocket vs. custom

**Strong vote: MCP+SSE.** It preserves the agent contract, the spec
supports auth headers, and every tool-using LLM SDK already has an
adapter. Going custom means re-implementing everything agents already
know how to use.

### 2. Concurrency model

Single-process asyncio with one task per game scales to ~dozens of
concurrent matches on a small VPS — more than this project will ever
need. **Don't reach for Redis / Postgres / Kubernetes yet.**

### 3. Fog of war is a *server-side filter*, not a client responsibility

Every tool response must be stripped of info the viewer isn't entitled
to, at the server. One leak and the whole model breaks.

Centralize this in a `ViewerFilter(state, viewer) -> filtered_dict`
layer that every tool response passes through — a single audit surface.

### 4. Authorization

When a client joins a game as "blue", the server issues a per-match
token. Every MCP tool call carries it; the server looks it up to know
"this is the blue player in game X." Simple JWT or opaque tokens both
work. This is the minimum to prevent one client from calling `end_turn`
on the other's behalf.

### 5. Lobby ≠ game

Browsing rooms, creating rooms, previewing scenarios — these aren't
game tool calls; forcing them into MCP is awkward. Expose a small REST
endpoint or a separate MCP namespace for lobby operations, and switch
the connection to the game's MCP endpoint only after join+ready.

### 6. What does "validate the login is an LLM" actually mean?

**Pushback here.** You can't reliably distinguish a human-operated
client from an AI client over the network. Every heuristic (rate limit,
response-latency, prompt acknowledgement, proof-of-inference challenge)
either rejects legitimate AI clients with slow backends or lets humans
through with simple scripting. Two realistic alternatives:

- **Self-declaration + rate limits**: the client sends
  `{name, provider, model}` on connect and agrees to a turn-time cap.
  Humans can technically play, but the cap (say, 5 min/turn) is
  designed for LLM latency — humans will just find it awkward.
- **Don't try.** Humans vs. AI can coexist in the same lobby; if
  anything, it's a feature (human coaching *is* what this repo is
  about).

Pick one. Don't build elaborate detection.

---

## Risks worth flagging

- **Graceful disconnect / reconnect.** What happens when blue's Python
  process crashes mid-turn? Options: auto-resign after N seconds,
  pause indefinitely, allow rejoin with the same token. Pick early —
  it changes how sessions are modeled.
- **Replay divergence.** Once there are two independent clients, each
  one deep-copies state locally for rendering. A subtle bug where the
  server and client apply actions differently produces a replay that
  doesn't reproduce. Keep the server strictly authoritative; clients
  only display, never compute state.
- **Client-side lesson injection.** If lessons are local to each
  client, an agent's priors depend on what lessons that specific client
  has on disk. That's fine, but surface it somewhere — the server log
  should record "blue played with 5 injected lessons" even if it
  doesn't know their content.

---

## Phasing

Don't do all of this at once. Three phases, each independently useful:

### Phase 1 — one game, over the network

Expose existing MCP tools over HTTP+SSE. Add per-match auth tokens.
Split `run_match` into:

- **`clash-serve`** (backend: holds sessions, runs the engine, serves
  MCP)
- **`clash-join`** (client: authenticates, connects, plugs the local
  agent into the remote MCP)

Still one game at a time. Two terminals on a laptop, or one local
client + one on a VPS. **This phase forces all the hard refactors —
transport, auth, state ownership — and proves the contract.**

### Phase 2 — multi-tenancy + lobby

Server hosts N concurrent sessions. Add lobby endpoints:

- `list_rooms`
- `create_room(scenario, config)`
- `preview_room(id)`
- `join_room(id, team)`
- `set_ready()`

Client gets a TUI flow: login → lobby → preview → ready → game. Room
preview renders scenario YAML — no agents needed; scenario files have
the map already.

### Phase 3 — fog of war + per-client rendering

Enable fog in scenarios that want it. Server filters every tool
response by viewer. Each client's TUI only shows what it sees. Lessons
stay local. Post-match reflection stays local. Server's `replay.jsonl`
is the global truth.

---

## Open questions for you

Two tension points worth deciding before Phase 1:

1. **Is the TUI lobby a hard requirement, or would a CLI
   (`clash-lobby list`, `clash-lobby host --map X`,
   `clash-lobby join ROOM_ID`) work for Phase 2?** CLI is ~3 days, TUI
   is ~2 weeks. If this is a portfolio / learning project, TUI is great
   practice; if the goal is to *use* it, CLI first.

2. **What does "hosted" really mean?** A VPS serving a handful of
   friends' games is very different from a public endpoint serving
   strangers. Public implies abuse mitigation, compute budgeting,
   per-user quotas, etc. — a whole other category of work. Default
   assumption: **private / semi-private.**

---

## Recommended next step

Produce a detailed Phase 1 plan — file tree, protocol messages, which
code moves where, what stays — so we can agree on shape before
refactoring. That's where the biggest architectural lock-in is and
where review pays the most.
