"""Coach communication + telemetry tools."""

from __future__ import annotations

from silicon_pantheon.shared.sanitize import sanitize_freetext

from ..engine.state import Team
from ..session import CoachMessage, Session


def send_to_agent(session: Session, viewer: Team, team: str, text: str) -> dict:
    try:
        target = Team(team)
    except ValueError:
        return {"ok": False, "error": f"invalid team: {team!r} (must be 'blue' or 'red')"}
    if target != viewer:
        return {"ok": False, "error": "can only message your own team's agent"}
    text = sanitize_freetext(text, max_length=2_000)
    session.coach_queues[target].append(CoachMessage(turn=session.state.turn, text=text))
    session.log("coach_message", {"to": target.value, "text": text, "turn": session.state.turn})
    return {"ok": True, "queued_for": target.value, "turn": session.state.turn}


def report_tokens(session: Session, viewer: Team, tokens: int) -> dict:
    """Client reports token usage so the server can aggregate both sides."""
    session.tokens_by_team[viewer] += tokens
    return {"ok": True}


def get_match_telemetry(session: Session, viewer: Team) -> dict:
    """Return server-side telemetry for both teams."""
    result: dict = {}
    for team in (Team.BLUE, Team.RED):
        times = session.turn_times_by_team[team]
        avg = sum(times) / len(times) if times else 0.0
        result[team.value] = {
            "turns_played": len(times),
            "total_thinking_time_s": round(sum(times), 1),
            "avg_thinking_time_s": round(avg, 1),
            "total_tokens": session.tokens_by_team[team],
            "total_tool_calls": session.tool_calls_by_team[team],
            "total_errors": session.tool_errors_by_team[team],
        }
    return result
