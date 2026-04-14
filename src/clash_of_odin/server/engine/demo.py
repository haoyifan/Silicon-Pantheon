"""Tiny end-to-end demo: load a scenario, apply scripted moves, print state.

Run: `python -m clash_of_odin.server.engine.demo`
"""

from __future__ import annotations

from .rules import EndTurnAction, MoveAction, apply
from .scenarios import load_scenario


def _ascii(state) -> str:
    rows = []
    for y in range(state.board.height):
        row = []
        for x in range(state.board.width):
            from .state import Pos  # local import

            p = Pos(x, y)
            u = state.unit_at(p)
            if u is not None:
                ch = (
                    u.class_[0].upper()
                    if u.owner.value == "blue"
                    else u.class_[0].lower()
                )
            else:
                t = state.board.tile(p).type
                ch = {"plain": ".", "forest": "F", "mountain": "M", "fort": "*"}[t]
            row.append(ch)
        rows.append(" ".join(row))
    return "\n".join(rows)


def main() -> None:
    state = load_scenario("01_tiny_skirmish")
    print("=== initial ===")
    print(_ascii(state))
    print(f"active: {state.active_player.value}")

    # Blue knight walks forward.
    from .state import Pos

    knight = next(
        u for u in state.units.values() if u.class_ == "knight" and u.owner.value == "blue"
    )
    apply(state, MoveAction(unit_id=knight.id, dest=Pos(0, 3)))
    apply(state, EndTurnAction())

    print("\n=== after blue T1 ===")
    print(_ascii(state))

    # Red responds: end turn without acting.
    apply(state, EndTurnAction())

    print("\n=== after red T1 (passed) ===")
    print(_ascii(state))
    print(f"turn: {state.turn}, active: {state.active_player.value}, status: {state.status.value}")


if __name__ == "__main__":
    main()
