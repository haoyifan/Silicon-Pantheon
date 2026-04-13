# Phase 1 — status snapshot

Work-in-progress on the `phase1` branch at
`/home/pringles/dev/agent-game-phase1/`. Everything described here is
committed and all **122 tests pass** on the tip of the branch.

## What's done (29 commits on top of `master`)

### Sub-phase 1a — protocol core ✅
Full MCP+SSE transport proven end-to-end.

- `shared/` package scaffolded; replay schema moved into it with a
  back-compat re-export at the old path.
- `shared/player_metadata.py`, `shared/protocol.py` with
  `ConnectionState` / `ErrorCode` enums.
- `server/auth.py` — `TokenRegistry` with TTL-based expiry, thread-safe
  issue/revoke/resolve.
- `server/rooms.py` — `Room`, `RoomRegistry`, `Slot` + `RoomStatus`.
- `server/app.py` — `App` holding all per-server state;
  `build_mcp_server()` returns a FastMCP instance wired with the always-
  available tools (`set_player_metadata`, `heartbeat`, `whoami`).
- `client/transport.py` — `ServerClient` async context manager over
  real MCP streamable-HTTP.
- `clash-serve` and `clash-join` CLI entry points in `pyproject.toml`.
- Integration test spinning up uvicorn in a background thread on an
  ephemeral port and driving real MCP+SSE clients against it.
- The 13 existing game tools exposed remotely via typed FastMCP
  wrappers; two clients play a full round over the network.

### Sub-phase 1b — lobby ✅

- `Room` extended with `RoomConfig` (scenario, max_turns,
  team_assignment fixed|random, host_team, fog_of_war, turn_time_limit).
- Full lobby tool set: `list_rooms`, `preview_room`, `create_room`,
  `join_room`, `leave_room`, `set_ready`, `get_room_state`.
- 10s auto-start countdown as cancelable asyncio task; any unready /
  leave / disconnect cancels it.
- Game runner (`start_game_for_room`) finalizes team assignment
  (deterministic for fixed rooms, coin-flipped for random), builds
  the engine Session, and promotes connections to `IN_GAME`.
- Integration tests: full host → join → ready → auto-start flow,
  unready-cancels-countdown, leave-returns-to-lobby.

### Sub-phase 1c — fog of war ✅

- `sight` stat added to `UnitStats`; class defaults: Knight 2,
  Archer 4, Cavalry 3, Mage 3.
- `shared/fog.py` — `visible_tiles(state, team)` using Chebyshev sight
  cones + Bresenham line-of-sight with forest/mountain as opaque
  (visible themselves, block view past them unless adjacent).
- `shared/viewer_filter.py` — `ViewerContext`, `filter_state`,
  `filter_unit`, `filter_threat_map`, `update_ever_seen`.
- Three fog modes: `"none"` (identity), `"classic"` (terrain
  remembered once seen, units only while visible), `"line_of_sight"`.
- Game tools dispatch pipeline wraps state-revealing tool outputs in
  the filter; classic-mode `ever_seen` grows on every `end_turn`.
- 9 invariant tests in `tests/test_fog.py` + integration test proving
  fog works over the wire.

### Sub-phase 1e — disconnect + replay download (mostly done)

- Server `heartbeat.py` with `run_sweep_once(app, now)` — single
  deterministic entrypoint for the disconnect state machine. Four
  transitions: lobby eviction, room eviction, in-game soft notice,
  in-game hard forfeit. Module-level timer constants are
  monkey-patchable for tests.
- Client `ServerClient.start_heartbeat()` / `stop_heartbeat()` runs a
  10s ping loop as a background asyncio task.
- `clash-serve` startup co-runs the sweeper alongside the HTTP app
  via an anyio task group.
- `download_replay` tool returns the server's authoritative
  replay.jsonl body while `IN_GAME` (valid until game over, purged
  shortly after).
- `concede` tool for manual resignation.
- 5 unit tests drive the four disconnect transitions deterministically.

## What's not yet done

### Sub-phase 1d — TUI client (not started)

Five screens (login, lobby, room preview, in-game, post-match) plus
transition / error handling. Substantial and visually-iterative; best
approached once the backend stabilizes. All 122 tests pass without it;
the transport, lobby, fog, and disconnect machinery can all be
exercised from plain Python or the existing `clash-join` smoke flow.

### Residual 1e items

- **1e.4 `resume_session` + event-stream replay.** Clients that
  reconnect mid-match currently have to rejoin from scratch (their
  token from the previous connection is gone with the dropped
  connection). Doable but requires adding a per-connection event
  queue abstraction so missed state deltas can be replayed.

### Cross-cutting

- **X.2 Structured JSON logging.** Left as plain `logging` for now;
  upgrade when more server code accretes.
- **X.3 `docs/USAGE.md` update.** Still only documents the local
  `clash-match` / `clash-play` flow. Add a "networked play" section
  covering `clash-serve`, `clash-join`, the lobby flow, and the new
  fog-of-war config.

## How to manually verify the backend today

Terminal 1:
```bash
cd /home/pringles/dev/agent-game-phase1
uv run clash-serve --host 127.0.0.1 --port 8080
```

Terminal 2 (smoke flow only — exercises connect, metadata, heartbeat,
whoami):
```bash
cd /home/pringles/dev/agent-game-phase1
uv run clash-join --name alice --kind ai --provider anthropic \
  --model claude-haiku-4-5
```

Everything else (hosting, joining, playing, downloading replay) is
exercised by the integration tests; a TUI client to drive it from a
human terminal is the 1d work that remains.

## Commit list

Use `git log master..phase1 --oneline` in the worktree to see all 29
commits. Highlights:

- `c345803` Scaffold shared/ package
- `d9418ca` Phase 1a end-to-end integration test
- `40d080d` Check off 1a tasks in plan
- `802fd41` 1b.1: extend Room with RoomConfig / ready flags / status
- `(lobby suite commit)` 1b: full lobby tool set + auto-start countdown
- `81ad4f0` 1c.1: sight stat per class
- `(fog suite commit)` 1c.2-1c.4: shared/fog + viewer_filter + invariant tests
- `e63bfd7` 1c.5-1c.6: wire filter into game tools + classic memory
- `(heartbeat commit)` 1e.1-1e.5: heartbeat sweeper + download_replay
- `1f31d49` 1e: client heartbeat + wire server sweeper into startup

## Recommended next step

Review this branch on a free evening; merge into `master` once you're
happy with the backend shape. The 1d TUI work should happen on a
fresh branch off the then-stabilized `master`.
