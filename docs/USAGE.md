# Usage guide

A hands-on reference for the CLIs shipped with this repo. Two modes
of play:

- **Local** (single process): `clash-match` runs both agents in one
  Python process; `clash-play` replays the result.
- **Networked** (backend + clients): `clash-serve` hosts matches;
  `clash-join` connects a TUI client. Supports lobby, host/join,
  ready-up, fog of war, disconnect handling, and replay download.

Sections:

- [Quick start](#quick-start)
- [`clash-match` — run a local match](#clash-match--run-a-match)
- [Lessons](#lessons)
- [Real-time reasoning](#real-time-reasoning)
- [Coaching during a match](#coaching-during-a-match)
- [`clash-play` — interactive step-through replayer](#clash-play--interactive-step-through-replayer)
- [**Networked play: `clash-serve` + `clash-join`**](#networked-play-clash-serve--clash-join)
- [Complete example workflows](#complete-example-workflows)

---

## Quick start

```bash
# Install dependencies.
uv sync --extra dev

# Smoke test: two random bots play the smallest scenario end-to-end.
uv run clash-match --game 01_tiny_skirmish --blue random --red random --render
```

Claude-backed providers need the `claude` CLI (Claude Code) installed and
logged in — no API key required.

---

## `clash-match` — run a match

```
clash-match [options]

Options (all optional):
  --game GAME                   scenario name (folder under games/). Default: 01_tiny_skirmish.
  --blue BLUE                   provider spec. Default: random.
  --red RED                     provider spec. Default: random.
  --blue-strategy PATH          path to a STRATEGY.md for blue (see strategies/).
  --red-strategy PATH           path to a STRATEGY.md for red.
  --max-turns N                 override the scenario's max_turns.
  --replay PATH                 explicit replay path. Default: <run-dir>/replay.jsonl.
  --render                      live TUI (board + units + agent reasoning panel).
  --seed N                      seed for random providers.
  --thoughts-height N           rows for the reasoning panel in --render mode. Default: 12.
  --coach-file-blue PATH        watch this file and send new lines to blue as coach messages.
  --coach-file-red PATH         same, for red.
  --runs-dir PATH               parent of the auto-created per-match folder. Default: ./runs.
  --no-run-dir                  don't auto-create a per-match folder.
  --lessons-dir PATH            where lessons are read from and written to. Default: ./lessons.
  --no-lessons                  skip both writing lessons AND injecting priors into prompts.
```

### Provider specs

| Spec | What it is |
|---|---|
| `random` | Picks a legal action at random. No LLM, no network, free. |
| `claude-haiku-4-5` | Cheapest Claude; fine for iteration. |
| `claude-sonnet-4-6` | Stronger; ~$X per match — see README's cost warning. |
| `claude-opus-4-6` | Strongest; slowest & priciest. |
| `gpt-*` | OpenAI stub (Phase 7; not fully wired). |

### Run directories

Every invocation creates a folder at `./runs/{timestamp}_{scenario}/` and
routes per-match artifacts into it. After a match ends you'll find:

```
runs/20260412T143022_01_tiny_skirmish/
├── replay.jsonl      every event (match_start, thoughts, actions, coach, errors)
└── thoughts.log      plain-text stream of agent reasoning, one thought per line
```

The run directory's path is printed at the start of every match. Use
`--no-run-dir` to suppress if you're iterating quickly and don't want
the clutter.

---

## Lessons

After a match ends, each Claude-backed provider writes a short
markdown lesson reflecting on one key decision or pattern that drove the
outcome. Lessons live at `./lessons/<scenario>/<slug>.md`, sharded by
scenario so the system-prompt injection in future matches only pulls
relevant priors.

### Anatomy of a lesson file

```markdown
---
title: Don't chase healers into the enemy fort
slug: dont-chase-healers-into-enemy-fort
scenario: 02_basic_mirror
team: red
model: claude-sonnet-4-6
outcome: loss
reason: seize
created_at: 2026-04-12T14:30:00+00:00
---

## Situation
Blue's mage was two tiles from our home fort with a knight escort...

## Lesson
Prioritize fort defense over kill-chasing when a healer is within
reach of the fort. A full-HP mage on a fort will outheal the damage
you can deal in two turns.
```

Filenames are slugified from the model-chosen title for searchability
(`grep -l "terrain" lessons/02_basic_mirror/`). Collisions within a
scenario disambiguate with a `-2`, `-3`, ... suffix.

### How lessons are injected

**Injection is automatic and happens in the system prompt** — no tool
call, no agent opt-in, no CLI flag needed. As long as lessons exist on
disk for the scenario you're playing, the agent sees them at the start
of every turn.

**What the agent actually sees** (appended after the rules and any
`--blue-strategy` / `--red-strategy` section):

```markdown
## Prior lessons from this scenario

These are reflections written by agents who played this scenario before you
(including past games you lost). Internalize the tactical principles — do not
just replay past moves.

### <title-of-lesson-1> [<team> <outcome>]

<body of lesson 1>

### <title-of-lesson-2> [<team> <outcome>]

<body of lesson 2>

...
```

**Selection rules:**

- **Scope**: lessons for *this scenario* only (the `scenario` field in a
  lesson's frontmatter must match `--game`). A lesson from
  `02_basic_mirror` is invisible while playing `01_tiny_skirmish`.
- **Cross-team**: both blue and red see **all** lessons for the
  scenario, regardless of which team wrote them. The `[<team> <outcome>]`
  tag tells the reader which side's perspective produced it so the agent
  can weight advice accordingly.
- **Ordering**: newest first, sorted by the `created_at` frontmatter
  field.
- **Cap**: up to **5** lessons per turn (`AnthropicProvider.max_injected_lessons`,
  defaults to 5). The cap is currently internal — edit the provider
  constructor if you need to override.
- **Cadence**: re-loaded every turn, so lessons written by one match are
  visible to the very next match on the same scenario.

**Failure modes** (all silent, by design — lessons are advisory):

- Lessons directory missing or empty → nothing injected, no warning.
- A lesson file has malformed YAML frontmatter → that one file is
  skipped via `try/except` in `LessonStore.list_for_scenario`; others
  still load.
- `--no-lessons` → disables both producer (won't write) and consumer
  (won't inject) in one switch.
- `--lessons-dir PATH` → point at a different folder; useful for
  A/B-ing a curated set.

**Want to force-inject a specific lesson?** Drop a hand-written
markdown file at `lessons/<scenario>/my-lesson.md` with correct
frontmatter. It's indistinguishable from an agent-written one once on
disk. Minimal example:

```markdown
---
title: Always check the threat map before moving cavalry
slug: threat-map-before-cavalry
scenario: 02_basic_mirror
team: coach
model: hand-written
outcome: advice
reason: ""
created_at: 2026-04-12T00:00:00+00:00
---

Cavalry dies to archers. Before moving one into the open, call
`get_threat_map` and verify no enemy archer can reach the destination tile.
```

The `team` / `outcome` / `model` fields are free-form strings; they
only feed the header tag the agent sees. `created_at` drives ordering,
so use a recent timestamp if you want your hand-written lesson to sit
above the auto-generated ones.

### Controlling lesson behavior

```bash
# Disable both writing new lessons AND injecting priors (useful while
# iterating on prompts so you don't pollute the corpus).
uv run clash-match --game 01_tiny_skirmish --no-lessons --render ...

# Point at an alternate lessons folder (e.g. to test with a curated set).
uv run clash-match --lessons-dir ./my-curated-lessons ...
```

Lessons are plain files — delete, hand-edit, or commit them to git at
will.

---

## Real-time reasoning

Three ways to watch the agent think:

1. **The TUI reasoning panel** (`--render`). A fixed-height panel at the
   bottom of the live view shows the last N thoughts, tagged with turn
   and team. Bump it with `--thoughts-height 30` for more context.

2. **`thoughts.log` tail** — every thought is also streamed
   line-by-line to `runs/<ts>_<scenario>/thoughts.log`. One thought per
   line, whitespace collapsed, tagged `[T{turn} {team}]`. Bidirectional
   scroll via `less`:

   ```bash
   # Find the run (printed at match start, or):
   ls -1t runs/ | head -1

   # In a second terminal, follow live:
   less +F --follow-name runs/20260412T143022_01_tiny_skirmish/thoughts.log
   #   Ctrl-C in less to pause following; / to search; q to quit.
   ```

3. **`clash-play`** (post-match). Interactive step-through visual
   replayer — see the section below for a full walkthrough of the board
   alongside each thought and action.

---

## Coaching during a match

A coach is a text file you append lines to while the match runs. Each
line becomes a coach message delivered to the team's agent at the start
of its next turn.

```bash
# Terminal 1 — start the match with coach file wiring:
uv run clash-match \
  --game 02_basic_mirror \
  --blue claude-sonnet-4-6 --red claude-sonnet-4-6 \
  --coach-file-blue coach_blue.txt \
  --coach-file-red  coach_red.txt  \
  --render

# Terminal 2 — issue guidance at any time. One line = one message.
echo "push the cavalry on the right flank" >> coach_blue.txt
echo "fall back to the fort, they're breaking through" >> coach_blue.txt
```

The agent calls `get_coach_messages` at the start of each turn and
drains the queue. Messages logged to the replay as `coach_message`
events (visible in `clash-play`).

Tip: create the coach files before starting the match (`touch
coach_blue.txt`) so the watcher has something to follow from turn one.

---

## `clash-play` — interactive step-through replayer

```
clash-play [run_dir] [--replay PATH]
```

Reconstructs the match visually, one event at a time. The board starts
at the scenario's initial state and updates as you step through actions.
Agent reasoning appears in a side panel *before* the paired action
updates the board — you see what the agent thought, then what it did.

**Controls** (single keypress — no Enter required on an interactive TTY;
non-TTY/piped stdin falls back to line input):

| Key | Action |
|---|---|
| `Enter` or `k` | advance one step |
| `j` | go back one step |
| `s` | skip forward to the next action event (past any thoughts) |
| `q` | quit |
| `Ctrl-C` | abort |

Backward navigation is O(1): on launch the replayer precomputes a
`GameState` snapshot for every step, so rewinding is as cheap as
advancing. Feel free to wander back and forth freely.

```bash
uv run clash-play runs/20260412T143022_01_tiny_skirmish
```

If the replay is older than the `match_start` metadata event
(commit `1deb07a`, 2026-04-12), the replayer will refuse with a clear
error — re-record the match to get the metadata.

---

## Networked play: `clash-serve` + `clash-join`

Two processes, one machine or many:

- **`clash-serve`** runs the authoritative backend over MCP + streamable
  HTTP (SSE). Holds the engine, rooms, tokens, heartbeat sweeper,
  replay storage. Stateless across restarts today; in-memory only.
- **`clash-join`** is the client — full TUI by default, with a
  `--smoke` fallback for connectivity testing.

### Start the backend

```bash
uv run clash-serve --host 127.0.0.1 --port 8080
# Prints:  clash-serve starting on http://127.0.0.1:8080
```

Remote access: point `--host 0.0.0.0` (public bind) and make sure the
port is reachable. For friends-only matches on a VPS this is enough;
public deployment needs more (auth, rate limits) and is out of scope
for Phase 1.

### Connect a client

```bash
uv run clash-join                     # TUI prompts for everything
uv run clash-join --url http://<host>:8080/mcp/ --name alice --kind ai
```

TUI flow:

| Screen | What it does | Keys |
|---|---|---|
| **login** | enter server URL, display name, kind (ai/human/hybrid), optional provider/model | Tab/↓ next field, Shift-Tab/↑ prev, ←/→ cycle kind, Enter submit, q quit |
| **lobby** | list open rooms, host a new one, join, preview | j/k or ↓/↑ select, Enter join, p preview, n new room, r refresh, q quit |
| **room** | preview the scenario, see seats + readiness, toggle ready and wait for auto-start | r toggle ready, l leave, q quit |
| **game** | live board view of the server-authoritative state (fog-masked if enabled) | e end_turn, c concede, q quit |
| **post-match** | winner banner + survivor summary; download replay locally | d download replay, l back to lobby, Enter same, q quit |

### Lobby flow

The server promotes the room to `in_game` ten seconds after both
players press `r`. The countdown resets if either player unreadies,
leaves, or disconnects.

Team assignment is picked at room-creation time:
- `--team_assignment fixed` (default) → host gets `host_team`, joiner gets the other.
- `--team_assignment random` → coin flip at game start; recorded in
  the replay.

### Fog of war

Per-room setting. Three modes:

- `none` — no filtering; both sides see everything. Good for
  debugging.
- `classic` (default) — tiles revealed once stay visible; enemy
  units only while currently in sight. The Session tracks per-team
  `ever_seen` across half-turns.
- `line_of_sight` — only currently visible tiles show anything.

Sight stats per class: Knight 2, Archer 4, Cavalry 3, Mage 3.
Forest + Mountain block line-of-sight past them unless adjacent.

### Disconnects + reconnects

Server runs a heartbeat sweeper every second:

| Context | Grace window | Effect |
|---|---|---|
| any state | 30s no heartbeat | → soft_disconnect |
| in_lobby soft 30s | 60s total idle | connection dropped |
| in_room soft 30s | 60s total idle | seat vacated; room reverts to waiting |
| in_game soft 60s | 90s total idle | opponent notified (log only) |
| in_game soft 120s | 150s total idle | auto-concede; opponent wins by disconnect_forfeit |

Clients send `heartbeat` every 10s automatically (via
`ServerClient.start_heartbeat`, started by the TUI on login).

Reconnect-mid-match (Phase 1d+) isn't wired yet — if your client
drops during a game, you lose. Rejoining as a fresh connection will
find the seat already vacated.

### Replay download

On the post-match screen, `d` calls the `download_replay` tool and
saves the result to
`~/.clash-of-robots/replays/<room_id>.jsonl`. Feed it to `clash-play`
locally to scroll through the match:

```bash
uv run clash-play --replay ~/.clash-of-robots/replays/<room_id>.jsonl
```

The post-match token is valid for about a minute after `game_over`;
download before leaving the screen.

### Smoke test the transport without the TUI

```bash
uv run clash-join --smoke --name alice --kind ai \
  --url http://127.0.0.1:8080/mcp/
# → connected: connection_id=...
#   whoami (pre): {...}
#   set_player_metadata: {...}
#   heartbeat: {...}
#   whoami (post): {...}
```

Useful for verifying auth / state transitions in isolation.

---

## Complete example workflows

### 1. Run a Claude-vs-Claude match with coaching and real-time reasoning monitoring

```bash
# Pre-create the coach files so the watcher starts at turn 1.
touch coach_blue.txt coach_red.txt

# Terminal 1 — start the match.
uv run clash-match \
  --game 02_basic_mirror \
  --blue claude-haiku-4-5 --blue-strategy strategies/aggressive_rush.md \
  --red  claude-haiku-4-5 --red-strategy  strategies/defensive_chokepoint.md \
  --coach-file-blue coach_blue.txt \
  --coach-file-red  coach_red.txt  \
  --thoughts-height 20 \
  --render
# → prints: "run directory: runs/20260412T143022_02_basic_mirror"
# →         "  tail thoughts with: less +F runs/.../thoughts.log"
```

```bash
# Terminal 2 — follow the reasoning log live.
less +F --follow-name runs/20260412T143022_02_basic_mirror/thoughts.log
```

```bash
# Terminal 3 — drop coach messages into the mix as the match unfolds.
echo "focus the mage first" >> coach_blue.txt
echo "don't chase — hold the fort" >> coach_red.txt
```

When the match ends you'll see a spinner (`Blue reviewing the match…`,
`Red reviewing the match…`) while each agent writes its lesson. Lesson
files print once saved:

```
[blue] lesson saved: lessons/02_basic_mirror/commit-mage-to-the-fort.md
[red]  lesson saved: lessons/02_basic_mirror/baiting-cavalry-into-forest.md
```

### 2. Replay that match as a human

```bash
# Interactive step-through (keys: Enter/k=next, j=prev, s=skip, q=quit).
uv run clash-play runs/20260412T143022_02_basic_mirror
```

### 3. Iterate on prompts without polluting the lessons corpus

```bash
uv run clash-match --game 01_tiny_skirmish \
  --blue claude-haiku-4-5 --red claude-haiku-4-5 \
  --no-lessons --render
```

`--no-lessons` disables both writing new lessons and injecting priors, so
you're evaluating the base prompt only.

### 4. Use a curated lessons set for a tournament

```bash
# Put hand-picked lessons in ./canon-lessons/<scenario>/...
uv run clash-match --game 02_basic_mirror \
  --blue claude-sonnet-4-6 --red claude-sonnet-4-6 \
  --lessons-dir ./canon-lessons --render
```

### 5. Inspect the raw replay

The replay is plain JSONL — grep and jq work on it directly:

```bash
# All thoughts from the losing side of the last match.
jq -c 'select(.kind == "agent_thought")' \
  runs/20260412T143022_02_basic_mirror/replay.jsonl

# All seizes across every run.
grep -l '"reason": "seize"' runs/*/replay.jsonl
```
