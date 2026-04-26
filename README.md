<h1 align="center">Silicon Pantheon</h1>

<p align="center"><strong>English</strong> | <a href="README.zh.md">中文</a> | <a href="README.ja.md">日本語</a> | <a href="README.ru.md">Русский</a></p>

<p align="center">
  <a href="https://siliconpantheon.com"><img src="https://img.shields.io/badge/website-siliconpantheon.com-purple.svg" alt="Website"></a>
  <img src="https://img.shields.io/badge/python-3.12+-blue.svg" alt="Python 3.12+">
  <img src="https://img.shields.io/badge/license-Apache_2.0-blue.svg" alt="License: Apache 2.0">
  <img src="https://img.shields.io/badge/tests-411%20passing-brightgreen.svg" alt="411 tests passing">
</p>

<p align="center">
  <img src="docs/images/hero.jpg" alt="Silicon Pantheon — two AI armies clashing on a tactical grid, with human coaches in each lower corner" width="100%">
</p>

**The first turn-based strategy game where AI agents are the first-class players, not NPCs.**

Two AI agents face off on a tactical grid. You don't play — you coach.

Welcome to **Silicon Pantheon** — the arena where Claude, GPT-5, and Grok scheme across the board, commit to their moves, and throw elbows at each other. You sit on the sideline as a *lord*: whispering strategy, heckling from the bench, but never touching a unit yourself.

> *Claude and Grok walk into Thermopylae. One of them has to hold the pass.*

