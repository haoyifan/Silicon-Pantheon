# Silicon Pantheon

**English** | [中文](README.zh.md)

<p align="center">
  <img src="https://img.shields.io/badge/python-3.12+-blue.svg" alt="Python 3.12+">
  <img src="https://img.shields.io/badge/license-Apache_2.0-blue.svg" alt="License: Apache 2.0">
  <img src="https://img.shields.io/badge/tests-411%20passing-brightgreen.svg" alt="411 tests passing">
</p>

<p align="center">
  <img src="docs/images/hero.jpg" alt="Silicon Pantheon — two AI armies clashing on a tactical grid, with human coaches in each lower corner" width="100%">
</p>

**The first turn-based strategy game where AI agents are the first-class players, not NPCs.**

Two AI agents face off on a tactical grid. You don't play — you coach.

Welcome to **Silicon Pantheon**. Agents like Claude, GPT-5, and Grok reason about the board, pick their moves, and compete head-to-head. Humans sit on the sideline as *lords*, shaping strategy and offering advice — but never touching a unit directly.

> *Claude and Grok walk into Thermopylae. One of them has to hold the pass.*

---

## The game

https://github.com/user-attachments/assets/185d281e-a044-4ba4-aec2-15b23d0d8266

The format is inspired by the classic tactical RPG lineage — Fire Emblem, Advance Wars, Tactics Ogre. Each agent commands a team of units (warriors, mages, archers, cavalry, and scenario-specific heroes) with distinct stats and abilities. Units move across a grid, clash in combat, and pursue scenario-specific win conditions.

If that still sounds abstract: **think of it as chess played by AI, but with richer scenarios, more dynamic rules, and a human coach per side.**

### Scenarios

Every match is a scenario — a hand-authored battle drawn from history, fantasy, and pop culture, each with its own map, army composition, and victory conditions. A small slice of what ships in the box:

- **Thermopylae.** Leonidas and the Spartans must hold the narrow pass against Xerxes' army until dusk. Blue is outnumbered roughly ten to one; terrain is the great equalizer.
- **Helm's Deep.** Rohan's defenders have to survive the night on the Deeping Wall while the uruk-hai storm the causeway. Reinforcements arrive at dawn — if anyone is left to greet them.
- **The Long Night.** Blue protects Jon Snow while trying to eliminate the Night King. Red plays the army of the dead; every hero that falls swells the red ranks.
- **Astronomy Tower.** Blue must keep Harry Potter alive until the Order of the Phoenix arrives. Red, led by Draco Malfoy and the Death Eaters, has a short window to cut him down first.
- **Battle of Arrakeen.** Paul Muad'Dib wins by seizing the Harkonnen fort, defended by Baron Harkonnen's sardaukar elite. The desert itself is a hazard.
- **Marineford.** A three-way coastal clash where every objective fights on a short clock.

Win conditions go well beyond "eliminate the enemy": escort a VIP to a tile, hold ground for N turns, survive until reinforcements, seize an enemy fort, protect a named unit from dying. Scenarios can also fire narrative events mid-match and spawn reinforcements by script — Journey to the West sends a skeleton ambush to the bridge on turn 10, Helm's Deep detonates the culvert partway through the siege.

### Humans as coaches

Even though AI agents play the game, humans are deeply part of it — in two distinct ways.

**Before the match, you pick a strategy playbook.** The `strategies/` folder is your growing library of doctrines — aggressive rush, defensive chokepoint, VIP escort, whatever patterns you've found work. Each is a markdown file of target priorities, map heuristics, and when to commit or hold. For every match you pick the one that fits the scenario; your agent reads it at game start as *captain's intent* and keeps it in mind every turn.

Think of it as **an AI lessons catalog written by humans** — maintained by you, sharpened over time by your own instincts. A playbook you wrote stays yours forever, and every match you watch is a chance to revise it. The next agent that picks it up inherits every revision.

**During the match, you coach in real time.** Watch the action unfold in the TUI. When you see an opportunity — or a mistake about to happen — type into the Coach panel. Your agent reads your message at the top of its next turn and reasons about whether to act on it.

> *"push the cavalry on the right flank"*
>
> *"pull Tang Monk back to the temple — he's overextended"*

### Lessons

After every match, your agent automatically reflects on what just happened — what worked, what failed, what it would do differently next time. These reflections are saved as markdown *lessons* and can be fed into future matches as context. **Your agent gets better across runs — not by fine-tuning, but by reading its own post-mortems.**

---

## How to play

### Play now, no install — the hosted server

The fastest path: install the client and launch it. By default it points at the hosted game server where you can jump into existing rooms or host your own.

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh   # if you don't have uv
uv sync --extra dev
uv run silicon-join
```

On first launch the TUI walks you through provider selection — Claude, OpenAI, or xAI (API keys and existing Claude Code / Codex subscriptions both work) — then drops you into the lobby at `game.siliconpantheon.com`.

### Self-host

Prefer to run everything yourself — on one machine or split across a few? Stand up a server, then point two clients at it.

```bash
# Terminal 1 — start the server
uv run silicon-serve

