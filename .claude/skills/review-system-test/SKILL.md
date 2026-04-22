# System-Test Bundle Review Skill

You are an experienced SRE + backend engineer reviewing a
system-test bundle produced by `silicon-system-test`. The bundle
captures everything from one run: server logs, per-agent logs,
replay files, plus the orchestrator's own log and manifest.

## Input

`` is the path to a bundle directory. If empty, look under
`~/silicon-system-test-results/` for the most recent one:

```bash
ls -t ~/silicon-system-test-results/ | head -1
```

Layout you should expect:

```
/
  run-manifest.json       # machine-readable: agents, config, outcomes
  INCIDENTS.md            # orchestrator-detected problems (pre-written)
  orchestrator.log        # what the orchestrator did, when
  server/
    silicon-serve.stdout.log
    *.log                 # silicon-serve's log file (pid-and-ts named)
    replays/*.jsonl       # one replay per completed match
    leaderboard.db        # sqlite, if any matches counted
  clients/
    -host.toml
    -host.log
    -host.stdout.log
    -joiner.toml
    ...
```

## What to check

Walk through the list, report every issue with severity:
**CRITICAL** (run can't be trusted), **HIGH** (likely regression),
**MEDIUM** (flaky / warning), **LOW** (noise), **INFO**.

### 1. Manifest summary (CRITICAL if missing)

Read `run-manifest.json`:
- `summary.n_crashed > 0` → each crashed agent is a **HIGH** finding; name it and cite the stdout tail
- `summary.n_killed_by_timeout > 0` → **HIGH**; the run ran out of wall clock
- `timed_out: true` → **CRITICAL** in most cases (random-vs-random should never hit the 4 h cap)
- `config.run.num_matches vs summary.n_clean_exit` — matches that didn't reach game_over are worth flagging

### 2. Orchestrator-detected incidents (CRITICAL-for-each)

Read `INCIDENTS.md`. The orchestrator has already pre-classified obvious
failures. Every line there is at minimum **HIGH**; treat as first-class
findings in your report.

### 3. Server log — crashes + invariants (CRITICAL)

`server/*.log` (not the stdout one — the `silicon-*.log` file):

```bash
grep -E "Traceback|InvariantViolation|invariant_violation|ERROR" server/*.log | head -50
grep -E "fog_leak_suspect" server/*.log | head -20
grep -E "tool handler STUCK|dispatch has not completed" server/*.log | head
```

- Any `Traceback` or `InvariantViolation` → **CRITICAL**, that's a real bug.
- `fog_leak_suspect` → **HIGH**, server saw a hidden enemy id leak to the response. Cite the tool name from the log line.
- `tool handler STUCK` (from the 10-s watchdog) → **HIGH**, a tool took > 10 s to dispatch. Include the tool name + cid.

### 4. Server log — performance signals (MEDIUM unless many)

```bash
grep -E "SLOW|sweep tick.*idle" server/*.log | head
grep -E "heartbeat_dead|evicting" server/*.log | head
```

- `heartbeat_dead` evictions that happen while the game was in-game
  (not in lobby) suggest the agent wedged. Cross-ref with the
  corresponding client log — did Layer 1/2/3 resilience fire?
- `sweep tick: … state=in_game idle=45+` without a matching
  eviction → **HIGH**, sweeper saw a dead connection but didn't
  act on it

### 5. Caddy aborts (if a Caddy is involved — usually not in local-mode runs)

Local mode talks directly to silicon-serve; this section only
applies when the bundle includes a Caddy journal (not yet wired
into the orchestrator). Skip when the bundle has no caddy log.

### 6. Per-client logs — transport health (MEDIUM)

For each `clients/*.log`:

```bash
grep -cE "call SLOW|HUNG|TIMEOUT|transport DEAD" clients/-host.log
```

- `transport DEAD detected` lines → **INFO**: Layer 1 resilience
  fired. If followed by a matching `forcing reconnect` and a new
  `worker X connected cid=…`, the recovery worked. If not →
  **HIGH**.
- `call SLOW` durations clustered at 5.0–5.5s → **HIGH**, the
  keep-alive/chunked-race bug (should not happen with
  `json_response=True` server-side)
- `HUNG … ws_closed=True` followed by `TIMEOUT` → zombie forming;
  check if the worker then reconnected

### 7. Per-client logs — game-level anomalies

```bash
grep -E "got winner|game_over|GAME_OVER|ERROR" clients/-host.log clients/-joiner.log | head
```

- `summarize_match failed` → **MEDIUM**, LLM post-game hook broke
- `no_progress_retries > N` → **MEDIUM**, agent wedged but recovered
- `concede` firing from the worker (not the human) in an LLM-mode
  run → **MEDIUM**, model ran out of turn budget

### 8. Replay files — match outcomes (INFO)

`server/replays/*.jsonl` — one per completed match. You can
correlate with clients/*.toml to pair matches with their agents.

```bash
for r in server/replays/*.jsonl; do
  echo "=== $r ==="
  grep -E "\"event\":\"game_over\"|winner" "$r" | head -3
done
```

- Matches that don't end in `game_over` → the match was
  interrupted; cross-ref with the agent stdout
- Both agents playing random-vs-random should produce a winner
  within the scenario's max_turns; if `max_turns_draw` fired
  often, the random bot's action selection may be biased toward
  stalling

### 9. Orchestrator log (INFO unless it has errors)

```bash
grep -E "ERROR|WARNING" orchestrator.log
```

Usually uninteresting; the orchestrator is small. Only flag
unexpected warnings.

### 10. Bundle completeness (INFO)

- Every agent in manifest has matching `.log` / `.stdout.log` /
  `.toml` in `clients/`
- Server log file exists and isn't empty
- If a pcap directory is present (only when `--diagnose-sse` was
  on for the test), sanity-check file sizes

## Output format

One section per severity, highest first. Within each section, one
bullet per finding:

```
- **CRITICAL** (server/silicon-*.log:423): InvariantViolation during
  move on cid=a1b2 — fog_target_check raised in debug mode. Cite:
  `<one-line excerpt>`.
```

Always cite `file:line` so the operator can jump to the evidence.
End with a **Summary** table:

| Severity | Count |
|---|---|
| CRITICAL | N |
| HIGH | N |
| MEDIUM | N |
| LOW | N |
| INFO | N |

And a one-sentence **Bottom line**: "ship it", "block — N
critical findings", or "flaky, investigate before shipping".

## Scope discipline

- Don't read every log line — use `grep` / `awk` to target
  specific patterns. Bundles can be >100 MB.
- Don't speculate about causes for findings with only one data
  point; raise to the user and let them decide.
- Don't re-derive things already in `INCIDENTS.md` — cite it
  directly.
- Cap the report at ~1000 words unless there are genuinely many
  findings.
