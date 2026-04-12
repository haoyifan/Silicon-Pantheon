"""Sidebar: turn info, unit HPs, last action."""

from __future__ import annotations

from rich.table import Table
from rich.text import Text

from clash_of_robots.server.engine.state import GameState, Team


def render_units_table(state: GameState) -> Table:
    t = Table(title="Units", show_header=True, header_style="bold", expand=False)
    t.add_column("ID")
    t.add_column("Team")
    t.add_column("Class")
    t.add_column("Pos")
    t.add_column("HP", justify="right")
    t.add_column("Status")

    for u in sorted(state.units.values(), key=lambda u: (u.owner.value, u.class_.value, u.id)):
        team_style = "cyan" if u.owner is Team.BLUE else "red"
        hp_pct = u.hp / u.stats.hp_max if u.stats.hp_max else 0
        hp_style = "green" if hp_pct > 0.66 else "yellow" if hp_pct > 0.33 else "red"
        t.add_row(
            u.id,
            Text(u.owner.value, style=team_style),
            u.class_.value,
            f"({u.pos.x},{u.pos.y})",
            Text(f"{u.hp}/{u.stats.hp_max}", style=hp_style),
            u.status.value,
        )
    return t


def render_header(state: GameState) -> Text:
    t = Text()
    t.append(f"Turn {state.turn}/{state.max_turns}  ", style="bold")
    t.append("Active: ")
    active_style = "cyan" if state.active_player is Team.BLUE else "red"
    t.append(state.active_player.value, style=active_style + " bold")
    t.append(f"   Status: {state.status.value}")
    if state.winner:
        t.append(f"   WINNER: {state.winner.value}", style="bold green")
    return t


def render_last_action(state: GameState) -> Text:
    t = Text("Last action: ", style="dim")
    la = state.last_action
    if la is None:
        t.append("—")
        return t
    t.append(str(la))
    return t
