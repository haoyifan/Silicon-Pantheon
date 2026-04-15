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

- **Damage per hit**: `max(1, attacker.ATK − mitigation)`.
  `mitigation` = defender.DEF + defender_tile.defense_bonus (physical) or
  defender.RES + defender_tile.res_bonus (magic, unit flagged `is_magic`).
  The *defender's* tile bonus always applies, including on counter-attacks
  (so when the defender counters, the attacker's terrain protects the
  attacker's hp pool).
- **Doubling**: if attacker.SPD ≥ defender.SPD + 3, the attack lands twice
  (2 × damage_per_hit). Applies to counter-attacks using defender.SPD
  vs attacker.SPD.
- **Counter-attack**: the defender counters IFF (a) the attacker's post-move
  tile is within the defender's attack range AND (b) the defender survives
  the incoming salvo. Use `simulate_attack` to see the exact outcome before
  committing.
- **Fort heal**: a unit standing on a friendly fort at the start of its
  team's turn regenerates +3 HP (capped at hp_max). Ferrying a wounded
  unit onto your own fort for one rotation is a standard recovery play.
- **Determinism**: combat has no RNG. `simulate_attack` is authoritative.
- **Max turns**: {max_turns}. Each side gets {max_turns} half-turns; after
  both sides act on turn {max_turns} with no win-condition fired, the match
  ends in a draw.
- **Fog of war**: mode `{fog_mode}`. "none" = full visibility. "classic" =
  enemies outside your sight radius are hidden, tiles ever-seen stay
  revealed with last-known terrain. "line_of_sight" = only currently
  visible tiles/enemies. Dead enemies remain visible regardless of fog —
  they're a known historical record.

## Your turn

Each of your units has a status:
- `ready` — can move AND then act (attack/heal/wait), OR skip the move and
  act immediately.
- `moved` — already moved this turn; *must still act* (attack/heal/wait)
  before you can `end_turn`. Trying to `end_turn` with a `moved` unit that
  hasn't acted returns an error "unit X moved but has not acted".
- `done` — finished for this turn.

Units in the `units` array with `alive: false` are casualties from earlier
turns — ignore them for planning, they're in the list so you can reconstruct
history but they can't be moved, attacked, healed, or countered.

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
     Requires it to be your turn and the unit to be yours.
   - `simulate_attack(attacker_id, target_id, from_tile?)` PREDICTS the
     outcome but does NOT change the board. Its response is clearly
     marked (`kind: "prediction"`, `predicted_*` fields). If you want
     the damage to actually happen you MUST follow it with `attack`.
     Never reason as if a simulation already killed the target.
     Pass `from_tile` to preview from a hypothetical post-move
     position.
   - `move(unit_id, dest)` then `attack(unit_id, target_id)` / `heal(...)` /
     `wait(unit_id)`, or skip the move and act directly from `ready`, or
     just `wait` if the unit has nothing useful to do.
   - `heal` requires a unit with `can_heal: true` in its class spec (not
     just "mage" — any can_heal class). Target must be an adjacent
     (Manhattan distance 1) friendly unit that is not the healer itself.
4. Before you call `end_turn`, every unit with status `moved` must have
   acted (attack/heal/wait). A unit that only moved and then you try to
   end the turn will reject the end_turn with an "moved but has not acted"
   error — send `wait(unit_id)` on it if you want it to hold.
5. When all desired units have acted, call `end_turn`. **You MUST call
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
                # Heal amount is load-bearing for the agent's planning —
                # without it they know "this unit can heal" but have to
                # spend an action to discover the per-use amount.
                amt = spec.get("heal_amount")
                flags.append(
                    f"can_heal (+{amt}/use)" if amt else "can_heal"
                )
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
    # fog_of_war is declared in scenario rules ("none" | "classic" |
    # "line_of_sight"). The room host can override it at room-config
    # time; the server-side session is what ultimately applies, so
    # this is just the default the scenario shipped with. Clients
    # that pass a scenario_description bundle should include the
    # room's effective mode once fog-override becomes room-config-
    # time. Missing = treat as the scenario's declared default.
    rules = scenario_description.get("rules") or {}
    fog_mode = str(rules.get("fog_of_war") or "none")
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
        fog_mode=fog_mode,
    )


TURN_PROMPT_TEMPLATE_BOOTSTRAP = """It is turn {turn} and it is your ({team}) turn to play.

This is your first turn, so here is the full state snapshot. Subsequent
turns will only include what changed (opponent actions, your unit
status) — call `get_state` any time you need the full picture.

```json
{state_json}
```

Play your turn. Remember to call end_turn at the end."""


TURN_PROMPT_TEMPLATE_DELTA = """It is turn {turn} and it is your ({team}) turn to play.

{opponent_actions_section}\
{your_units_section}\
Call `get_state` if you need the full board / enemy positions /
fog-of-war map. Remember to call `end_turn` at the end."""


