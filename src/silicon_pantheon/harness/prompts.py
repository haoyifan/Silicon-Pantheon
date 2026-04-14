"""System prompt + per-turn prompt builders."""

from __future__ import annotations

import json
from pathlib import Path

from silicon_pantheon.lessons import Lesson
from silicon_pantheon.server.engine.serialize import state_to_dict
from silicon_pantheon.server.engine.state import Team
from silicon_pantheon.server.session import Session

SYSTEM_PROMPT_TEMPLATE = """You are an AI player in "SiliconPantheon", a turn-based tactical grid combat game.

You are playing as **{team}**. {scenario_name_line}

{scenario_description}

## Match invariants (this section won't repeat)

The information below does not change during the match. Read it once, refer back
to it as needed. If you ever want to re-fetch it mid-match, call `describe_class`
on any unit's class slug (e.g. `describe_class(class="tang_monk")`) — the server
always has the authoritative values.

### Win conditions

{win_conditions}

### Classes in play

{class_catalog}

### Terrain

{terrain_catalog}

### Starting map (turn 1)

{map_grid}

## Universal combat rules

- **Damage**: max(1, attacker.ATK − defender.DEF) for physical attacks,
  max(1, attacker.ATK − defender.RES) for magic attacks (flagged `is_magic`).
  Terrain bonuses apply to the defender.
- **Doubling**: if attacker.SPD ≥ defender.SPD + 3, the attacker hits twice per
  attack. Doubling applies to counter-attacks too.
- **Counter**: the defender counter-attacks if (and only if) the attacker is
  within the defender's attack range.
- **Determinism**: combat has no RNG. `simulate_attack` returns exact outcomes.
- **Max turns**: {max_turns}; reaching that is a draw.

## Your turn

Each of your units has a status: `ready` (can move AND act), `moved` (moved,
still can attack/heal/wait), or `done` (finished). You must issue actions for
the units you want to use, then call `end_turn` to pass control.

## How to play

1. Call `get_coach_messages` at the start of your turn — a human coach may have
   left strategic advice.
2. The per-turn user message you're about to receive contains only **dynamic
   state** (turn number, unit positions, HP, status, last action). Everything
   else — class stats, terrain effects, win conditions — is in this system
   prompt above. You do not need to call `get_state` unless something changed
   unexpectedly (e.g. a plugin mutated the board).
3. For each ready unit, decide what to do:
   - `get_legal_actions(unit_id)` shows moves / attacks / heals available.
   - `simulate_attack(attacker_id, target_id)` predicts the outcome.
   - `move(unit_id, dest)` then `attack(unit_id, target_id)` / `heal(...)` /
     `wait(unit_id)`, or just `wait` if the unit has nothing to do.
4. When all desired units have acted, call `end_turn`. **You MUST call
   `end_turn`** — the game will not advance otherwise.

### Tactical priors

Prefer attacks that kill without dying to counter. Control key terrain (forests,
forts) for defensive bonuses. Range matters — a 2–3 ranged unit can attack
without being countered by a melee unit. A VIP in the `protect_unit` win
condition is the highest priority to protect or kill depending on which side
it belongs to.

{strategy_section}
{lessons_section}
When you're done with your turn, call `end_turn` and stop issuing tool calls."""


STRATEGY_SECTION_TEMPLATE = """## Your coach's strategy playbook

The following is your coach's intent for this match. Treat it as guidance, not
law — deviate when the tactical situation demands it:

----
{strategy}
----
"""


LESSONS_SECTION_HEADER = """## Prior lessons from this scenario

These are reflections written by agents who played this scenario before you
(including past games you lost). Internalize the tactical principles — do not
just replay past moves.

"""


def _format_lessons(lessons: list[Lesson]) -> str:
    if not lessons:
        return ""
    parts = [LESSONS_SECTION_HEADER]
    for le in lessons:
        outcome_tag = f"[{le.team} {le.outcome}]"
        parts.append(f"### {le.title} {outcome_tag}\n\n{le.body.strip()}\n")
    return "\n".join(parts) + "\n"


