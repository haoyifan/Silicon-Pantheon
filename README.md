# SiliconPantheon

Tactical grid combat (Fire Emblem-ish) played by AI agents, with humans
participating as coaches. Two agents (Claude, GPT-5, or a random bot) take
turns moving units on a grid to satisfy each scenario's win conditions; a
human watching in the TUI can type advice that the agent sees at the top of
its next turn.

Ships with **12 scenarios** — from `01_tiny_skirmish` through historical
battles (Thermopylae, Cannae, Agincourt, Red Cliffs, Kadesh, ...) and novel
set-pieces (Journey to the West, Helm's Deep, Battle of the Five Armies).
Every scenario is YAML-authored and can carry custom unit classes, terrain
types, win conditions, ASCII portrait art, `rules.py` plugins, and narrative
events.

Read next:
- [`docs/USAGE.md`](docs/USAGE.md) — CLI reference
- [`docs/AUTHORING_SCENARIOS.md`](docs/AUTHORING_SCENARIOS.md) — write your own scenarios
- [`docs/SCENARIOS.md`](docs/SCENARIOS.md) — design notes for the shipped battles
- [`DECISIONS.md`](DECISIONS.md) — log of design calls

---

## Install

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh      # if you don't have uv
uv sync --extra dev
```

Python 3.12+. One of:
- **Claude (default)**: install the `claude` CLI (Claude Code) and `claude login`. No API key needed — uses your subscription.
- **OpenAI**: set `OPENAI_API_KEY` or drop it in `~/.silicon-pantheon/credentials.json`. The first TUI login walks you through it.
- **Random bot** (no LLM): useful for engine smoke tests, no setup.

---

## Run a networked game (server + TUI clients)

The normal way to play. Two terminals per player, one for the server.

**Terminal 1 — the server.** Runs until you Ctrl-C; holds rooms and matches.

```bash
uv run silicon-serve
# → streamable-HTTP MCP on http://127.0.0.1:8080/mcp/
```

**Terminal 2 — player A's client.** First launch walks through LLM provider
selection and (for OpenAI) API-key entry; credentials are cached in
`~/.silicon-pantheon/credentials.json`.

```bash
uv run silicon-join
# → login screen → lobby → create or join a room
```

From the lobby, pick **Create Room** to host. Inside the room you can:
- **Change Scenario** — full-screen picker with map preview, descriptions, win conditions, and per-class stats for every scenario in `games/`.
- **Strategy** — pick a playbook from `strategies/*.md` to inject into your agent's system prompt.
- **Change Fog / Team Mode / Host Team** — all with inline explanations.

**Terminal 3 — player B's client.** Same command; from the lobby pick your
friend's room and click *Ready*. When both players are ready the match
auto-starts.

During play the TUI is a 4-panel grid: **Map** (cursor-navigable with
`←↑↓→`/`hjkl`, `Enter` on a unit opens its stat card with ASCII portrait),
**Player** (turn / team / agent status / unit roster with Unit·HP·Status
table), **Reasoning** (scrollable live agent thought stream), and **Coach**
(Tab in, type, Enter to send — your agent reads it at the start of its next
turn). Press `F2` anywhere for help, `q` to quit (with confirmation).

### Networked without a partner — watch Claude vs Claude

Start the server, then open two `silicon-join` windows on the same machine.
Name them `A` and `B`, create+join a room, and hit ready in both. You now
have a spectating-friendly two-headed match.

---

## Run a quick local match (no server, no TUI)

Useful for verifying the engine or smoke-testing a new scenario.

```bash
# random vs random — instant, no LLM cost
uv run silicon-match --game 01_tiny_skirmish --blue random --red random --render --seed 42

# claude vs claude with named strategies
uv run silicon-match \
  --game 02_basic_mirror \
  --blue claude-sonnet-4-6 --blue-strategy strategies/aggressive_rush.md \
  --red  claude-sonnet-4-6 --red-strategy  strategies/defensive_chokepoint.md \
  --replay replays/my_match.jsonl \
  --render
```

Cost note: ~15k tokens/turn on Sonnet; a 30-turn match is ~1M tokens. Use
`claude-haiku-4-5` or a random player for cheap iteration.

### Coaching in `silicon-match`

```bash
# Terminal 1 — start the match with a coach file
uv run silicon-match --game 02_basic_mirror \
  --blue claude-sonnet-4-6 --red claude-sonnet-4-6 \
  --coach-file-blue coach_blue.txt

# Terminal 2 — append advice any time; agent picks it up next turn
echo "push the cavalry on the right flank" >> coach_blue.txt
```

---

## Run a tournament

```bash
uv run python -m silicon_pantheon.match.tournament \
  --game 02_basic_mirror \
  --a claude-sonnet-4-6 \
  --b claude-haiku-4-5 \
  --rounds 6 --cooldown 30
```

Colors swap each round to remove first-player advantage.

---

## Replay a match

Every match writes a `replay.jsonl`. Scrub through it:

```bash
uv run silicon-play replays/my_match.jsonl
```

---

## Layout

```
src/silicon_pantheon/
├── server/               MCP server + game engine
│   ├── engine/           pure game logic (no MCP) — state, rules, combat,
│   │                     serialize, scenarios, narrative, win_conditions
│   ├── tools/            in-process tool layer (13 game tools)
│   ├── rooms.py          lobby + room lifecycle
│   ├── lobby_tools.py    set_player_metadata, list/create/join_room,
│   │                     describe_scenario, etc.
│   ├── game_tools.py     in-game tool endpoints
│   ├── main_http.py      silicon-serve entry point (streamable HTTP MCP)
│   └── main.py           silicon-server entry point (stdio MCP — legacy)
├── client/
│   ├── transport.py      ServerClient — streamable-HTTP MCP wrapper
│   ├── agent_bridge.py   NetworkedAgent — drives one team through an LLM
│   ├── credentials.py    ~/.silicon-pantheon/credentials.json store
│   ├── providers/        Anthropic + OpenAI adapters behind a Protocol
│   ├── tui/              Rich-based terminal UI
│   │   ├── app.py        TUIApp + key reader + help overlay
│   │   ├── panels.py     Panel base class + focus/border helpers
│   │   └── screens/      login, provider_auth, lobby, room, game,
│   │                     scenario_picker, post_match
│   └── main.py           silicon-join entry point
├── harness/              in-process match orchestration
│   ├── providers/        random + anthropic + openai adapters
│   └── prompts.py        system + per-turn prompt builders
├── match/                silicon-match + tournament + silicon-play
└── shared/               protocol codes, fog-of-war filter, replay schema
games/                    scenario directories (config.yaml, rules.py, art/)
strategies/               agent playbooks (markdown)
lessons/                  auto-written reflections from past matches
docs/                     AUTHORING_SCENARIOS, SCENARIOS, USAGE, DECISIONS...
tests/                    pytest suite (277 passing)
```

---

## Key features

- **Multi-provider agents.** Anthropic + OpenAI out of the box, plus a
  random-move bot. Provider selection is per-player at room time.
- **Scenario authoring.** YAML scenarios declare custom unit classes
  (stats + ASCII portrait frames + tags), custom terrain (per-class
  overrides, effect plugins), declarative win conditions (7 built-in
  rule types + plugin escape hatch), narrative events
  (`on_turn_start` / `on_unit_killed`), and optional `rules.py` plugins
  for board mutation / reinforcement spawning / scripted events.
- **Live coaching.** Humans can push messages to their agent's queue
  via the TUI Coach panel or a watched text file.
- **Agent context discipline.** Scenario invariants (classes, terrain,
  win conditions, starting map) ship once in the system prompt; per-turn
  prompts carry only turn-dynamic state; `describe_class` tool lets the
  agent re-check invariants without bloating every round trip.
- **Fog of war**, client-cached scenario bundles, animated portraits in
  unit cards, stable-shape modals with option descriptions, strategy
  picker from `strategies/`, F2 help overlay that never blocks gameplay,
  and a lessons store that writes per-match reflections the next agent
  can read.
