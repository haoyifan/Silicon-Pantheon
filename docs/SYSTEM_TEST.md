# System-test framework

`silicon-system-test` is an unattended end-to-end fuzz harness for
silicon-pantheon. It spawns a throwaway `silicon-serve`, drives N
concurrent matches with random-action agents over the real MCP+SSE
transport, and bundles every log, replay, and manifest into a single
timestamped directory. Pair it with the `/review-system-test` skill
to auto-triage the bundle.

**When to use it:**

- Pre-release: shake out regressions across transport, game rules,
  fog of war, and lobby/room lifecycle before exposing the server to
  users.
- Soak testing: confirm the server is stable under concurrent load
  (50-100 clients) without piling up connections or leaks.
- Bug reproduction: capture a full incident bundle (server log,
  replays, per-client transcript) that a reviewer can triage
  asynchronously.

**When NOT to use it:**

- Unit tests — too slow (~30-60s per run at minimum). Use `pytest`
  for tight feedback.
- LLM correctness — the default random-action agents don't exercise
  prompts or provider adapters. Opt into LLM agents via per-slot
  overrides only for narrow provider-path checks.

Sections:

- [Quick start](#quick-start)
- [How it works](#how-it-works)
- [Config reference](#config-reference)
- [CLI](#cli)
- [Bundle layout](#bundle-layout)
- [Reviewing a bundle](#reviewing-a-bundle)
- [Troubleshooting](#troubleshooting)
- [Design notes](#design-notes)

---

## Quick start

```bash
# Install (first time only)
uv sync --extra dev

# Smallest possible run: N=1 match, random agents, ~30s wall clock.
# Handy for verifying the framework itself works on a new machine.
uv run silicon-system-test --config system_test.example.toml -N 1

# Default: N=10 matches, all random, ~5-15 minutes depending on
# which scenarios get picked.
uv run silicon-system-test --config system_test.example.toml
```

Output goes to `~/silicon-system-test-results/<timestamp>/`. The CLI
prints the bundle path on stdout when it finishes.

---

## How it works

One run proceeds in phases:

1. **Preflight.** Parse the config, compute a bundle directory path,
   verify the server port isn't already in use. Refuse to start if
   anything's off.
2. **Server bring-up.** Spawn `silicon-serve` as a subprocess with
   `HOME=<bundle>/server/home` so its logs, replays, and leaderboard
   land in the bundle, not your user's real data dir. Poll
   `GET /health` until it's reachable.
3. **Stagger.** Spawn N host `silicon-host` subprocesses (each with
   `one_shot=true` so they exit after one match). Wait 5 s so each
   host has time to call `create_room`. Then spawn N joiner
   subprocesses with `join_only=true` — they list rooms, pick any
   that's still `waiting_for_players`, and join.
4. **Run.** Poll every 2 s. When a subprocess exits, record its
   return code. Continue until all 2N subprocesses have exited OR
   the global timeout fires.
5. **Collect.** SIGTERM any survivors, copy server logs + replays +
   leaderboard into the bundle, write `run-manifest.json` and
   `INCIDENTS.md`.

Each agent is its own subprocess so a crash in one doesn't take the
others down. The orchestrator watches every subprocess's return
code; a non-zero exit becomes an incident in the manifest.

The framework is **local-mode only** today — both `server.ip` and
`client.ip` must be `127.0.0.1` / `localhost`. Remote SSH is planned
but not implemented.

---

## Config reference

The TOML file is the canonical source; see
[`system_test.example.toml`](../system_test.example.toml) in the
repo root. Schema:

```toml
[server]
ip = "127.0.0.1"       # only 127.0.0.1 / localhost supported today
port = 8090            # throwaway port; nothing should be listening here

[client]
ip = "127.0.0.1"

[run]
num_matches = 10       # N: the test spawns 2N agents (N hosts + N joiners)
timeout_s = 14400      # 4h global wall-clock cap; survivors get SIGTERM
seed = 42              # optional, deterministic planning + random-bot RNG

[defaults]
mode = "random"        # "random" | "llm"
provider = "xai"       # only consulted when mode = "llm"
model = "grok-3-mini"
locale = "en"
turn_time_limit_s = 1800

[randomize]
scenarios = "all"      # or an explicit list: ["01_tiny_skirmish", ...]
fog_modes = ["none", "classic", "line_of_sight"]
team_assignments = ["fixed"]
locales = ["en"]
max_turns_range = [8, 20]

# Optional per-slot override — rarely needed
# Slots 0..N-1 are hosts, slots N..2N-1 are joiners.
[[agent]]
slot = 0
mode = "llm"
model = "claude-haiku-4-5"
provider = "anthropic"
```

**Default is full random-action**. You almost never need `[[agent]]`
overrides — they exist for the narrow case of "I want ONE llm agent
in an otherwise random run, to exercise the provider adapter path
under real load."

### Mode: "random" vs "llm"

- **`random`** (default) uses `RandomNetworkAgent`: picks a uniformly
  random legal action each turn via the MCP tool interface, with a
  mild bias toward lethal attacks. No LLM, no prompts, no cost.
  Exercises transport + server + rules + fog. Good for most testing.
- **`llm`** uses `NetworkedAgent` with the configured provider/model,
  same as auto-host. Real LLM calls, real credentials required. Use
  sparingly — 20 concurrent LLM agents can burn tens of dollars.

### Reproducibility

When `[run].seed` is set, the orchestrator uses it for:

- Picking scenarios per match (shuffle)
- Picking fog mode / team_assignment / locale per match
- Deriving per-agent RNG seeds for random-mode agents (seed × 1000
  + slot)

Two runs with the same seed + same config produce the same agent
plan and the same random-bot trajectories (modulo network-latency
nondeterminism in tool-call ordering).

---

## CLI

```
silicon-system-test [-h] --config CONFIG [--out-dir OUT_DIR]
                    [-N NUM_MATCHES] [--seed SEED] [--dry-run]
```

- `--config PATH` (required) — TOML file as above.
- `-N NUM_MATCHES` — override `run.num_matches`. Handy for quick
  smoke runs without editing the TOML.
- `--seed SEED` — override `run.seed`. Combine with `-N 1` for tight
  reproducible smoke runs.
- `--out-dir DIR` — write the bundle under this base instead of
  `~/silicon-system-test-results/`.
- `--dry-run` — parse config, compute the bundle path, print the
  plan, exit. Does NOT spawn anything. Use to validate a TOML.

**Exit codes:**

- `0` — all agents exited cleanly, no timeout
- `1` — at least one agent crashed OR the global timeout fired
- `2` — config file missing or invalid
- `130` — `Ctrl-C` during the run (bundle may be incomplete)

---

## Bundle layout

Every run produces a single directory like
`~/silicon-system-test-results/20260422T001119/`:

```
/
  run-manifest.json       machine-readable: agents, config, outcomes
  INCIDENTS.md            orchestrator-detected crashes / timeouts
  orchestrator.log        what the orchestrator did, when
  server/
    silicon-serve.stdout.log   stdout + stderr from the server subprocess
    server-.log    silicon-serve's structured log file
    replays/*.jsonl            one replay per completed match
    leaderboard.db             sqlite snapshot
  clients/
    -host.toml         the per-agent silicon-host config we generated
    -host.log          silicon-host's structured log
    -host.stdout.log   stdout + stderr from the host subprocess
    -joiner.{toml,log,stdout.log}
```

### `run-manifest.json`

```jsonc
{
  "started_at": "2026-04-22T00:11:19",
  "wall_clock_s": 35.7,
  "timed_out": false,
  "git_sha": "2fc9318...",
  "config": {  /* echoes the parsed config */  },
  "agents": [
    {
      "slot": 0, "role": "host", "name": "match00-host",
      "scenario": "01_tiny_skirmish",
      "mode": "random", "model": "grok-3-mini", "provider": "xai",
      "pid": 123456, "returncode": 0,
      "toml_path": "/.../match00-host.toml",
      "log_path":  "/.../match00-host.log",
      "stdout_path": "/.../match00-host.stdout.log"
    },
    ...
  ],
  "summary": {
    "n_agents": 2,
    "n_clean_exit": 2,
    "n_crashed": 0,
    "n_killed_by_timeout": 0
  }
}
```

### `INCIDENTS.md`

Human-readable summary of what the orchestrator flagged during the
run: subprocess crashes, global-timeout survivors. Deeper analysis
(fog leaks, slow tool calls, server warnings) is NOT auto-generated
here — run the `/review-system-test` skill for that.

---

## Reviewing a bundle

In Claude Code:

```
/review-system-test /path/to/bundle
```

or just:

```
/review-system-test
```

with no arguments to auto-pick the most recent bundle.

The skill produces a severity-ranked markdown report
(CRITICAL → HIGH → MEDIUM → LOW → INFO) with `file:line`
citations. It checks:

- Manifest outcomes (clean exits vs crashes vs timeouts)
- Server log: crashes, `InvariantViolation`, `fog_leak_suspect`,
  `tool handler STUCK`
- Performance: `SLOW`, heartbeat drift, eviction patterns
- Per-client transport: `HUNG` / `TIMEOUT` / `transport DEAD`
  (Layer 1/2/3 signals)
- Per-client game: forced concede, no-progress retries
- Replay consistency: every started match should end in `game_over`
- Bundle completeness: every manifest agent has matching files

Bottom line is always one of: "ship it", "block — N critical
findings", or "flaky, investigate before shipping".

---

## Troubleshooting

**"port 8090 is already in use"** — something else is listening
there. Kill it, or change `[server].port` in the config.

**"silicon-serve did not become healthy in 30 s"** — the server
failed to start. Check `server/silicon-serve.stdout.log` for the
error. Usually a missing dependency, port conflict, or a config
typo in the server build path.

**Random-vs-random run timed out** — on very large scenarios
(e.g., 32_battle_of_new_york at 16×14 with 17 units, classic fog),
two random agents can fail to converge within the per-match turn
limit. Either:

1. Restrict `[randomize].scenarios` to smaller scenarios for the
   run (e.g., the 1-5 scenarios), OR
2. Widen `[randomize].max_turns_range` upper bound so the draw rule
   kicks in sooner, OR
3. Accept that some matches will be killed by the global timeout.

**Agents crashed with `xai adapter selected but no API key…`** —
this is `silicon-host`'s startup preflight for LLM workers. In
random-mode it's skipped. If you opted a specific slot into LLM
mode via `[[agent]]`, make sure the API key env var is set OR the
credential is in `~/.silicon-pantheon/credentials.json`.

**Nothing happens after "joiners spawned"** — check
`clients/match00-joiner.log`. The joiner polls `list_rooms` up to
60 s looking for a `waiting_for_players` room; if no hosts
published one, it fails. Usually means the host crashed earlier;
look at its `.stdout.log`.

**"remote SSH mode is not yet implemented"** — set both
`[server].ip` and `[client].ip` to `127.0.0.1` or `localhost`. The
framework runs locally only for now.

---

## Design notes

Detailed rationale for each design decision is in
`~/dev/system-test-plan.md` (not in the repo). The highlights:

- **`HOME` override, not `SILICON_DATA_DIR`.** Python's
  `Path.home()` reads `$HOME` on Linux; every code site that uses
  `~/.silicon-pantheon/...` naturally lands in the bundle dir with
  zero codebase changes. Cleaner than adding a new env var that all
  callers would need to respect.
- **One subprocess per agent**, not one-process-with-2N-workers.
  Isolation matters more than overhead: a crash in one agent
  doesn't take down the rest, and the orchestrator's
  "did-this-exit?" signal becomes trivial (just `proc.poll()`).
- **`one_shot` flag on `WorkerConfig`**, not a new `silicon-host`
  binary. Reuses all the existing retry + reconnect + game-loop
  code; the only change is "exit after one completed match instead
  of looping."
- **`join_only` flag for joiners**, learning team assignment from
  the room state. The alternative — orchestrator picks rooms and
  passes explicit `room_id`s to joiners — is more coordinated but
  much more code. The polling approach degrades gracefully if hosts
  are slow to publish.
- **Stagger delay of 5 s between hosts and joiners.** Long enough
  that every host's `create_room` has landed; short enough that the
  total run isn't noticeably slowed. On slower hosts you can
  increase this in `orchestrator.py:STAGGER_DELAY_S` — though at
  that point you probably want to fix whatever's slow.
- **Random-action default, LLM opt-in.** A full N=10 random run
  costs $0 and ~5 min. A full LLM run can cost tens of dollars.
  Making random the default lets you run the framework often
  enough to catch regressions pre-release without a budget review.

---

## See also

- `system_test.example.toml` — the canonical example config
- `src/silicon_pantheon/systemtest/` — framework source
- `.claude/skills/review-system-test/SKILL.md` — triage skill
- `src/silicon_pantheon/client/random_agent.py` — the random-action
  agent implementation
- `docs/USAGE.md` — general silicon-pantheon CLI reference
- `docs/THREADING.md` — server-side locking model (useful when
  interpreting the skill's `sweeper` / `state_lock` findings)