def _format_win_conditions(
    win_conditions: list[dict], scenario_description: dict | None
) -> str:
    """Prose list of win rules side-explicitly. Reuses the same
    translator the TUI uses so the agent and the human see the same
    wording."""
    # Local import avoids pulling the TUI stack into server-side code
    # that doesn't need Rich.
    from silicon_pantheon.client.tui.screens.room import _describe_win_condition

    if not win_conditions:
        return "(scenario did not declare any — defaults: seize fort / eliminate / draw at turn cap)"
    lines = [
        f"- {_describe_win_condition(wc, scenario_description)}"
        for wc in win_conditions
    ]
    return "\n".join(lines)


def _format_class_catalog(
    armies: dict, unit_classes: dict
) -> str:
    """One block per team listing every fielded class with its stats
    and description. The agent reads this once and refers back; no
    per-turn re-send."""
    if not armies:
        return "(no armies data)"
    parts: list[str] = []
    for team_name in ("blue", "red"):
        in_play: list[str] = []
        seen: set[str] = set()
        for u in armies.get(team_name) or []:
            c = u.get("class")
            if c and c not in seen:
                in_play.append(c)
                seen.add(c)
        if not in_play:
            continue
        parts.append(f"**{team_name.upper()}**")
        for slug in in_play:
            spec = unit_classes.get(slug) or {}
            name = spec.get("display_name") or slug
            line = (
                f"  {name}  "
                f"HP {spec.get('hp_max', '?')}  "
                f"ATK {spec.get('atk', '?')}  "
                f"DEF {spec.get('defense', spec.get('def', '?'))}  "
                f"RES {spec.get('res', '?')}  "
                f"SPD {spec.get('spd', '?')}  "
                f"MOVE {spec.get('move', '?')}  "
                f"RNG {spec.get('rng_min', '?')}-{spec.get('rng_max', '?')}"
            )
            flags: list[str] = []
            if spec.get("is_magic"):
                flags.append("magic")
            if spec.get("can_heal"):
                flags.append("can_heal")
            # Terrain restrictions (defaults: forest=True, mountain=False).
            if spec.get("can_enter_forest") is False:
                flags.append("no forest")
            if spec.get("can_enter_mountain") is True:
                flags.append("can enter mountain")
            if flags:
                line += f"  ({', '.join(flags)})"
            parts.append(line)
            desc = (spec.get("description") or "").strip()
            if desc:
                # Indent description one level beneath the stat line.
                for dline in desc.splitlines():
                    parts.append(f"    {dline.strip()}")
            # Slug — the value the agent types if it calls describe_class.
            parts.append(f"    slug: `{slug}`")
        parts.append("")
    return "\n".join(parts).rstrip()


def _format_terrain_catalog(terrain_types: dict) -> str:
    """Terrain table with effect summary per type."""
    if not terrain_types:
        return "(plain ground everywhere — no custom terrain effects)"
    lines: list[str] = []
    for name, spec in terrain_types.items():
        mc = spec.get("move_cost")
        db = spec.get("defense_bonus") or 0
        rb = spec.get("res_bonus") or 0
        heals = spec.get("heals", 0)
        blocks = spec.get("blocks_sight", False)
        passable = spec.get("passable", True)
        glyph = spec.get("glyph") or _default_terrain_glyph(name)
        parts: list[str] = []
        if mc is not None:
            parts.append(f"move {mc}")
        else:
            parts.append("move 1")
        if db:
            parts.append(f"+{db} DEF")
        if rb:
            parts.append(f"+{rb} RES")
        if heals:
            sign = "+" if heals > 0 else ""
            parts.append(f"{sign}{heals} HP/turn")
        if blocks:
            parts.append("blocks LoS")
        if passable is False:
            parts.append("impassable")
        desc = spec.get("description") or ""
        summary = ", ".join(parts)
        line = f"  {glyph}  {name:<14} {summary}"
        if desc:
            line += f"  — {desc.strip()}"
        lines.append(line)
    return "\n".join(lines)


def _default_terrain_glyph(name: str) -> str:
    return {"plain": ".", "forest": "f", "mountain": "^", "fort": "*"}.get(
        name, name[:1] or "?"
    )


