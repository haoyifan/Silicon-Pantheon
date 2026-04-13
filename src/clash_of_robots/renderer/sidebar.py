"""Sidebar: turn info, unit HPs, last action."""

from __future__ import annotations

from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from clash_of_robots.server.engine.state import GameState, Team
from clash_of_robots.server.session import Session

THOUGHTS_PANEL_HEIGHT = 12  # fixed rows so the board doesn't shift around


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


def render_thoughts_panel(session: Session, height: int = THOUGHTS_PANEL_HEIGHT) -> Panel:
    """Fixed-height panel of recent agent reasoning. Newest at the bottom.

    The fixed height keeps the rest of the layout (board, units table) from
    shifting as new thoughts arrive.
    """
    inner = height - 2  # account for panel borders
    body = Text()
    thoughts = list(session.thoughts)[-inner:] if inner > 0 else []
    if not thoughts:
        body.append("(no agent reasoning yet)", style="dim italic")
    else:
        for i, th in enumerate(thoughts):
            style = "cyan" if th.team is Team.BLUE else "red"
            body.append(f"T{th.turn} {th.team.value}: ", style=style + " bold")
            # Collapse whitespace; one thought per line so the panel stays bounded.
            collapsed = " ".join(th.text.split())
            body.append(collapsed)
            if i != len(thoughts) - 1:
                body.append("\n")
    return Panel(body, title="Agent reasoning", border_style="dim", height=height)


def render_last_action(state: GameState) -> Text:
    t = Text("Last action: ", style="dim")
    la = state.last_action
    if la is None:
        t.append("—")
        return t
    t.append(str(la))
    return t
