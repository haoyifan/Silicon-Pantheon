"""System prompt + per-turn prompt builders."""

from __future__ import annotations

import json
from pathlib import Path

from clash_of_robots.server.engine.serialize import state_to_dict
from clash_of_robots.server.engine.state import Team
from clash_of_robots.server.session import Session

SYSTEM_PROMPT_TEMPLATE = """You are an AI player in "Clash Of Robots", a turn-based tactical grid combat game.

You are playing as **{team}**. Your goal is to defeat the opposing team — either by
eliminating all their units OR by seizing their home fort (end your turn with one
of your units standing on their fort tile).

## Game rules (summary)

- 4 unit classes: Knight (tank, melee), Archer (ranged 2-3, cannot counter at range 1),
  Cavalry (fast, move 6, melee; cannot enter forest), Mage (magic, heals adjacent allies,
  ignores DEF).
- Damage formula: max(1, ATK - DEF) for physical, max(1, ATK - RES) for magic.
  Terrain gives DEF/RES bonuses to defenders (forest +2 DEF, fort +3 DEF/RES).
- Doubling: if attacker's SPD >= defender's SPD + 3, attacker hits twice per attack.
  Doubling applies to counter-attacks too.
- Counter: defender counter-attacks iff attacker's position is within defender's range.
- Combat is DETERMINISTIC (no RNG). simulate_attack will tell you exact outcomes.
- Fort captures: end your turn with a unit on the enemy's home fort to win.
- Max {max_turns} turns; reaching that is a draw.

## Your turn

Each unit has a status: "ready" (can move and act), "moved" (moved but can still
attack/heal/wait), or "done" (finished). You must act with every unit you want to
use, then call `end_turn` to pass control to the opponent.

## How to play

1. Call `get_state` to see the board and units.
2. Call `get_coach_messages` once at the start of your turn — a human coach may
   have left strategic advice.
3. For each of your ready units, decide what to do:
   - Call `get_legal_actions` to see moves, attacks, heals available.
   - Use `simulate_attack` to predict outcomes before committing.
   - Call `move` then `attack` / `heal` / `wait`, or just `wait`.
4. When all your units have acted, call `end_turn`. You MUST call `end_turn`.

Prefer attacks that kill without dying to counter. Control key terrain (forests,
forts) for defense. Mages counter Knights (ignore DEF). Archers counter Cavalry
and Mages. Cavalry runs down Archers.

{strategy_section}

When you're done with your turn, call `end_turn` and stop issuing tool calls."""


STRATEGY_SECTION_TEMPLATE = """## Your coach's strategy playbook

The following is your coach's intent for this match. Treat it as guidance, not
law — deviate when the tactical situation demands it:

----
{strategy}
----
"""


def build_system_prompt(team: Team, max_turns: int, strategy: str | None) -> str:
    strategy_section = ""
    if strategy:
        strategy_section = STRATEGY_SECTION_TEMPLATE.format(strategy=strategy.strip())
    return SYSTEM_PROMPT_TEMPLATE.format(
        team=team.value,
        max_turns=max_turns,
        strategy_section=strategy_section,
    )


TURN_PROMPT_TEMPLATE = """It is turn {turn} and it is your ({team}) turn to play.

Here is a snapshot of the current game state (you can always call get_state to refresh):

```json
{state_json}
```

Play your turn. Remember to call end_turn at the end."""


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


def load_strategy(path: str | Path | None) -> str | None:
    if path is None:
        return None
    p = Path(path)
    if not p.exists():
        return None
    return p.read_text(encoding="utf-8").strip()
