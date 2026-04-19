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

Call the new protocol version **N+1**.

### 1. Land the code change that bumps `PROTOCOL_VERSION`

In the same PR:
- Raise `PROTOCOL_VERSION = N + 1` in `shared/protocol.py`.
- Implement the new wire shape (server + client).
- Add tests covering the new shape.
- **Do NOT raise `MINIMUM_CLIENT_PROTOCOL_VERSION` yet.** The server
  should still accept old clients while the rollout is in progress —
  they'll just use the old code path (which you need to keep working
  for now via a compatibility shim, if the change touches a path
  old clients still use).

### 2. Deploy the server

- `git pull && sudo systemctl restart silicon-serve.service` on
  the production box.
- At this point: new server is up. Old clients still work. New
  clients also work. Nobody is locked out.

### 3. Let clients catch up

- Announce the new version in the Discord community channel.
- Wait enough time for active players to update — usually a
  week, but judge by actual connection telemetry.
- While waiting, don't ship another breaking change. One at a time.

### 4. Raise `MINIMUM_CLIENT_PROTOCOL_VERSION = N + 1` and redeploy the server

- Now old (version-N) clients get `CLIENT_TOO_OLD` on login with a
  friendly upgrade prompt. They can't play until they upgrade, but
  they know exactly what to do.
- This is the point at which you can remove the compatibility shim
  from step 1 — no clients below `MIN_CLIENT` are reaching that code
  path anymore.

Doing 1–4 in one big bang (bumping `PROTOCOL_VERSION` AND
`MINIMUM_CLIENT_PROTOCOL_VERSION` in the same deploy) is allowed only
for true emergencies — it hard-gates every unupdated client the
moment the server restarts, which is user-hostile.

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
