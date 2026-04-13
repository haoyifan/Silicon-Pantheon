# Networked-backend architecture — review round 2

Follow-up to [NETWORKED_ARCHITECTURE_REVIEW.md](NETWORKED_ARCHITECTURE_REVIEW.md).
The first round's open questions have been answered:

- Transport: **MCP + SSE**
- Concurrency: **simple now, scalable later**
- Fog of war: **yes, with per-unit visibility differences**
- Teams: **pre-selected or randomized at game-start (host's choice)**
- Lobby ≠ game: **agreed**
- AI/human validation: **don't enforce; design a metadata hook for later**
- Disconnect: **heartbeat + auto-finish/auto-kick**
- TUI: **full TUI experience**
- Deployment: **private first, architect for eventual public**

Status: **proposal, not adopted.** New open questions flagged below
need decisions before the Phase 1 design doc is written.

---

## What still needs a call

Before locking Phase 1, five decisions are ambiguous enough that coding
them different ways would give different games.

### 1. Visibility mechanics (fog-of-war spec)

"Some units have better vis" is the right instinct, but four or five
decisions live underneath it that all affect gameplay:

1. **Sight as a stat.** Add `sight` to unit stats
   (e.g. Knight 2, Archer 4, Cavalry 3, Mage 3). Visibility = tiles
   within Chebyshev distance `sight` from a unit.
2. **Team vision = union.** If any ally sees a tile, the whole team
   sees it. (Alternative: per-unit vision only, which is brutal.)
3. **Terrain effects.** Forest blocks vision past it? Or just imposes
   a sight penalty?
   Default recommendation: forest / mountain tiles are visible but
   block sight *past* them unless you're adjacent. Simple
   line-of-sight rule.
4. **Memory model.** Two choices with very different feel:
   - **Classic fog** — once revealed, the tile *terrain* stays
     visible; *units* on it only show while currently in sight.
     (Easier for agents.)
   - **Line of sight** — if you lose sight, the tile goes fully dark.
     (Harder, more tactical, more frustrating.)
   - Recommendation: **classic fog for MVP.**
5. **Enemy unit info granularity.** When an enemy appears in sight,
   does the agent get full stats (HP, type, status) or just
   "enemy here"?
   Simplest: full stats while in sight (no "partial information"
   layer).

**Decision needed on each of 1–5.**

### 2. Team slot vs. team binding

Because the host can pre-assign teams *or* randomize at match start,
tokens need to bind to **slots** (`game_id, slot_1|slot_2`) rather
than teams. At match start the server maps `slot → team`
(deterministic if pre-assigned, coin-flip if random), stores the
mapping, and tool calls look up `slot → team` on each request.

One slot = one session, so if the host wants to spectate only, that's
a third slot kind (`spectator`). Worth deciding now whether
**spectators are in scope for Phase 1 or pushed to Phase 2/3.**

### 3. Disconnect / heartbeat spec

The original proposal is sound. Concrete rules to write:

- **Client → server heartbeat** every 10s (lightweight MCP ping or
  dedicated tool).
- **Grace window:** 30s of no heartbeat triggers `disconnected` state.
- **In-lobby** (pre-game): `disconnected` → slot auto-vacated after
  30s; room becomes joinable again.
- **In-game, game-not-started-yet** (both players readied but the
  game hasn't begun): disconnected player unreadies after 30s.
- **In-game, playing:**
  - Disconnected-for-60s (soft): opponent notified.
  - Disconnected-for-120s (hard): disconnected player resigns;
    opponent wins by `disconnect_forfeit`.
- **Reconnect before hard timeout:** same token; server replays any
  missed state deltas; game continues.

Key design point: **the server owns all these timers.** Clients just
send heartbeats.

### 4. Player metadata shape

Since the hook is wanted now (even without enforcement), define the
self-declaration shape today:

```json
{
  "display_name": "pringles-claude",
  "kind": "ai" | "human" | "hybrid",
  "provider": "anthropic",
  "model": "claude-opus-4-6",
  "version": "1"
}
```

Sent at connect. Stored with the match. Shown in room preview and in
the replay's `match_start` event. No validation — if someone lies,
they lie. Later layers (reputation, attestation) attach to this
without a schema change.

### 5. Spectators in Phase 1?

Called out above but worth isolating: **yes or no for Phase 1?** Saying
yes adds a slot kind, read-only tool subset, and broadcast flow. Saying
no lets Phase 1 stay two-player-only.

---

## What "prepare for scale later" means concretely

Three architectural choices pay off for public scale *and* cost
nothing up front:

1. **One match = one asyncio task, one in-memory object graph, no
   global mutable state.** This lets matches later shard across
   processes with a trivial frontdoor (by `game_id`). Leaking global
   state early means paying to untangle it later.
2. **Structured JSON logs from day 1** (`logging` + JSON formatter to
   stderr). Adding observability later is painful; adding it now is
   free.
3. **Auth is a middleware seam.** Phase 1 uses opaque per-match
   tokens from an in-memory dict. The *interface* is: "given a token,
   return `{game_id, slot}` or 401." Later, swap the implementation
   for JWT / OAuth / attestation without changing handler code. Don't
   let auth checks sprawl through tool handlers.

Things to **avoid** building now that sound tempting:

- A database. Files + in-memory are fine until real users exist.
- Horizontal scaling. A single Python process handles plenty.
- A web frontend. The TUI is the differentiator.
- Abuse prevention beyond turn timers. Handle abuse when abuse exists.

---

## Proposed next step

Don't write code yet. Next artifact: **`docs/PHASE_1_DESIGN.md`**,
covering:

1. File tree after the split (`server/`, `client/`, `shared/`, what
   moves where).
2. Full protocol: MCP tool list for lobby (`list_rooms`,
   `create_room`, `join_room`, `set_ready`, `heartbeat`,
   `leave_room`) and for game (the existing 13 tools + filtering).
3. Viewer-filter design — concrete code sketch of the centralized
   filter.
4. Token lifecycle — when issued, when invalidated, what's in them.
5. Heartbeat + disconnect state machine (ASCII diagram).
6. Fog-of-war algorithm (once the 5 open questions above are
   answered).
7. Phase 1 deliverable checklist: what "done" means, what's
   explicitly punted to Phase 2 / 3.

**To unblock that doc, decisions are needed on:**

- Visibility specifics 1–5 (above)
- Spectators in Phase 1: yes / no
