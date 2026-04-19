# Server threading & synchronisation model

This document defines the concurrency contract for the Silicon
Pantheon server. Read this before adding a new MCP tool, touching
`App` / `Session` / `Room` fields, or changing the heartbeat
sweep.

## Current execution model (as-deployed)

The server runs on a single asyncio event loop started by
`anyio.run(_serve)` in `server/main_http.py`:

- **uvicorn + FastMCP** dispatch HTTP requests as async tasks on
  that loop.
- **Sync tool handlers** (`@mcp.tool()` + `def handler(...)`) are
  called inline by FastMCP — verified in
  `mcp/server/fastmcp/utilities/func_metadata.py:92-95`. **Not**
  dispatched to a threadpool.
- **Async tool handlers** (`@mcp.tool()` + `async def handler(...)`)
  are awaited by FastMCP. If they contain no `await`, they run
  effectively atomically.
- **Background asyncio tasks**: `run_sweep_loop`,
  `_reattach_handlers`, `_run_countdown(room_id)`. Each awaits
  only `asyncio.sleep` — state mutations run synchronously
  between sleeps.

As of this writing, **races are architecturally impossible** —
every critical section runs on a single thread with no `await`
between acquire and release. The locks below are **defensive
/ future-proofing** for when we move to:

- **threadpool-dispatched sync handlers** (FastMCP could be
  upgraded), or
- **truly multi-threaded** execution (unlikely for Python asyncio
  servers, but possible), or
- **async with real awaits inside handlers** (e.g. awaiting a DB
  client). At that point the locks become load-bearing.

The test suite in `tests/test_concurrency.py` invokes server paths
from real OS threads precisely to catch regressions before we
make any of the above transitions.

## Lock hierarchy

Exactly four kinds of lock exist:

| # | Name | Type | Scope |
|---|---|---|---|
| 1 | `App._state_lock` | `threading.RLock` | All App dicts + all Room fields + all Connection fields (except `last_heartbeat_at`) |
| 2 | `Session.lock` | `threading.Lock` | `GameState` + all `Session` telemetry |
| 3 | `ReplayWriter._lock` | `threading.Lock` | Replay file handle |
| 4 | `ThoughtsLogWriter._lock` | `threading.Lock` | Thoughts log file handle |

`TokenRegistry` has an internal lock which is unchanged and
self-contained — treat it as a black box.

### Strict acquisition order

```
_state_lock   →   session.lock   →   writer locks
```

**Never acquire in reverse.** Violating this creates a cycle →
deadlock under real concurrency.

Specifically:

- ❌ While holding `session.lock`, **do not** acquire
  `_state_lock`. If you need cross-room state inside a tool call,
  snapshot it under `_state_lock` **before** you enter
  `session.lock`.
- ❌ While holding a writer lock, **do not** acquire anything
  else. Writer locks are leaves.
- ✅ `_state_lock` is `RLock` — same-thread re-entry is legal (a
  convenience method like `app.get_session(rid)` called from
  inside `with app.state_lock():` does not deadlock).

### Canonical patterns

#### Single-shot reads — use convenience methods

```python
# GOOD
conn = app.get_connection(cid)         # takes _state_lock briefly
session = app.get_session(room_id)     # takes _state_lock briefly
```

#### Multi-step atomic operations — use the context manager

```python
# GOOD
with app.state_lock():
    conn = app._connections.get(cid)
    if conn is None or conn.state != ConnectionState.IN_ROOM:
        return _error(...)
    info = app.conn_to_room.pop(cid, None)
    ...
```

#### Tool dispatch — three explicit phases

`game_tools._dispatch` is the reference implementation:

1. **Resolve** under `_state_lock`: look up connection, validate
   state, resolve `session` + `viewer`. Snapshot everything you
   need for the tool call.
2. **Execute** under `session.lock`: call into the engine. Hooks
   fired here (via `session.notify_action`) must NOT acquire any
   other lock.
3. **Post-process** with no lock held: call things like
   `_note_game_over_if_needed`, which have their own multi-phase
   locking protocols.

#### Post-game-over flow

`_note_game_over_if_needed` is the canonical "read session state
→ flip room state → do slow I/O" pattern. Three disjoint
critical sections:

1. `session.lock` to read `state.status` → snapshot whether
   game-over.
2. `_state_lock` to flip `room.status = FINISHED` and snapshot
   `room` + `slot_to_team`. Only the thread that actually flips
   `FINISHED` does the I/O (`won_race` flag).
3. No lock: `session.log_match_end()` (writer lock is internal)
   + `record_match()` (SQLite has its own connection).

## Rules (enforced by code review, not compiler)

1. **No `await` while any lock is held.** Holding a
   `threading.Lock`/`RLock` across an `await` blocks the asyncio
   event loop until the lock is released. If another coroutine
   needs the same lock, we deadlock.
2. **No user hooks / callbacks with `_state_lock` held.** Hooks
   fire under `session.lock` only. Hook authors MUST NOT
   acquire `_state_lock` from inside a hook — that violates
   the acquisition order.
3. **Critical sections are short.** Validation, YAML load, and
   other I/O happen OUTSIDE locks. See `create_room` for the
   "validate outside, register inside" split.
4. **Slow I/O is a leaf operation.** Writer locks are leaves;
   SQLite gets its own connection; file system writes happen
   outside app-level locks.
5. **RoomRegistry is lockless.** Callers MUST hold
   `_state_lock`. Direct reads like `app.rooms.get(rid)` need
   the lock too. The `App` convenience methods (`get_room`,
   `list_rooms`) do this for you.
6. **`Connection.last_heartbeat_at` is a lock-free scalar.**
   Deliberate carve-out: single-float store is GIL-atomic and
   the heartbeat tool fires every ~10s per connection; paying
   lock contention on every ping is wasteful. All other
   Connection fields require `_state_lock`.

## Why `threading.Lock` rather than `asyncio.Lock`?

Three reasons:

1. **Uniformity across sync and async callers.** Many handlers
   are sync `def` and cannot `async with` an `asyncio.Lock`.
   `threading.Lock` works in both.
2. **Future compatibility.** If we ever move sync tool handlers
   to a threadpool (Case 2), `asyncio.Lock` wouldn't work
   across thread boundaries. `threading.Lock` does.
3. **Safe to hold briefly in async code.** The discipline rule
   "no await while locked" ensures we never block the event
   loop. In current code, held durations are microseconds.

If we ever need to hold a lock across an `await` (e.g. awaiting
a slow DB call inside a critical section), we'll switch that
specific lock to `asyncio.Lock` — but the default everywhere else
stays `threading.Lock`.

## Deadlock-free proof

Under the rules above, deadlock requires a cycle in the
"holds-and-waits" graph. The only edges we permit are:

- `_state_lock → session.lock`
- `_state_lock → writer lock`
- `session.lock → writer lock`

No permitted edge runs from `session.lock` back to `_state_lock`
or from a writer lock back to either. The graph is a DAG → no
cycle → no deadlock.

`_state_lock` is `RLock`, so same-thread re-entry isn't a
deadlock either.

## Tested in `tests/test_concurrency.py`

- Distinct + same-cid `ensure_connection` races
- 24-client lobby churn with random create/join/leave/ready
- Sweep ↔ reader contention
- `force_end_turn` non-blocking acquire under held-lock
  contention (sweep ticks must stay <200ms)
- `_auto_concede` vs `_vacate_room` on sibling seats
- `ReplayWriter` concurrent JSON-line atomicity
- `TokenRegistry` issue/resolve concurrency

All use a watchdog timer: deadlock manifests as "workers didn't
finish in the timeout window" → loud test failure.
