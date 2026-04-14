# Authoring Scenarios

This guide covers the scenario YAML format, plugin rules, and narrative
events. For the broader design intent, see
[FLEXIBILITY_PROPOSAL.md](FLEXIBILITY_PROPOSAL.md).

## Security & trust model

**Scenarios are operator-trusted.** A scenario's `rules.py` is imported
with full Python privileges — no sandbox. An operator who loads a
scenario from an untrusted source can have arbitrary code executed.
Only install scenarios from sources you trust.

Rationale: this is a local-play tactical game shipped by hobbyists;
sandboxing Python for a few plugin hooks is not worth the complexity
when the alternative is "don't install malware."

---

## Directory layout

Each scenario lives under `games/<name>/`:

```
games/my_scenario/
├── config.yaml          # required
├── rules.py             # optional — plugin callables
└── art/                 # optional — ASCII portraits per unit class
    ├── tang_monk/
    │   ├── 0.txt        # frame 0
    │   └── 1.txt        # frame 1 (animation)
    └── sun_wukong/
        └── 0.txt        # single frame, no animation
```

## ASCII portraits (optional)

Drop `<scenario>/art/<class_slug>/*.txt` files. The loader
auto-discovers them, sorts lexically, validates per-frame size, and
attaches the frames to the unit class. The TUI unit-card modal
renders the portrait above the stats; if a class has more than one
frame, the card cycles through them at **one frame every 2 seconds**.

The feature is fully optional — a scenario can ship art for some
classes, all classes, or none.

### Size limits

By default each frame is capped at **80 columns × 30 rows**. Authors
who want larger pieces can override at the scenario level:

```yaml
art_limits:
  max_cols: 120
  max_rows: 50
```

Files exceeding the cap make `load_scenario` raise — there's no
silent truncation. Pick a size your players' terminals will fit.

### Format

Plain text. Each file is one frame. Trailing newlines are stripped.
Use spaces for alignment (rows can be ragged — Rich handles uneven
widths). Color is applied team-wide (cyan for blue, red for red); art
itself is monochrome.

A two-frame "head bob" or "weapon sway" is the cheapest convincing
animation. See `games/journey_to_the_west/art/` for examples.

## `config.yaml` anatomy

```yaml
schema_version: 1         # optional; engine refuses > its support cap
name: My Scenario
description: |
  Free-form longform description for the lobby screen.

board:
  width: 12
  height: 8
  terrain: [{x: 2, y: 3, type: forest}, ...]
  forts:   [{x: 0, y: 0, owner: blue}, ...]

terrain_types:            # optional — custom terrain
  lava:
    move_cost: 99         # impassable
    passable: false
    glyph: "~"
    color: red
  poison_swamp:
    move_cost: 2
    heals: -2             # negative = end-of-turn damage
    effects_plugin: poison_damage   # plugin function name

unit_classes:             # optional — override built-ins or add new
  sun_wukong:
    hp_max: 45
    atk: 14
    defense: 6
    res: 5
    spd: 8
    move: 5
    tags: [hero, monkey, immortal]
    glyph: S               # one-char map symbol (UPPER blue / lower red)
    color: bright_yellow   # any Rich color name

armies:
  blue:
    - {class: sun_wukong, pos: {x: 1, y: 4}}
  red:
    - {class: knight, pos: {x: 10, y: 4}}

rules:
  max_turns: 30
  first_player: blue

win_conditions:           # optional — defaults to seize/elim/draw
  - {type: protect_unit, unit_id: u_b_tang_monk_1, owning_team: blue}
  - {type: reach_tile,   team: blue, pos: {x: 11, y: 4}}
  - {type: eliminate_all_enemy_units}
  - {type: max_turns_draw}

plugin_hooks:             # optional — scenario-local Python callbacks
  on_turn_start:
    - spawn_wave          # fn name in rules.py

narrative:                # optional — story beats
  title: The Pilgrimage
  description: Tang Monk and his disciples journey west.
  intro: "Day 1. The road stretches on."
  events:
    - {trigger: on_turn_start, turn: 5,  text: "Bandits emerge from the forest."}
    - {trigger: on_turn_start, turn: 10, text: "A storm rolls in."}
    - {trigger: on_unit_killed, unit_id: u_r_boss_1, text: "The demon king falls!"}
```

## Built-in win-condition types

| `type:`                     | Required fields                          |
|-----------------------------|------------------------------------------|
| `seize_enemy_fort`          | —                                        |
| `eliminate_all_enemy_units` | —                                        |
| `max_turns_draw`            | `turns?` (override scenario cap)         |
| `protect_unit`              | `unit_id`, `owning_team`                 |
| `reach_tile`                | `team`, `pos`, `unit_id?`                |
| `hold_tile`                 | `team`, `pos`, `consecutive_turns?`      |
| `reach_goal_line`           | `team`, `axis: x\|y`, `value`            |
| `plugin`                    | `module`, `check_fn`, `kwargs?`          |

## Writing a plugin (`rules.py`)

```python
"""games/my_scenario/rules.py — scenario-local callables."""

from clash_of_odin.server.engine.state import Pos, Team, Unit, UnitStatus
from clash_of_odin.server.engine.units import make_stats
from clash_of_odin.server.engine.state import UnitClass


def poison_damage(state, unit, tile, hook):
    """Terrain effects_plugin. Returns {'hp_delta': -N}."""
    return {"hp_delta": -3}


def spawn_wave(state, turn: int, team: str, **_):
    """on_turn_start plugin_hook. Spawn reinforcements on turn 5."""
    if turn != 5 or team != "red":
        return
    stats = make_stats(UnitClass.KNIGHT)
    state.units["u_r_wave_1"] = Unit(
        id="u_r_wave_1",
        owner=Team.RED,
        class_="knight",
        pos=Pos(3, 0),
        hp=stats.hp_max,
        status=UnitStatus.READY,
        stats=stats,
    )


def custom_win_check(state, hook, **kwargs):
    """Called via win_conditions: [{type: plugin, check_fn: custom_win_check}]."""
    if hook != "end_turn":
        return None
    if some_condition(state):
        return {"winner": "blue", "reason": "custom_victory"}
    return None
```

Name visibility: only module-level names **not** starting with `_`
appear in the plugin namespace.

## Narrative triggers

| `trigger`           | Fires when                           | Matches on              |
|---------------------|--------------------------------------|-------------------------|
| `on_turn_start`     | start of each team's turn            | `turn` (optional)       |
| `on_unit_killed`    | a unit's HP hits 0 in combat         | `unit_id` (optional)    |
| `on_plugin`         | a plugin explicitly fires this hook  | `tag`                   |

Each event fires **at most once per match**. Events render in the TUI
game panel and land in `replay.jsonl` as `kind: narrative_event`.
