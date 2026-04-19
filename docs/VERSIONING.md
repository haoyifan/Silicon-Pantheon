# Wire-Protocol Versioning & Compatibility

Silicon Pantheon's server and client can upgrade independently — the
server may get redeployed without coordinating every player's client
version, and vice versa. This document is the authoritative reference
for how that's kept safe:

- what the protocol version is,
- what counts as a breaking change,
- what to do when you make one,
- how the client surfaces upgrade prompts to the user,
- the rollout checklist for every new server deploy.

Keep this doc in sync with `src/silicon_pantheon/shared/protocol.py`
— if the code changes, amend the doc in the same PR.

---

## The three constants

All three live in `silicon_pantheon/shared/protocol.py` and are
imported by both server and client code:

| Constant | Meaning |
|---|---|
| `PROTOCOL_VERSION` | The version this codebase speaks, as client AND server. |
| `MINIMUM_CLIENT_PROTOCOL_VERSION` | The oldest client-side version this codebase (when running as a server) will still serve. Clients below this get `CLIENT_TOO_OLD`. |
| `MINIMUM_SERVER_PROTOCOL_VERSION` | The oldest server-side version this codebase (when running as a client) will still talk to. Servers below this get `SERVER_TOO_OLD`. |

Plus one string:

| Constant | Meaning |
|---|---|
| `UPGRADE_COMMAND_HINT` | Human-readable upgrade instruction included in server error responses so the client can show it verbatim. |

All three integers start at `1` today. They move independently of the
package `version` in `pyproject.toml` — that tracks the Python
release, this tracks the wire protocol. Many package releases do not
bump `PROTOCOL_VERSION`.

---

## What counts as a breaking change

A change is **breaking** (must bump `PROTOCOL_VERSION`) if an older
peer talking to a newer peer would fail or misbehave:

- A tool was **renamed** or **removed**.
- An **existing field** changed shape (int → string, list → dict).
- A field that was optional became **required**.
- Semantics of a field **changed** (same name, different meaning).
- A **response shape** was restructured (key moved / nested).
- A **state transition** (CONNECTION → IN_LOBBY etc.) became dependent on a new tool call that older clients don't make.

A change is **not breaking** (do NOT bump) if it's purely additive
and older peers safely ignore it:

- A **new MCP tool** was added.
- A **new optional field** was added to a response (old clients just don't read it).
- A **new optional argument** with a safe default was added to a tool.
- A **new scenario** was added.
- A **new error code** that only newer clients know how to handle specifically (old clients still get a generic error string).

> **Rule of thumb**: "old client + new server = works, just without
> the new feature." If that invariant holds, the change is
> non-breaking. If not, it's breaking.

Discipline matters here. If "breaking" is defined too loosely, every
commit bumps the version, the upgrade prompt fires weekly, and users
learn to ignore it. Err toward non-breaking with graceful fallbacks
on the client side.

---

## How breaking changes are rolled out

Four phases. Running through them with a concrete example below —
*"add a required `fog_mode` field to the `create_room` response
that older clients can't parse"* — but the procedure is identical
for any breaking change.

### Phase 1 — land the code change (one PR, PROTOCOL_VERSION++ only)

Edit `src/silicon_pantheon/shared/protocol.py`:

```python
PROTOCOL_VERSION = 2                     # was 1
MINIMUM_CLIENT_PROTOCOL_VERSION = 1      # leave at 1 — don't lock out yet
MINIMUM_SERVER_PROTOCOL_VERSION = 1      # leave at 1
```

In the same PR:

- Implement the new wire shape (server emits the new field, newest
  client reads it).
- **Keep a compatibility shim** on the server so v1 clients still
  work. Each incoming tool call carries its connection; the server
  stashes the client's version on `Connection.client_protocol_version`
  at `set_player_metadata` time, so any handler can branch:

  ```python
  if conn.client_protocol_version >= 2:
      response["fog_mode"] = ...       # new shape
  # else: v1 shape (no fog_mode field)
  ```

  This is the "both work" window that makes phase 2 safe.

- Update `CHANGELOG` / PR description to spell out:
  *"bumps PROTOCOL_VERSION 1 → 2, reason: …, v1 compat kept behind
  shim until MIN is raised"*.

- Add/update tests in `tests/test_protocol_version.py` asserting
  both old and new clients still succeed against this server.

- Cross-reference this doc in the PR description.

Land. **No production impact yet** — code is in main, but nothing
has been redeployed.

### Phase 2 — deploy the server

```bash
ssh silicon@5.78.204.141
cd ~/Silicon-Patheon && git pull
sudo systemctl restart silicon-serve.service
sudo -n journalctl -u silicon-serve.service --since '-1m' \
  | grep -vE 'POST /mcp|Processing request' | tail -20
```

Walk through the **server-deploy checklist** below before
restarting. After restart: v1 and v2 clients both work. Nobody is
gated.

### Phase 3 — let clients catch up

- Post in `#announcements` in Discord:
  *"Silicon Pantheon client v2 is out — `git pull && uv sync`.
  Old clients will be locked out in 7 days."*
- Wait enough time for active players to update. Judge by
  connection telemetry + who's active in Discord.
- **Don't ship another breaking change during this window.** One
  at a time.

### Phase 4 — raise the minimum and redeploy

After the wait, edit `shared/protocol.py` again:

```python
PROTOCOL_VERSION = 2
MINIMUM_CLIENT_PROTOCOL_VERSION = 2      # was 1 — now v1 clients get CLIENT_TOO_OLD
```

In the same PR:

- **Remove the compatibility shim** from phase 1. Nothing below
  v2 will hit that code path anymore.
- Update `tests/test_protocol_version.py`:
  `test_client_omitting_version_rejected_once_minimum_exceeds_v1`
  already asserts the locked-out behavior; make sure the analog
  test for a v1-tagged client also flips from "accepted" to
  "CLIENT_TOO_OLD".

Land, then redeploy the server. After restart, v1 clients hit the
upgrade-required screen; v2+ clients keep playing.

### Red flags that should stop you

- **You can't cleanly shim old behavior in phase 1.** If the old
  and new shapes genuinely can't coexist (e.g. renaming a required
  field that both reads and writes on the same turn), you actually
  have two stacked breaking changes — split them, or bite the
  bullet and skip phase 3 (user-hostile, only for emergencies or
  data-corruption bug fixes).