def _format_map_grid(
    board: dict, tiles_by_pos: dict[tuple[int, int], str], forts: list[dict],
    terrain_types: dict, armies: dict, unit_classes: dict,
) -> str:
    """ASCII representation of the starting board: terrain + forts +
    initial unit positions. Classes use their scenario glyph; empty
    plain cells render as '.'."""
    w = int(board.get("width", 0))
    h = int(board.get("height", 0))
    if w == 0 or h == 0:
        return "(empty board)"

    # Fort positions.
    fort_pos: dict[tuple[int, int], str] = {}
    for f in forts or []:
        pos = f.get("pos") or {}
        fort_pos[(int(pos.get("x", -1)), int(pos.get("y", -1)))] = f.get("owner", "?")

    # Initial unit positions.
    unit_pos: dict[tuple[int, int], tuple[str, str]] = {}
    for team, army in (armies or {}).items():
        for u in army or []:
            pos = u.get("pos") or {}
            spec = unit_classes.get(u.get("class")) or {}
            g = spec.get("glyph") or (u.get("class", "?")[:1] or "?")
            g = g.upper() if team == "blue" else g.lower()
            unit_pos[(int(pos.get("x", -1)), int(pos.get("y", -1)))] = (g, team)

    rows: list[str] = []
    # X-axis header.
    header = "     " + " ".join(f"{x:>2}" for x in range(w))
    rows.append(header)
    for y in range(h):
        cells: list[str] = [f"{y:>3}  "]
        for x in range(w):
            if (x, y) in unit_pos:
                g, _ = unit_pos[(x, y)]
                cells.append(f"{g:>2}")
                continue
            if (x, y) in fort_pos:
                cells.append(" *")
                continue
            ttype = tiles_by_pos.get((x, y), "plain")
            spec = terrain_types.get(ttype) or {}
            glyph = spec.get("glyph") or _default_terrain_glyph(ttype)
            cells.append(f" {glyph[:1]}")
        rows.append(" ".join(cells))
    return "\n".join(rows)


def build_system_prompt(
    team: Team,
    max_turns: int,
    strategy: str | None,
    lessons: list[Lesson] | None = None,
    scenario_description: dict | None = None,
) -> str:
    """Build the per-session system prompt.

    The scenario bundle (from describe_scenario) carries the
    invariants — classes, terrain, win conditions, starting map.
    Everything scenario-specific lives in this prompt. The per-turn
    user messages only need dynamic state.
    """
    strategy_section = ""
    if strategy:
        strategy_section = STRATEGY_SECTION_TEMPLATE.format(strategy=strategy.strip())
    lessons_section = _format_lessons(lessons or [])

    scenario_description = scenario_description or {}
    name = scenario_description.get("name") or "(unknown scenario)"
    story = (scenario_description.get("description") or "").strip()
    armies = scenario_description.get("armies") or {}
    unit_classes = scenario_description.get("unit_classes") or {}
    terrain_types = scenario_description.get("terrain_types") or {}
    board = scenario_description.get("board") or {}
    tiles_by_pos: dict[tuple[int, int], str] = {}
    for t in board.get("terrain") or []:
        tiles_by_pos[(int(t.get("x", -1)), int(t.get("y", -1)))] = str(t.get("type", "plain"))

    scenario_name_line = f"The scenario is **{name}**."
    scenario_desc_block = (
        f"> {story}\n" if story else "(no scenario description provided)\n"
    )
    win_conds = _format_win_conditions(
        scenario_description.get("win_conditions") or [], scenario_description
    )
    class_catalog = _format_class_catalog(armies, unit_classes)
    terrain_catalog = _format_terrain_catalog(terrain_types)
    map_grid = _format_map_grid(
        board, tiles_by_pos, board.get("forts") or [],
        terrain_types, armies, unit_classes,
    )

    return SYSTEM_PROMPT_TEMPLATE.format(
        team=team.value,
        max_turns=max_turns,
        scenario_name_line=scenario_name_line,
        scenario_description=scenario_desc_block,
        win_conditions=win_conds,
        class_catalog=class_catalog,
        terrain_catalog=terrain_catalog,
        map_grid=map_grid,
        strategy_section=strategy_section,
        lessons_section=lessons_section,
    )


