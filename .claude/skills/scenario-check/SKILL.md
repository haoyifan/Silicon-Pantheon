---
name: scenario-check
description: Validate scenario configuration files for correctness, consistency, balance, localization, and playability. Run with no arguments to check all scenarios, or pass a scenario name to check one.
argument-hint: "[scenario_name]"
allowed-tools: Bash Read Grep Glob Agent
---

# Scenario Validation Skill

You are an experienced game designer and QA engineer reviewing tactical grid combat scenarios for Silicon Pantheon.

## Target

- If `$ARGUMENTS` is empty or blank: check ALL scenarios in `games/*/config.yaml` (skip `_test_plugin`)
- If `$ARGUMENTS` is a scenario name (e.g., `03_thermopylae`): check only that scenario

## Checks to Perform

For each scenario, read the full `config.yaml` and the `locale/zh.yaml` (if it exists), then run every check below. Report each issue with severity (CRITICAL / HIGH / MEDIUM / LOW / INFO).

### 1. Format & Loading (CRITICAL if broken)
- Run `load_scenario(name)` via Python to verify the config parses and loads without errors:
  ```bash
  .venv/bin/python3 -c "from silicon_pantheon.server.engine.scenarios import load_scenario; s = load_scenario('SCENARIO_NAME'); print(f'{s.board.width}x{s.board.height} {len([u for u in s.units.values() if u.owner.value==\"blue\"])}b+{len([u for u in s.units.values() if u.owner.value==\"red\"])}r')"
  ```
- Every terrain `type` used in board entries must be defined in `terrain_types`
- `move_cost` on impassable terrain must be `99` (not `1` or a string)
- `passable: false` must accompany `move_cost: 99`

### 2. Unit Placement (CRITICAL if broken)
- No unit placed on an impassable tile (cross-reference positions against `passable: false` terrain)
- No unit out of bounds (x must be 0..width-1, y must be 0..height-1)
- No two units on the same tile
- Both teams have at least 3 units
- No unit has ATK 0 or HP 0

### 3. Glyph & ID Consistency
- No two `unit_classes` share the same `glyph` within a scenario
- Win condition `unit_id` values must match actual units (format: `u_{b|r}_{class}_{N}`)
- `reach_tile` positions must be within bounds and NOT on impassable tiles
- `seize_enemy_fort` requires forts to exist
- Fort entries should use `owner` not `team`

### 4. Localization Completeness
- `locale/zh.yaml` must exist
- zh.yaml must have `name` translated
- Every `unit_classes` key in config must appear in zh.yaml with `display_name` and `description`
- Every `terrain_types` key in config must appear in zh.yaml with at least `display_name`
- `narrative.title`, `narrative.description`, `narrative.intro` must be translated
- Every `narrative.events` entry must have translated `text`
- The zh `name` source tag (e.g., "— 权力的游戏") must match the English tag (e.g., "— Game of Thrones")

### 5. Narrative & Description Consistency
- `description` mentions of turn numbers must match `max_turns` and actual reinforcement timing
- `narrative.events` turn numbers must not exceed `max_turns`
- `on_unit_killed` events must reference valid unit IDs
- Plugin hooks mentioned in narrative must exist in `plugin_hooks`
- `first_player` should match the story (attackers go first in assault scenarios)
- `protect_unit_survives` + `max_turns_draw` together: check which one actually fires

### 6. Map Design Quality
- Map should be at least 14x12 (168 tiles) for 10+ units, or 20x14 for 12+ units
- Tile-to-unit ratio should be 10x-25x (not too cramped, not too empty)
- Terrain coverage: at least 20% of tiles should be non-plain
- Check for "dead space" — large rectangular areas (5x5+) with zero terrain features
- At least 2 distinct routing options between the two armies (not just a straight line)
- No "island" units — every unit must be able to reach at least one enemy unit via passable tiles (BFS reachability)

### 7. Unit Balance & Diversity
- No "god-tier" unit: HP > 50 AND ATK > 15 AND DEF > 10 (any one is fine, all three is broken)
- No unit with DEF or RES so high that no enemy can deal more than 1 damage (check: max enemy ATK - unit DEF <= 1 for ALL enemies)
- Role diversity: the scenario should have at least 3 of these 5 roles per team: melee DPS, ranged, tank (DEF >= 7), healer, fast/assassin (SPD >= 7)
- At least one unit per team should have `is_magic: true` OR at least one enemy should have low RES
- Check for absurd terrain bonuses: no terrain with `defense_bonus > 5` or `heals > 10`

### 8. Unit Spread & Engagement Pacing
- Minimum distance between closest blue and red unit should be >= 4 tiles (Manhattan distance)
- Same-team units should not ALL be within a 3x3 box (too clustered)
- Average distance from each unit to the nearest enemy should be >= 5
- With average unit MOVE of 3-4, first contact should be turn 3+ (distance / avg_move >= 3)

### 9. Copyright & Metadata
- `schema_version: 1` present
- `license` field present (one of: `original`, `public_domain`, `fan_work`)
- If `license: fan_work`, `fan_work_source` must be present and non-empty
- `name` should contain a source tag after " — " (era for historical, franchise for fiction)
- `difficulty` should be 1-5

### 10. Plugin Security (if `plugin_hooks` exists)
- Check that referenced plugin names correspond to a `rules.py` file in the scenario directory
- Read the `rules.py` and check for: `import os`, `import subprocess`, `import socket`, `exec(`, `eval(`, `open(` on paths outside the game directory, `__import__`, network calls
- Plugin should only modify game state (spawn units, modify HP, check win conditions)

## Output Format

For each scenario, output:

```
## SCENARIO_NAME — [PASS | ISSUES FOUND]

[If issues found, list each with severity and description]

- CRITICAL: [description]
- HIGH: [description]  
- MEDIUM: [description]
- LOW: [description]
```

At the end, output a summary table:

```
| Scenario | Status | Critical | High | Medium | Low |
```

If fixing is straightforward (e.g., missing locale key, wrong move_cost), fix it directly and note "[FIXED]" next to the issue.