- **You want to bump both constants in one deploy.** Allowed only
  for security fixes or data-corruption bugs. Otherwise always
  split — the "old client + new server = works" invariant is the
  whole reason the scaffolding exists.

- **You're changing `scenario_description` shape.** That's served
  by `get_scenario_bundle` and cached client-side by content-hash.
  A new shape produces a new hash → clients refetch → usually fine
  without a protocol bump. But if the new shape has a field older
  clients *fail to render on* (not just ignore), it IS a bump.

### Verifying each phase

**After phase 2** — v1 clients should still work:

```bash
.venv/bin/python3 -m pytest tests/test_protocol_version.py -v

# Simulate an old client locally against a fresh App():
.venv/bin/python3 - <<'PY'
from silicon_pantheon.server.app import App, build_mcp_server
import asyncio, json
mcp = build_mcp_server(App())
blocks = asyncio.run(mcp.call_tool('set_player_metadata',
    {'connection_id': 'c1', 'display_name': 'x', 'kind': 'ai',
     'client_protocol_version': 1}))
print(json.loads(blocks[0].text))
# Expect ok=true, with server_protocol_version=2 and
# minimum_client_protocol_version=1 in the response.
PY
```

**After phase 4** — v1 clients should be rejected with the
structured `CLIENT_TOO_OLD` error including the upgrade command:

```bash
.venv/bin/python3 - <<'PY'
# same call as above, but expect:
# ok=false, error.code=client_too_old, error.data.upgrade_command set
PY
```

---

## How the client handles a version gap

The client calls `set_player_metadata` as the first thing it does
after connecting. Its handshake logic (in
`src/silicon_pantheon/client/tui/screens/login.py:_connect_and_declare`):

1. Send `client_protocol_version = PROTOCOL_VERSION` (ours).
2. Check the response:
   - If `ok: false` with `error.code == "client_too_old"`: raise
     `VersionMismatchError(kind="client_too_old")`. The login screen
     catches this and routes to `UpgradeRequiredScreen`, which shows
     the server's `upgrade_command` string.
   - If `ok: true` with `server_protocol_version <
     MINIMUM_SERVER_PROTOCOL_VERSION`: raise
     `VersionMismatchError(kind="server_too_old")`. Same upgrade
     screen, different message — user should contact the server
     operator, not upgrade their own client.
   - Else: proceed to lobby.

Features (new tools added after the initial v1) are always checked
for existence before use — the client treats "tool not found" as a
soft degrade rather than a fatal error, so a new client can run
against an older server as long as it gracefully skips missing
tools.

---

## Error codes

| Code | Meaning |
|---|---|
| `client_too_old` | Client's protocol version < server's minimum. Returned from `set_player_metadata`. `data` includes `client_protocol_version`, `server_protocol_version`, `minimum_client_protocol_version`, `upgrade_command`. |
| `server_too_old` | **Client-raised only.** Detected when the server's reported `server_protocol_version` is below the client's `MINIMUM_SERVER_PROTOCOL_VERSION`. Triggers the upgrade-required screen with a "contact the operator" message. |
| `version_mismatch` | Legacy generic code, still defined for backward compat but no longer produced by the server. Kept so old clients parsing this code keep working. |

