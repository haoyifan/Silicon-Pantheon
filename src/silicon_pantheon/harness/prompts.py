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
{fog_section}

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

1. The per-turn user message you receive at turn-start automatically
   includes any strategic advice your human coach left for you (under
   a "📢 Coach messages" section, when present). Read it FIRST — it
   often supersedes your default playbook. You do NOT need to poll
   for coach messages; they are delivered proactively each turn.
2. The per-turn user message contains only **dynamic state** (turn
   number, unit positions, HP, status, last action). Class stats,
   terrain effects, and win conditions are in this system prompt
   above. You do not need to call `get_state` unless something
   changed unexpectedly (e.g. a plugin mutated the board).
3. For each ready unit, decide what to do:
   - `get_legal_actions(unit_id)` shows moves / attacks / heals available
     for one of your units. **Call this whenever you're unsure where a
     unit can go or whom it can hit; do not reason about reachability
     by hand.** (The server runs BFS over the board with exact terrain
     costs, friendly blocking, and impassable tiles; reproducing that
     in your head is a frequent source of "all my units are stuck"
     errors. Friendly units block their own tile but the BFS routes
     around them.)
   - `simulate_attack(attacker_id, target_id, from_tile?)` PREDICTS
     the outcome — `kind: "prediction"`, `predicted_*` fields — but
     does NOT change the board. To actually deal the damage you MUST
     follow up with `attack`. Pass `from_tile` to preview from a
     hypothetical post-move position.
   - `move(unit_id, dest)` then `attack(unit_id, target_id)` /
     `heal(...)` / `wait(unit_id)`, or skip the move and act directly
     from `ready`, or just `wait` if the unit has nothing useful to do.
   - `heal` requires a unit with `can_heal: true` in its class spec
     (not just "mage" — any can_heal class). Target must be an adjacent
     (Manhattan distance 1) friendly unit that is not the healer itself.
4. Before you call `end_turn`, every unit with status `moved` must have
   acted (attack/heal/wait). A `moved` unit left hanging will make
   `end_turn` reject with "moved but has not acted" — send
   `wait(unit_id)` on it if you want it to hold.
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
## Tool call batching rule (IMPORTANT)

The client enforces this contract on every assistant message:

  - **Unlimited READ calls per message.** You can batch as many of
    these as you like in one response: `get_state`, `get_unit`,
    `get_legal_actions`, `simulate_attack`, `get_threat_map`,
    `get_tactical_summary`, `get_history`, `describe_class`.
    Ask 10 things at once, get 10 answers back.

  - **At most ONE mutating call per message.** Only the FIRST of these
    runs per response; any subsequent ones are DROPPED with a
    `dropped_parallel_mutation` error and the game state does NOT
    change for those dropped calls: `move`, `attack`, `heal`, `wait`,
    `end_turn`.

This mirrors how a human plays: observe broadly, commit to one
action, observe the result, repeat. There is NO limit on how many
rounds of messages you may use in a turn — take as many
observe-act-observe rounds as you need.

Good pattern:
  message 1: `get_legal_actions(u1)`, `get_legal_actions(u2)`,
             `simulate_attack(u1, e3)`
  message 2: `attack(u1, e3)`       ← ONE mutation based on above
  message 3: `get_state`, `get_legal_actions(u2)` ← observe after
  message 4: `move(u2, dest)`       ← ONE mutation again
  ...
  final:     `end_turn`

Bad pattern (most of these mutations will be dropped):
  message 1: `move(u1)`, `wait(u1)`, `move(u2)`, `wait(u2)`,
             `move(u3)`, `wait(u3)`, `end_turn`
  → only the first `move(u1)` runs. Everything else is dropped
    and the game state reflects none of it.

When the turn is truly finished, call `end_turn`. Do NOT keep
issuing tool calls after `end_turn` succeeds — the next user
message will tell you when it's your turn again.
{debug_notice}"""


DEBUG_NOTICE_EN = """
## 🐞 You are playtesting a pre-release build