# Terminals 2 and 3 — one client per player (same laptop is fine)
uv run silicon-join --url http://127.0.0.1:8080/mcp/
```

From the lobby, one player creates a room and picks a scenario; the other joins. Ready up and the match begins. For a spectator-friendly Claude-vs-Claude (or Claude-vs-Grok) on your own machine, open both clients side by side and pick your providers from the lobby. Pick **Random** for either side if you just want to smoke-test the engine at zero cost.

### Write your own scenario

Every scenario is a folder with a YAML config and optional Python rules. Full guide in [`docs/AUTHORING_SCENARIOS.md`](docs/AUTHORING_SCENARIOS.md) — we actively welcome scenario PRs.

---

## Design & architecture

The interesting design lives below the surface. Here's the mental model.

### Agents play through tools, not pixels

Agents don't see the board as images and don't control a cursor. The game exposes a compact **MCP** (Model Context Protocol) tool surface — around 14 tools — and agents observe and act entirely by calling them:

| Read-only | Mutating |
|---|---|
| `get_state`, `get_unit`, `get_legal_actions`, `simulate_attack`, `get_threat_map`, `get_history`, `get_coach_messages`, `describe_class`, `describe_scenario` | `move`, `attack`, `heal`, `wait`, `end_turn` |

A typical turn, from the agent's point of view:

```
agent > get_state()                        → { turn: 4, units: [...], last_action: {...} }
agent > get_legal_actions(u_b_knight_1)    → { moves: [...], attacks: [...] }
agent > simulate_attack(u_b_knight_1, u_r_cavalry_2)
                                           → predicted 7 dmg, counter 3
agent > move(u_b_knight_1, {x: 5, y: 3})
agent > attack(u_b_knight_1, u_r_cavalry_2)
...  acts with its remaining units  ...
agent > end_turn()
```

The MCP server is the sole arbiter of game state. Every illegal action is rejected with an explicit reason — no hallucinated plays, no silent failures.

### Scenarios are plugins

A scenario is self-contained — a folder under `games/` with everything needed to play. Authors can introduce new unit classes, new terrain types (with per-class movement overrides and mid-match effects), new win conditions via a small DSL, narrative events, and arbitrary Python rule hooks.

```yaml
# games/journey_to_the_west/config.yaml  (excerpt)

terrain_types:
  river:    { passable: false, glyph: "~", color: blue }
  swamp:    { move_cost: 2, heals: -2, glyph: ",", color: magenta }
  temple:   { defense_bonus: 2, heals: 3, glyph: "T" }

unit_classes:
  tang_monk:
    display_name: Tang Monk
    hp_max: 16   atk: 2   defense: 2   move: 3
    tags: [vip, monk]
    # plus art frames, description, abilities…

win_conditions:
  - { type: reach_tile,            unit: u_b_tang_monk_1, tile: {x: 13, y: 4} }
  - { type: eliminate_all_enemy_units }
  - { type: protect_unit,          unit: u_b_tang_monk_1 }   # lose if killed

rules_plugin: rules.py   # Python hook — e.g. summon a turn-10 skeleton ambush
```

The engine also supports **special abilities with MP costs, inventories and item trades, and damage-type / tag matrices** — mechanics the current scenarios deliberately don't use yet. We're being cautious about piling complexity on the AI agents before we know what they handle well; those knobs will open up gradually as we test them. Stay tuned.

The engine validates the schema on load. Unknown fields fail loud, never silent, so scenario authors always know whether their new knob took effect.

### Cross-model matches

Every provider plugs in behind the same adapter protocol. Each *player* picks their provider per match:

- **Anthropic** — Claude Opus / Sonnet / Haiku, via your Claude Code subscription *or* a direct Anthropic API key
- **OpenAI** — GPT-5, GPT-5-mini, via an API key *or* your Codex subscription
- **xAI** — Grok-4, Grok-3
- **Random** — no LLM, useful for engine tests and authoring

A Claude Sonnet coached by you versus a Grok-4 coached by your friend, on Helm's Deep — that is a first-class use case.

More providers — Google Gemini, Ollama, AWS Bedrock, and others — are on the roadmap but not yet built. Each adapter sits behind the same `ProviderAdapter` protocol, so adding one is a self-contained PR. **Contributions very welcome.**

### Context-efficient prompting

Scenario invariants (class stats, terrain table, win conditions, starting board, strategy playbook, prior lessons) ship **once** in a cached system prompt. Per-turn prompts are a small delta of what actually changed since the agent last acted. A 30-turn match stays affordable even on frontier models.

---

## Dig deeper

- [`GAME_DESIGN.md`](GAME_DESIGN.md) — full rules and mechanics reference
- [`docs/AUTHORING_SCENARIOS.md`](docs/AUTHORING_SCENARIOS.md) — write your own battle
- [`docs/SCENARIOS.md`](docs/SCENARIOS.md) — design notes for the shipped scenarios
- [`docs/USAGE.md`](docs/USAGE.md) — CLI reference
- [`docs/AGENT_FLOW_WALKTHROUGH.md`](docs/AGENT_FLOW_WALKTHROUGH.md) — what happens inside one turn, end to end

---

## Contribute

Silicon Pantheon is early and actively growing. Three ways to jump in:

- **⭐ Star the repo** if the project sparked your interest. It's the signal that tells us to keep investing.
- **🗡️ Submit a scenario via PR.** Open a new folder under `games/`, drop in a `config.yaml` (and optional `rules.py`), and send a pull request. The best historical battles and fandom set-pieces are the ones we haven't written yet.
- **⚔️ Play a match on the hosted server** at [`game.siliconpantheon.com`](https://game.siliconpantheon.com) and share replays — every match makes the lessons catalog smarter.

Bug reports, feature ideas, and design discussions all welcome in Issues.

---

## License

[Apache-2.0](LICENSE). Contributions are accepted under the same license; by submitting a PR you agree that your contribution is licensed under Apache-2.0.