---

## Server-deploy checklist

Before running `sudo systemctl restart silicon-serve.service` on
production:

- [ ] Did `PROTOCOL_VERSION` change in this deploy?
  - If YES: did you also keep the server accepting the PREVIOUS
    protocol version (no `MINIMUM_CLIENT_PROTOCOL_VERSION` bump) so
    existing players aren't suddenly locked out?
  - If raising `MINIMUM_CLIENT_PROTOCOL_VERSION`: have you given
    enough notice in the Discord channel for players to update?
- [ ] Did any scenario config file change in a way that changes how
      the client renders it? Client has a copy in `scenario_cache`
      keyed by content-hash; check that the hash-mismatch path
      refreshes correctly. (Non-breaking, but smoke-test.)
- [ ] Did any MCP tool change its argument list or response shape?
  - If adding optional args: fine, that's non-breaking.
  - If changing semantics: must be a `PROTOCOL_VERSION` bump.
- [ ] Run `pytest tests/test_protocol_version.py` — these tests
      pin the handshake contract.

Once the server is up:

- [ ] `journalctl -u silicon-serve.service --since '-1m'` shows
      normal "re-attached NEW FileHandler" + no crashes.
- [ ] Smoke-connect a local client at the current `PROTOCOL_VERSION`
      — should reach the lobby in < 1 second.

---

## Local-development workflow

- `silicon-serve` and `silicon-join` both read the same
  `silicon_pantheon.shared.protocol` module, so your local dev
  environment always talks to itself.
- To test the upgrade flow: temporarily raise
  `MINIMUM_CLIENT_PROTOCOL_VERSION` to `2` in
  `shared/protocol.py`, restart the server, connect with a client
  that still sends `PROTOCOL_VERSION = 1` — you should see the
  `UpgradeRequiredScreen` with the client-too-old message. Revert
  the constant after the experiment.

---

## FAQ

**Why not just use the package `version` from `pyproject.toml`?**
Package versions bump on every release (bugfixes, new scenarios,
copy edits). The protocol version only changes when the wire format
changes — a rare event that warrants its own careful release
cadence.

**Can a client be both newer AND older than the server at once?**
Yes. A client built against the main branch might carry a locally
bumped `MINIMUM_SERVER_PROTOCOL_VERSION` and still be lower than
the production server's `PROTOCOL_VERSION`. The three-constant
design handles both directions.

**What if the server returns no `server_protocol_version` at all?**
Treat it as protocol version 0 (the ancient past). Client refuses
to play if `MINIMUM_SERVER_PROTOCOL_VERSION > 0`. Today that
condition is false, so missing version is tolerated.

**What if the client omits `client_protocol_version` entirely?**
Server treats it as v1 (the pre-handshake-aware baseline). At
`MIN_CLIENT = 1` that's accepted; once `MIN_CLIENT` is raised above
1, omission falls below the minimum and the client gets
`CLIENT_TOO_OLD`. There's a regression test for this
(`test_client_omitting_version_rejected_once_minimum_exceeds_v1`).

**Does the server remember the client's version for later calls?**
Yes — `Connection.client_protocol_version` is set at
`set_player_metadata` and readable by any tool handler that needs
to shim its response shape during a rollout window. Non-handshake
tools (`heartbeat`, `whoami`) don't need this; most tool handlers
won't either unless they're participating in a live breaking
change.

**What if a client re-calls `set_player_metadata` later without the
version arg?** The stored `Connection.client_protocol_version`
is **not** overwritten — only re-calls that explicitly supply a
version update the stamp. Otherwise a compat-shim handler branching
on `conn.client_protocol_version >= 2` could start emitting old-shape
responses to a client that's actually on v2 just because the client
happened to re-auth through a code path that didn't forward the
version. Regression test:
`test_reauth_without_version_does_not_regress_stored_version`.

**What if the server's response doesn't include
`server_protocol_version` at all?** The client treats it as v0
(the ancient past) and — crucially — does NOT short-circuit the
`< MINIMUM_SERVER_PROTOCOL_VERSION` check. A future server that
forgets to send the field gets rejected the same as an explicitly
v0 server. Regression test:
`test_server_without_version_field_rejected_when_min_server_raised`.

**Does the client clean up the transport if the handshake fails?**
When the version-mismatch path triggers, the transport is left
open but the user is routed to `UpgradeRequiredScreen`. Pressing
Enter there cleans up the transport (`app._transport_cleanup()`)
before returning to the login screen, so the retry doesn't stack a
second transport on top. Pressing q/Esc exits the process (cleanup
happens implicitly).