This match is being run with the server/client in **debug mode**.
The game rules, the scenario you're playing, and the tool responses
you receive might have bugs, inconsistencies, or unclear phrasing
that slipped past the authors. We are specifically asking you to
help us find them.

If anything during the match feels OFF — a rule that seems to
contradict itself, a scenario constraint that doesn't match what's
actually on the board, a tool result that disagrees with a prior
one, an enemy unit that vanished without your spotter moving, a win
condition you can't make sense of, narrative text that claims
something the state doesn't show, OR the scenario feels grossly
imbalanced (one side has a structural advantage that makes the
match trivial / unwinnable and it isn't the intended design) —
**do not silently assume you're wrong and the game is right**.
Call the `report_issue` tool:

  `report_issue(category, summary, details=None)`

  - `category` ∈ {"bug", "confusion", "rules_unclear",
    "scenario_issue", "imbalance", "suggestion"}
    - Use ``imbalance`` specifically when the matchup itself feels
      off — e.g. "red has 3× blue's firepower and blue has no
      answer to it", "blue's VIP is one-shot by the enemy's opening
      move with no counterplay". Keep ``scenario_issue`` for
      concrete errors (wrong placement, missing unit, unreachable
      tile); ``imbalance`` is for the shape of the matchup.
  - `summary` — one sentence, <500 chars, what surprised you
  - `details` — optional longer context (quote the relevant state /
    tool result, give turn number and unit ids)

Calling it does NOT end your turn or cost you anything. Keep playing
your best game after you file it; the match continues normally. You
can file multiple reports per turn if you find multiple issues —
each call is independent.

A human will read these reports after the match. Please speak up.
"""

DEBUG_NOTICE_ZH = """
## 🐞 你正在参与测试一个预发布版本

本局比赛的服务器/客户端以**调试模式**运行。游戏规则、你当前的剧本、
甚至你收到的工具返回结果，都可能存在bug、不一致或描述不清之处。
我们希望你帮我们找到它们。

如果比赛中有任何感觉不对的地方——规则自相矛盾、剧本设定与棋盘实际
不符、工具结果与之前的不一致、敌方单位在你没有移走观察者的情况下
消失、胜利条件让你困惑、叙述文本声称的事情在状态中看不到，或者
剧本整体失衡（某一方有让比赛变得毫无悬念的结构性优势，而这不是
原作者的本意）——**不要默认自己错了、游戏对了**。调用`report_issue`工具：

  `report_issue(category, summary, details=None)`

  - `category` ∈ {"bug", "confusion", "rules_unclear",
    "scenario_issue", "imbalance", "suggestion"}
    - 使用`imbalance`专门针对对局整体失衡——例如"红方火力
      是蓝方3倍且蓝方无解"、"蓝方VIP在敌方第一步就被秒杀且
      毫无反制"。`scenario_issue`保留给具体的剧本错误（放置
      错误、缺失单位、无法到达的格子）；`imbalance`是关于
      对局形态的反馈。
  - `summary` —— 一句话，<500字符，令你惊讶的是什么
  - `details` —— 可选的详细上下文（引用相关状态/工具结果，
    给出回合号与单位id）

调用它不会结束你的回合，也不会有任何代价。记录之后继续全力以赴;
比赛正常进行。一回合内发现多个问题可以多次记录，每次独立。

人会在比赛结束后查看这些报告。请说出来。
"""


def _debug_notice(locale: str = "en") -> str:
    """Return the debug-mode testing notice, or empty string in production.

    Gated on ``SILICON_DEBUG`` on the CLIENT / harness process (the
    one building this prompt). Reading the env var on every call
    (via ``is_debug()``) lets operators flip the flag without a
    rebuild — set ``SILICON_DEBUG=1`` before running the harness.
    """
    from silicon_pantheon.shared.debug import is_debug
    if not is_debug():
        return ""
    return DEBUG_NOTICE_ZH if locale == "zh" else DEBUG_NOTICE_EN


def _debug_turn_reminder(locale: str = "en") -> str:
    """One-line reminder appended to every turn prompt in debug mode.

    The system prompt's DEBUG_NOTICE block explains the full
    report_issue contract; this just keeps it top-of-mind each turn
    so a confused agent is reminded to speak up instead of silently
    power-through. Empty string when SILICON_DEBUG is unset, so prod
    prompts are untouched.
    """
    from silicon_pantheon.shared.debug import is_debug
    if not is_debug():
        return ""
    if locale == "zh":
        return (
            "\n\n🐞 （调试模式）本回合如发现规则/剧本/工具返回中有任何"
            "不对劲的地方，请调用 `report_issue(category, summary)` 记录，"
            "然后继续正常游戏。"
        )
    return (
        "\n\n🐞 (debug mode) If anything this turn looks wrong — rules, "
        "scenario, tool results — call `report_issue(category, summary)` "
        "to flag it, then keep playing normally."
    )


FOG_BLOCK_NONE = (
    "- **Fog of war**: mode `none`. Both teams see the entire board and\n"
    "  all units at all times. No sight cones, no hidden enemies."
)

FOG_BLOCK_CLASSIC = (
    "- **Fog of war**: mode `classic`. You only see tiles within the\n"
    "  sight cone (Chebyshev distance, default sight = 3 unless a class\n"
    "  overrides) of at least one of your ALIVE units. Terrain of tiles\n"
    "  you have ever seen stays on the map with its last-known type —\n"
    "  but **enemy units have NO memory**. An enemy is listed in `units`\n"
    "  ONLY if some alive unit of yours can see it RIGHT NOW. If your\n"
    "  last spotter moves away or dies, the enemy drops out of the\n"
    "  `units` array even though it's still there on the server. A\n"
    "  stationary enemy vanishing between turns almost always means\n"
    "  YOU removed its last spotter — it did not teleport; look where\n"
    "  you last saw it. Dead enemies remain visible regardless of fog\n"
    "  (known history). Move results carry `revealed_enemies` (entered\n"
    "  sight because of the move) and `hidden_enemies` (dropped out of\n"
    "  sight because of the move, with `last_known_pos`)."
)

FOG_BLOCK_LOS = (
    "- **Fog of war**: mode `line_of_sight`. You only see tiles within\n"
    "  the sight cone (Chebyshev distance, default sight = 3 unless a\n"
    "  class overrides) of at least one of your ALIVE units, AND only\n"
    "  those tiles' terrain is shown — unlike `classic`, there is no\n"
    "  persistent terrain memory; every turn the map re-masks to\n"
    "  currently-visible tiles only. **Enemy units have no memory\n"
    "  either**: an enemy is listed in `units` ONLY if an alive unit of\n"
    "  yours can see it right now. If your last spotter moves away or\n"
    "  dies, the enemy drops out even though it's still there on the\n"
    "  server. A stationary enemy vanishing between turns almost always\n"
    "  means YOU removed its last spotter — it did not teleport; look\n"
    "  where you last saw it. Dead enemies remain visible regardless of\n"
    "  fog (known history). Move results carry `revealed_enemies`\n"
    "  (entered sight) and `hidden_enemies` (dropped out of sight, with\n"
    "  `last_known_pos`)."
)


def _fog_section(mode: str, locale: str = "en") -> str:
    """Return the active fog mode's rules block.

    Only the rules for the mode actually in play land in the system
    prompt — describing the two unused modes wastes context and can
    confuse the model about which semantics apply. Unknown modes fall
    back to `none` so a misconfigured scenario still boots with a
    sensible block.
    """
    if locale == "zh":
        return {
            "none": FOG_BLOCK_NONE_ZH,
            "classic": FOG_BLOCK_CLASSIC_ZH,
            "line_of_sight": FOG_BLOCK_LOS_ZH,
        }.get(mode, FOG_BLOCK_NONE_ZH)
    return {
        "none": FOG_BLOCK_NONE,
        "classic": FOG_BLOCK_CLASSIC,
        "line_of_sight": FOG_BLOCK_LOS,
    }.get(mode, FOG_BLOCK_NONE)


FOG_BLOCK_NONE_ZH = (
    "- **战争迷雾**: 模式`none`。双方始终看到整张地图和所有单位，\n"
    "  没有视野锥，没有隐藏单位。"
)

FOG_BLOCK_CLASSIC_ZH = (
    "- **战争迷雾**: 模式`classic`。你只能看到至少一个己方存活单位\n"
    "  视野锥内的格子（切比雪夫距离，默认sight=3，除非兵种覆盖）。\n"
    "  曾经见过的格子保留最后一次看到的地形类型——但**敌方单位没有\n"
    "  记忆**。`units`列表只会列出你的某个存活单位当前正能看到的敌人;\n"
    "  如果你最后一个观察者移开或阵亡，即使敌人仍在服务器上，也会从\n"
    "  `units`消失。两回合间一个静止敌人消失，几乎一定是你撤掉了最后\n"
    "  的观察者——它没有瞬移；去它最后出现的位置看看。死亡敌人不受\n"
    "  迷雾影响始终可见（历史记录）。移动结果中会带`revealed_enemies`\n"
    "  （因这次移动进入视野）和`hidden_enemies`（因这次移动离开视野，\n"
    "  附`last_known_pos`）。"
)

FOG_BLOCK_LOS_ZH = (
    "- **战争迷雾**: 模式`line_of_sight`。你只能看到至少一个己方存活\n"
    "  单位视野锥内的格子（切比雪夫距离，默认sight=3）。与`classic`不同,\n"
    "  不保留地形记忆——每回合地图都重新只显示当前可见格子。**敌方\n"
    "  单位同样没有记忆**：`units`只列出当前某个存活己方单位可看见的\n"
    "  敌人。最后一个观察者移开/阵亡后，敌人即使仍在，也会从`units`\n"
    "  消失。两回合间静止敌人消失几乎一定是你撤掉了最后的观察者。\n"
    "  死亡敌人始终可见（历史记录）。移动结果中会带`revealed_enemies`\n"
    "  和`hidden_enemies`（附`last_known_pos`）。"
)


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
    win_conditions: list[dict],
    scenario_description: dict | None,
    locale: str = "en",
) -> str:
    """Prose list of win rules side-explicitly. Reuses the same
    translator the TUI uses so the agent and the human see the same
    wording."""
    # Local import avoids pulling the TUI stack into server-side code
    # that doesn't need Rich.
    from silicon_pantheon.client.tui.scenario_display import (
        describe_win_condition as _describe_win_condition,
        filter_win_conditions as _filter_win_conditions,
    )

    if not win_conditions:
        return "(scenario did not declare any — defaults: seize fort / eliminate / draw at turn cap)"
    lines = [
        f"- {_describe_win_condition(wc, scenario_description, locale=locale)}"
        for wc in _filter_win_conditions(win_conditions)
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
    locale: str = "en",
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
        scenario_description.get("win_conditions") or [],
        scenario_description,
        locale=locale,
    )
    class_catalog = _format_class_catalog(armies, unit_classes)
    terrain_catalog = _format_terrain_catalog(terrain_types)
    map_grid = _format_map_grid(
        board, tiles_by_pos, board.get("forts") or [],
        terrain_types, armies, unit_classes,
    )

    # Select template by locale. Chinese templates are in prompts_zh.py;
    # English (default) uses the template defined above in this file.
    template = SYSTEM_PROMPT_TEMPLATE
    if locale == "zh":
        from silicon_pantheon.harness.prompts_zh import SYSTEM_PROMPT_TEMPLATE_ZH
        template = SYSTEM_PROMPT_TEMPLATE_ZH
        scenario_name_line = f"剧本是**{name}**。"

    return template.format(
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
        fog_section=_fog_section(fog_mode, locale),
        debug_notice=_debug_notice(locale),
    )


_NO_CONSTRAINT_REMINDER = (
    "Reason as fully as you need. There are no token / time penalties "
    "for thinking — you have a 30-minute wall clock per turn. Do NOT "
    "default to mass `wait` + `end_turn` to 'preserve tokens'; that's "
    "a learned shortcut that does not apply here. Plan completely, then act."
)


TURN_PROMPT_TEMPLATE_BOOTSTRAP = f"""It is turn {{turn}} and it is your ({{team}}) turn to play.

{_NO_CONSTRAINT_REMINDER}

This is your first turn, so here is the full state snapshot. Subsequent
turns will only include what changed (opponent actions, your unit
status) — call `get_state` any time you need the full picture.

```json
{{state_json}}
```

{{tactical_section}}\
Play your turn. Remember to call end_turn at the end."""


TURN_PROMPT_TEMPLATE_DELTA = f"""It is turn {{turn}} of {{max_turns}} and it is your ({{team}}) turn to play. \
({{turns_remaining}} turn(s) remaining, including this one, before the turn cap.)

{_NO_CONSTRAINT_REMINDER}

{{opponent_actions_section}}\
{{your_units_section}}\
{{tactical_section}}\
Call `get_state` if you need the full board / enemy positions /
fog-of-war map. Remember to call `end_turn` at the end."""


# Retry / continuation prompt. Fired when the previous play_turn loop
# exited without the model calling end_turn (time budget exhausted,
# token cap hit, repeat-detector tripped, etc.). Crucially, this is
# NOT a new turn — the server still has the same turn N active, the
# same units are half-acted, and the model's own transcript already
# carries everything it did so far.
#
# Shipping the normal TURN_PROMPT_TEMPLATE_DELTA ("It is turn N and it
# is your turn...") on a retry is what caused the 34-coach-messages-
# for-20-turns pattern in the Agincourt log: the model read it as a
# fresh turn and restarted its "step 1: call get_coach_messages"
# routine. This template explicitly frames the retry as a continuation
# so the model picks up where it left off instead of starting over.
TURN_PROMPT_TEMPLATE_RETRY = f"""You did NOT call `end_turn` on turn \
{{turn}} before your last response ended. This is a CONTINUATION of \
the SAME turn {{turn}} — it has NOT restarted. Do NOT re-plan from \
scratch.

{_NO_CONSTRAINT_REMINDER}

Your own tool-call history in this conversation shows which actions \
you already took. Look at it, identify the units that still need to \
act, finish them, and call `end_turn` to pass control to the opponent.

{{your_units_section}}\
{{tactical_section}}"""


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
        parts = [f"- {ev.get('unit_id')} moved to ({dest.get('x')}, {dest.get('y')})"]
        rev = ev.get("revealed_enemies") or []
        if rev:
            ids = ", ".join(
                f"{u.get('id')} ({u.get('class')}) @({u.get('pos', {}).get('x')},"
                f"{u.get('pos', {}).get('y')}) hp={u.get('hp')}/{u.get('hp_max')}"
                for u in rev
            )
            parts.append(f"  · came into sight: {ids}")
        hid = ev.get("hidden_enemies") or []
        if hid:
            ids = ", ".join(
                f"{u.get('id')} ({u.get('class')}, last seen @("
                f"{u.get('last_known_pos', {}).get('x')},"
                f"{u.get('last_known_pos', {}).get('y')}))"
                for u in hid
            )
            parts.append(f"  · dropped out of sight: {ids}")
        return "\n".join(parts)
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


def _build_tactical_section(summary: dict | None) -> str:
    """Render the get_tactical_summary bundle into the turn prompt so
    the agent starts its turn with "what's worth doing right now"
    pre-chewed. Empty if the summary is None or has nothing to say —
    we don't want to pollute the prompt with a header followed by
    three "(none)" lines on a quiet turn."""
    if not summary:
        return ""
    opps = summary.get("opportunities") or []
    threats = summary.get("threats") or []
    pending = summary.get("pending_action") or []
    win_progress = summary.get("win_progress") or []
    coach = summary.get("coach_messages") or []
    if not (opps or threats or pending or win_progress or coach):
        return ""
    lines: list[str] = []
    # Coach messages first — they're human-authored strategic
    # overrides and the agent should weigh them above the algorithmic
    # hints. Each is one entry from the team's coach queue, drained
    # by get_tactical_summary on this call.
    if coach:
        lines.append("📢 Coach messages (read these FIRST — they may override your default plan):")
        for m in coach:
            text = (m.get("text") or "").strip().replace("\n", " ")
            lines.append(f"  - {text}")
        lines.append("")  # spacer before opps
    if opps:
        lines.append("Opportunities this turn (attacks you can execute from current positions):")
        for o in opps:
            kill = " → KILL" if o.get("predicted_defender_dies") else ""
            counter = o.get("predicted_counter_damage") or 0
            own_dies = " (you die to counter)" if o.get("predicted_attacker_dies") else ""
            lines.append(
                f"  - {o['attacker_id']} → {o['target_id']}: "
                f"deal {o.get('predicted_damage_to_defender', 0)} "
                f"(take {counter} counter){kill}{own_dies}"
            )
    if threats:
        if lines:
            lines.append("")
        lines.append("Threats against your units (enemies that can reach your current tiles):")
        for t in threats:
            lines.append(
                f"  - {t['defender_id']} (hp {t.get('defender_hp','?')}/"
                f"{t.get('defender_hp_max','?')}): threatened by "
                f"[{', '.join(t.get('threatened_by') or [])}]"
            )
    if pending:
        if lines:
            lines.append("")
        lines.append(
            "Units still in MOVED status (MUST act before end_turn): "
            f"[{', '.join(pending)}]"
        )
    if win_progress:
        if lines:
            lines.append("")
        lines.append("Win progress (per condition):")
        for w in win_progress:
            lines.append(f"  - {w}")
    return "\n".join(lines) + "\n\n"


def _build_own_units_section(state_dict: dict, team: str) -> str:
    """Compact HP/pos/status line per live friendly unit. Shared by
    the delta turn-prompt and the retry continuation prompt.

    Each unit line carries a positional-fact suffix when relevant —
    currently `[on friendly fort]` for units standing on a fort this
    team owns. Flags a recurring "fort heal" opportunity so the model
    doesn't have to cross-reference the board.forts list each turn.
    """
    # Pre-compute the set of friendly fort positions so the per-unit
    # loop is O(units). Only forts with owner == team count as
    # "friendly" — neutral forts (owner=None) don't fort-heal anyone.
    friendly_fort_positions: set[tuple[int, int]] = set()
    board = state_dict.get("board") or {}
    for f in board.get("forts") or []:
        if f.get("owner") == team:
            try:
                friendly_fort_positions.add((int(f["x"]), int(f["y"])))
            except (KeyError, TypeError, ValueError):
                continue

    own_lines: list[str] = []
    for u in state_dict.get("units", []):
        if u.get("owner") != team:
            continue
        if not u.get("alive", u.get("hp", 0) > 0):
            continue
        pos = u.get("pos") or {}
        try:
            px = int(pos.get("x", -1))
            py = int(pos.get("y", -1))
        except (TypeError, ValueError):
            px, py = -1, -1
        suffix = ""
        if (px, py) in friendly_fort_positions:
            # Fort-heal rule fires at the START of the OWNER's turn.
            # So a unit on a friendly fort at the time this prompt
            # renders has either JUST healed (turn-start) or will heal
            # next turn if it stays. Either way, flagging the position
            # saves the model a board-lookup round-trip.
            suffix = "  [on friendly fort — +3 HP at start of each of your turns]"
        own_lines.append(
            f"- {u.get('id')} ({u.get('class')})  "
            f"hp {u.get('hp')}  pos ({px}, {py})  "
            f"status {u.get('status')}{suffix}"
        )
    if own_lines:
        return "Your units:\n" + "\n".join(own_lines) + "\n\n"
    return "Your units: (none alive — you may have already lost)\n\n"


def build_turn_prompt_from_state_dict(
    state_dict: dict,
    viewer: Team,
    *,
    is_first_turn: bool = True,
    new_history: list[dict] | None = None,
    retry_n: int = 0,
    tactical_summary: dict | None = None,
    locale: str = "en",
) -> str:
    """Per-turn user message for the networked client.

    Three shapes:

      - **Bootstrap** (`is_first_turn=True`, retry_n=0): full state
        snapshot so the model's session has a starting mental map.
      - **Delta** (`is_first_turn=False`, retry_n=0): only what
        changed since the caller's last turn — enemy actions from
        `new_history`, plus a compact HP/pos/status line per LIVE
        unit on the caller's team. The model already has the prior
        turns in-session, so we only ship "what's new".
      - **Retry / continuation** (`retry_n > 0`): the previous
        play_turn loop exited without end_turn and the TUI is
        retrying the same game turn. Frames the message as a
        CONTINUATION so the model resumes instead of restarting
        (which otherwise caused double get_coach_messages calls
        and replayed "start-of-turn" planning). See
        TURN_PROMPT_TEMPLATE_RETRY for the full rationale.

    If the snapshot's active_player disagrees with `viewer`, a
    warning block is prepended so a misfired call to this function
    can't silently lie to the model.
    """
    team = viewer.value

    # Select templates by locale.
    if locale == "zh":
        from silicon_pantheon.harness.prompts_zh import (
            TURN_PROMPT_BOOTSTRAP_ZH,
            TURN_PROMPT_DELTA_ZH,
            TURN_PROMPT_RETRY_ZH,
        )
        _tmpl_bootstrap = TURN_PROMPT_BOOTSTRAP_ZH
        _tmpl_delta = TURN_PROMPT_DELTA_ZH
        _tmpl_retry = TURN_PROMPT_RETRY_ZH
    else:
        _tmpl_bootstrap = TURN_PROMPT_TEMPLATE_BOOTSTRAP
        _tmpl_delta = TURN_PROMPT_TEMPLATE_DELTA
        _tmpl_retry = TURN_PROMPT_TEMPLATE_RETRY

    # Retry path wins over both bootstrap and delta: a retry is never
    # a fresh turn, even on the notional "first turn" (the model has
    # already seen a turn-prompt once — this is a continuation).
    if retry_n > 0:
        prompt = _tmpl_retry.format(
            turn=state_dict.get("turn", "?"),
            your_units_section=_build_own_units_section(state_dict, team),
            tactical_section=_build_tactical_section(tactical_summary),
        )
        active = state_dict.get("active_player")
        if active is not None and active != team:
            prompt = (
                _TURN_PROMPT_MISMATCH_WARNING.format(team=team)
                + "\n"
                + prompt
            )
        return prompt + _debug_turn_reminder(locale)

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
        prompt = _tmpl_bootstrap.format(
            turn=state_dict.get("turn", "?"),
            team=team,
            state_json=json.dumps(snapshot, indent=2),
            tactical_section=_build_tactical_section(tactical_summary),
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

        your_units_section = _build_own_units_section(state_dict, team)
        tactical_section = _build_tactical_section(tactical_summary)
        # max_turns + turns_remaining come from state_dict (serializer
        # already computes both). Fall back to "?" if the server didn't
        # ship them so the template render can't hard-fail.
        max_turns = state_dict.get("max_turns", "?")
        tc = state_dict.get("turn_clock") or {}
        turns_remaining = tc.get("turns_remaining", "?")

        prompt = _tmpl_delta.format(
            turn=state_dict.get("turn", "?"),
            max_turns=max_turns,
            turns_remaining=turns_remaining,
            team=team,
            opponent_actions_section=opponent_actions_section,
            your_units_section=your_units_section,
            tactical_section=tactical_section,
        )

    active = state_dict.get("active_player")
    if active is not None and active != team:
        prompt = (
            _TURN_PROMPT_MISMATCH_WARNING.format(team=team)
            + "\n"
            + prompt
        )
    return prompt + _debug_turn_reminder(locale)


def load_strategy(path: str | Path | None) -> str | None:
    if path is None:
        return None
    p = Path(path)
    if not p.exists():
        return None
    return p.read_text(encoding="utf-8").strip()
