# Phase 1 — execution plan and task breakdown

Companion to [PHASE_1_DESIGN.md](PHASE_1_DESIGN.md). Concrete, ordered
list of commits. Each line is one commit; tests must be green after
every one.

Branch: `phase1`. Worktree: `/home/pringles/dev/agent-game-phase1/`.

## Execution rules

1. One logical change per commit.
2. `uv run python -m pytest -q` must pass after every commit.
3. Don't edit files outside the current sub-phase's scope.
4. Check this doc off as we go (in-file edits).
5. When stuck > one commit, stop and escalate rather than pile on fixes.

---

## Sub-phase 1a — protocol core

Goal: two `clash-join` processes can connect to `clash-serve` and run
one hard-coded match end-to-end (random bots, no lobby, no fog).

- [x] **1a.1** Scaffold `shared/` package with `__init__.py`.
- [x] **1a.2** Move `match/replay_schema.py` → `shared/replay_schema.py`;
      re-export from old path for compat.
- [x] **1a.3** Add `shared/player_metadata.py` (PlayerMetadata dataclass).
- [x] **1a.4** Add `shared/protocol.py` — enums for connection state,
      error codes, tool namespaces.
- [x] **1a.5** Add `server/auth.py` — TokenRegistry with tests.
- [x] **1a.6** Add `server/rooms.py` — minimal Room + RoomRegistry
      (single-game hardcoded for now).
- [x] **1a.7** Add `server/app.py` — FastMCP HTTP+SSE server scaffold
      exposing `set_player_metadata`, `heartbeat`, `whoami`.
- [x] **1a.8** Add `client/transport.py` — MCP+SSE client wrapper.
- [x] **1a.9** Add `clash-serve` CLI entry wired to `server/app.py`.
- [x] **1a.10** Add `clash-join` CLI entry with minimal "connect +
      metadata + whoami" smoke flow.
- [x] **1a.11** Integration test: start server in-process, run two
      clients, verify metadata round-trip.
- [x] **1a.12** Wire the existing 13 game tools into the server under
      a hardcoded-single-game session.
- [x] **1a.13** End-to-end test: two random clients play a full 1v1
      match via the server.

## Sub-phase 1b — lobby

Goal: host + join via lobby, ready up, auto-start, game runs.

- [x] **1b.1** Extend `Room` with config, seats, ready flags.
- [x] **1b.2** Implement `list_rooms`.
- [x] **1b.3** Implement `create_room` (issues token).
- [x] **1b.4** Implement `preview_room` (scenario config + seats).
- [x] **1b.5** Implement `join_room` (issues token for second slot).
- [x] **1b.6** Implement `leave_room`.
- [x] **1b.7** Implement `set_ready` + auto-start countdown (10s).
- [x] **1b.8** Implement `get_room_state`.
- [x] **1b.9** Game runner transitions room → `in_game` at countdown.
- [x] **1b.10** Finalize team assignment (fixed / random) at game start.
- [x] **1b.11** Integration test: full host/join/ready/auto-start flow.

## Sub-phase 1c — fog of war

- [x] **1c.1** Add `sight` stat per unit class with engine defaults.
- [x] **1c.2** Extend scenario YAML loader to parse `fog_of_war`.
- [x] **1c.3** Add `shared/fog.py` — `visible_tiles(state, team)` with
      LOS + opaque terrain.
- [x] **1c.4** Add `shared/viewer_filter.py` — `filter_state`,
      `filter_unit`, `filter_threat_map`.
- [x] **1c.5** Wire the filter into every game tool that returns state.
- [x] **1c.6** Classic-mode memory (`ever_seen`) tracked on Session.
- [x] **1c.7** Invariant tests (no-leak, subset relations, none-mode
      identity).

## Sub-phase 1d — TUI client

- [ ] **1d.1** Login screen.
- [ ] **1d.2** Lobby screen with refresh.
- [ ] **1d.3** Room/preview screen with ready toggle and countdown.
- [ ] **1d.4** In-game screen (reuses board renderer, fog-aware).
- [ ] **1d.5** Post-match screen with replay download prompt.
- [ ] **1d.6** Screen transitions + error-state handling.

## Sub-phase 1e — disconnect + replay download

- [x] **1e.1** Client heartbeat task on transport.
- [x] **1e.2** Server heartbeat-sweep task with the timer table.
- [x] **1e.3** Disconnect transitions implemented per table.
- [ ] **1e.4** `resume_session` + event-stream replay.
- [x] **1e.5** `download_replay` tool.
- [x] **1e.6** Fault-injection tests for each disconnect path.

## Cross-cutting

- [x] **X.1** `pyproject.toml`: add `clash-serve` and `clash-join` CLI
      entries; add any new deps (httpx-sse or mcp's http transport).
- [ ] **X.2** Structured JSON logger module used by all new server code.
- [ ] **X.3** README pointer to `docs/USAGE.md` with the new commands
      section.

## Definition of done

- All tasks above checked off.
- `uv run python -m pytest -q` passes.
- Hand-verified: two terminals, `clash-serve` running, two
  `clash-join`s complete a fog-of-war match end-to-end and each can
  download the replay for local `clash-play`.
- `docs/USAGE.md` updated with the new commands and lobby flow.