TURN_PROMPT_TEMPLATE = """It is turn {turn} and it is your ({team}) turn to play.

Here is a snapshot of the current game state (you can always call get_state to refresh):

```json
{state_json}
```

Play your turn. Remember to call end_turn at the end."""


_TURN_PROMPT_MISMATCH_WARNING = """\
WARNING: the per-turn prompt builder was invoked but the snapshot's
active_player does not match you ({team}). The server is authoritative —
every action tool will reject with "not_your_turn" until the opposing
side ends their turn. Do NOT call move/attack/heal/wait/end_turn.
Call get_state once to re-check; if still not your turn, reply with a
short note acknowledging the mismatch and do nothing else.
"""


def build_turn_prompt(session: Session, viewer: Team) -> str:
    state_dict = state_to_dict(session.state, viewer=viewer)
    # Strip the verbose terrain grid from the snapshot — it's constant across turns.
    # Keep forts and board dimensions.
    snapshot = {
        "turn": state_dict["turn"],
        "active_player": state_dict["active_player"],
        "you": state_dict["you"],
        "board": {
            "width": state_dict["board"]["width"],
            "height": state_dict["board"]["height"],
            "forts": state_dict["board"]["forts"],
        },
        "units": state_dict["units"],
        "last_action": state_dict["last_action"],
    }
    return TURN_PROMPT_TEMPLATE.format(
        turn=session.state.turn,
        team=viewer.value,
        state_json=json.dumps(snapshot, indent=2),
    )


_AGENT_UNIT_KEYS = (
    # Only fields that change during a match. Class invariants
    # (atk, def, res, spd, move, rng, is_magic, can_heal, hp_max)
    # live in the system prompt's class catalog — the agent can
    # look them up there or via describe_class. Keeping them per-
    # turn duplicates information on every call.
    "id", "owner", "class", "pos", "hp", "status", "alive",
)


def _slim_unit(u: dict) -> dict:
    """Strip the per-unit dict down to TURN-DYNAMIC fields only.

    Drops ASCII art, class descriptions, reserved-v2 metadata
    (tags, MP, abilities, damage_profile, etc.) AND class-invariant
    combat stats — the agent has those in its system prompt catalog
    and can call describe_class to re-check. Per-turn unit entries
    are now ~7 keys instead of ~30; per-turn JTTW prompt drops from
    ~21 KB (pre-slim) to roughly 1.5 KB."""
    return {k: u.get(k) for k in _AGENT_UNIT_KEYS if k in u}


def build_turn_prompt_from_state_dict(
    state_dict: dict, viewer: Team
) -> str:
    """Same as build_turn_prompt but takes a pre-filtered state dict.

    Used by the networked client which receives the state via a
    `get_state` tool call rather than holding a local Session. The
    dict should already be filtered for `viewer` by the server's
    viewer-filter layer.

    If the snapshot's active_player disagrees with `viewer`, a
    warning block is prepended so a misfired call to this function
    can't silently lie to the model (see agent_bridge.play_turn
    which guards against the same race earlier in the pipeline).
    """
    snapshot = {
        "turn": state_dict.get("turn"),
        "active_player": state_dict.get("active_player"),
        "you": state_dict.get("you"),
        "board": {
            "width": state_dict.get("board", {}).get("width"),
            "height": state_dict.get("board", {}).get("height"),
            "forts": state_dict.get("board", {}).get("forts"),
        },
        "units": [_slim_unit(u) for u in state_dict.get("units", [])],
        "last_action": state_dict.get("last_action"),
    }
    prompt = TURN_PROMPT_TEMPLATE.format(
        turn=state_dict.get("turn", "?"),
        team=viewer.value,
        state_json=json.dumps(snapshot, indent=2),
    )
    active = state_dict.get("active_player")
    if active is not None and active != viewer.value:
        prompt = (
            _TURN_PROMPT_MISMATCH_WARNING.format(team=viewer.value)
            + "\n"
            + prompt
        )
    return prompt


def load_strategy(path: str | Path | None) -> str | None:
    if path is None:
        return None
    p = Path(path)
    if not p.exists():
        return None
    return p.read_text(encoding="utf-8").strip()