_TURN_PROMPT_MISMATCH_WARNING = """\
WARNING: the per-turn prompt builder was invoked but the snapshot's
active_player does not match you ({team}). The server is authoritative —
every action tool will reject with "not_your_turn" until the opposing
side ends their turn. Do NOT call move/attack/heal/wait/end_turn.
Call get_state once to re-check; if still not your turn, reply with a
short note acknowledging the mismatch and do nothing else.
"""


# Kept as an alias for any existing callers still importing the old
# name — the bootstrap template is the closest match to what they got.
TURN_PROMPT_TEMPLATE = TURN_PROMPT_TEMPLATE_BOOTSTRAP


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


def _format_action_event(ev: dict) -> str:
    """One-line, human-first render of a server action record.

    history entries come from _record_action and have shapes like
    {"type":"move","unit_id":"u_r_speedboat_1","dest":{"x":5,"y":3}}
    or {"type":"attack","unit_id":"a","target_id":"t",
        "damage_dealt":8,"counter_damage":3,"target_killed":False,
        "attacker_killed":False}
    or end_turn variants. Keep this compact — it's read on every
    turn prompt."""
    t = ev.get("type")
    if t == "move":
        dest = ev.get("dest") or {}
        return f"- {ev.get('unit_id')} moved to ({dest.get('x')}, {dest.get('y')})"
    if t == "attack":
        dmg = ev.get("damage_dealt")
        ctr = ev.get("counter_damage")
        killed_bits = []
        if ev.get("target_killed"):
            killed_bits.append(f"{ev.get('target_id')} killed")
        if ev.get("attacker_killed"):
            killed_bits.append(f"{ev.get('unit_id')} killed")
        tail = f" — {', '.join(killed_bits)}" if killed_bits else ""
        return (
            f"- {ev.get('unit_id')} attacked {ev.get('target_id')}: "
            f"damage={dmg}, counter={ctr}{tail}"
        )
    if t == "heal":
        return (
            f"- {ev.get('unit_id')} healed {ev.get('target_id')} "
            f"(+{ev.get('heal_amount')})"
        )
    if t == "wait":
        return f"- {ev.get('unit_id')} waited"
    if t == "end_turn":
        parts = [f"- {ev.get('by') or 'opponent'} ended turn"]
        if ev.get("winner"):
            parts.append(f"WINNER: {ev.get('winner')}")
        if ev.get("reason"):
            parts.append(f"reason={ev.get('reason')}")
        return " | ".join(parts)
    # Fallback — render whatever shape we got as compact json.
    return f"- {json.dumps(ev, default=str)}"


def build_turn_prompt_from_state_dict(
    state_dict: dict,
    viewer: Team,
    *,
    is_first_turn: bool = True,
    new_history: list[dict] | None = None,
) -> str:
    """Per-turn user message for the networked client.

    Two shapes:

      - **Bootstrap** (`is_first_turn=True`): full state snapshot
        so the model's session has a starting mental map.
      - **Delta** (`is_first_turn=False`): only what changed since
        the caller's last turn — enemy actions from `new_history`,
        plus a compact HP/pos/status line per LIVE unit on the
        caller's team. Previously every turn dumped the whole
        state again, which (a) burned tokens duplicating info the
        model already had in-session and (b) made long matches
        hit the context window. The caller (NetworkedAgent) is
        the one that knows "what's changed since last time",
        hence this param.

    If the snapshot's active_player disagrees with `viewer`, a
    warning block is prepended so a misfired call to this function
    can't silently lie to the model.
    """
    team = viewer.value

    if is_first_turn:
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
        prompt = TURN_PROMPT_TEMPLATE_BOOTSTRAP.format(
            turn=state_dict.get("turn", "?"),
            team=team,
            state_json=json.dumps(snapshot, indent=2),
        )
    else:
        events = new_history or []
        if events:
            opponent_actions_section = (
                "Opponent actions since your last turn:\n"
                + "\n".join(_format_action_event(e) for e in events)
                + "\n\n"
            )
        else:
            opponent_actions_section = (
                "Opponent did not act since your last turn.\n\n"
            )

        # Compact your-own-units block: a single line per live
        # friendly unit with HP / pos / status. Small enough to
        # include every turn; no enemy data (use get_state for that).
        own_lines: list[str] = []
        for u in state_dict.get("units", []):
            if u.get("owner") != team:
                continue
            if not u.get("alive", u.get("hp", 0) > 0):
                continue
            pos = u.get("pos") or {}
            own_lines.append(
                f"- {u.get('id')} ({u.get('class')})  "
                f"hp {u.get('hp')}  pos ({pos.get('x')}, {pos.get('y')})  "
                f"status {u.get('status')}"
            )
        your_units_section = (
            "Your units:\n" + "\n".join(own_lines) + "\n\n"
            if own_lines else
            "Your units: (none alive — you may have already lost)\n\n"
        )

        prompt = TURN_PROMPT_TEMPLATE_DELTA.format(
            turn=state_dict.get("turn", "?"),
            team=team,
            opponent_actions_section=opponent_actions_section,
            your_units_section=your_units_section,
        )

    active = state_dict.get("active_player")
    if active is not None and active != team:
        prompt = (
            _TURN_PROMPT_MISMATCH_WARNING.format(team=team)
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