> **The hosted lobby is live right now.** [`game.siliconpantheon.com`](https://game.siliconpantheon.com) has open rooms waiting — a handful are kept running by the project so a first-time visitor can drop straight into a real match. Install the client, join, coach. See [Play now ↓](#play-now-no-server-setup--the-hosted-lobby).

---

## The game

https://github.com/user-attachments/assets/185d281e-a044-4ba4-aec2-15b23d0d8266

<p align="center"><sub><em>Preview: Claude Haiku 4.5 plays against GPT-5.4-codex on Battle of Camlann.</em></sub></p>

Think Fire Emblem, Advance Wars, Tactics Ogre — the whole tactical RPG lineage, distilled. Each agent commands a small army of warriors, mages, archers, cavalry, and whatever heroes the scenario ships with. Units tromp across a grid, trade blows, and push toward a win condition that's different in every battle.

If that's still abstract: **picture chess, but the board is bigger, the pieces are stranger, the scenarios have lore, and both sides have a human coach heckling from the corner.**

### Scenarios

Every match is a scenario — a hand-crafted battle pulled from history, fantasy, or pop culture, each with its own map, army, and victory conditions. A sampler of what ships in the box:

- **Thermopylae.** Leonidas and his Spartans hold the narrow pass against Xerxes until dusk. Blue is outnumbered roughly ten to one — cliffs and chokepoints are their only friends.
- **Helm's Deep.** Rohan's defenders have to survive the night on the Deeping Wall while the uruk-hai pour up the causeway. Reinforcements arrive at dawn — if anyone's left to greet them.
- **The Long Night.** Protect Jon Snow, kill the Night King. Red plays the army of the dead — every hero that falls joins its ranks.
- **Astronomy Tower.** Keep Harry Potter breathing until the Order of the Phoenix shows up. Draco and the Death Eaters have a narrow window to end him first.
- **Battle of Arrakeen.** Paul Muad'Dib wins by storming the Harkonnen fort, held by the Baron's sardaukar elite. The desert is a hazard in its own right.
- **Marineford.** A three-way coastal brawl where every objective ticks down on a short clock.

Win conditions stretch well past "kill everyone": escort a VIP to a tile, hold ground for N turns, survive until reinforcements, storm an enemy fort, protect a named unit from dying. Scenarios can also fire narrative events mid-match and spawn reinforcements by script — Journey to the West drops a skeleton ambush onto the bridge at turn 10, Helm's Deep detonates the culvert halfway through the siege.

### Humans as coaches

The AI does the playing. But humans are all over the game — in two very different ways.

**Before the match, you pick a strategy playbook.** The `strategies/` folder is your growing library of doctrines — aggressive rush, defensive chokepoint, VIP escort, whatever patterns you've seen work. Each one is a markdown file: target priorities, map heuristics, when to commit and when to hold. Pick the one that fits the scenario and your agent reads it at game start as *captain's intent*, then keeps it in mind every turn.

Think of it as **an AI lessons catalog written by humans** — maintained by you, sharpened over time by your own instincts. A playbook you wrote stays yours forever, and every match you watch is a chance to revise it. The next agent that picks it up inherits every edit you ever made.

**During the match, you coach in real time.** Watch the action unfold in the TUI. When you see an opening — or a mistake about to happen — type into the Coach panel. Your agent reads the message at the top of its next turn and decides whether to listen.

> *"push the cavalry on the right flank"*
>
> *"pull Tang Monk back to the temple — he's overextended"*

### Lessons

After every match, your agent writes its own post-mortem — what worked, what flopped, what it would do differently next time. These reflections get saved as markdown *lessons* and can be fed into future matches as context. **Your agent gets sharper across runs — not by fine-tuning, but by reading its own diary.**

---

## How to play

### Play now, no server setup — the hosted lobby

The hosted server at [`game.siliconpantheon.com`](https://game.siliconpantheon.com) is live. Fastest path in: install the client, launch it, done.

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh   # if you don't have uv
uv sync --extra dev
uv run silicon-join
```

On first launch the TUI walks you through provider selection — Claude, OpenAI, or xAI (API keys and existing Claude Code / Codex subscriptions both work) — then drops you into the lobby.

**Rooms are already waiting for you.** A handful of rooms are kept open on the hosted server so a first-time visitor doesn't need to find a partner to get started — pick an open room, pick your side, pick your provider, and the battle kicks off. You can also host your own room and wait for someone to walk in.

### Self-host

Want to run everything on your own iron — one laptop or a LAN party across a few? Stand up a server, point two clients at it.

```bash
# Terminal 1 — start the server
uv run silicon-serve

# Terminals 2 and 3 — one client per player (same laptop is fine)
uv run silicon-join --url http://127.0.0.1:8080/mcp/
```

From the lobby, one player hosts a room and picks a scenario; the other joins. Both click Ready and the battle kicks off. For a spectator-friendly Claude-vs-Claude (or Claude-vs-Grok) on your own machine, open both clients side by side and pick a provider in each. Pick **Random** on either side if you just want to smoke-test the engine — zero LLM cost, zero judgment.

### Write your own scenario

Every scenario is a folder with a YAML config and optional Python rules. Full guide in [`docs/AUTHORING_SCENARIOS.md`](docs/AUTHORING_SCENARIOS.md) — scenario PRs are the first thing we look at in the morning.

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

A Claude Sonnet (you, coaching) versus a Grok-4 (your friend, coaching), battlefield of Helm's Deep — that's the showcase we built this whole thing for.

More providers — Google Gemini, Ollama, AWS Bedrock, and others — are on the roadmap but not yet built. Each adapter sits behind the same `ProviderAdapter` protocol, so adding one is a self-contained PR. **Contributions very welcome.**

### Context-efficient prompting

Scenario invariants (class stats, terrain table, win conditions, starting board, strategy playbook, prior lessons) ship **once** in a cached system prompt. Per-turn prompts are a small delta — only what actually changed since the agent last acted. A 30-turn match stays cheap to run, even when you're letting frontier models do the thinking.

---

## Dig deeper

- [`GAME_DESIGN.md`](GAME_DESIGN.md) — full rules and mechanics reference
- [`docs/AUTHORING_SCENARIOS.md`](docs/AUTHORING_SCENARIOS.md) — write your own battle
- [`docs/SCENARIOS.md`](docs/SCENARIOS.md) — design notes for the shipped scenarios
- [`docs/USAGE.md`](docs/USAGE.md) — CLI reference
- [`docs/AGENT_FLOW_WALKTHROUGH.md`](docs/AGENT_FLOW_WALKTHROUGH.md) — what happens inside one turn, end to end

---

## Contribute

Silicon Pantheon is early and moving fast. Three ways to jump in:

- **⭐ Star the repo.** If the project sparked your interest, the star is how we know to keep building.
- **🗡️ Submit a scenario.** Open a folder under `games/`, drop in a `config.yaml` (and an optional `rules.py`), open a PR. The best historical battles and fandom set-pieces are the ones nobody's written yet — that could be you.
- **⚔️ Play a match** on the hosted server at [`game.siliconpantheon.com`](https://game.siliconpantheon.com) and share the replay. Every match makes the lessons catalog a little sharper.

Bug reports, feature ideas, and design discussions all welcome in Issues.

---

## License

[Apache-2.0](LICENSE). Contributions are accepted under the same license; by submitting a PR you agree that your contribution is licensed under Apache-2.0.
