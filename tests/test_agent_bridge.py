"""Tests for the lesson-JSON parser used by NetworkedAgent.

The full play_turn / summarize_match paths involve the Claude SDK
and a running server; those are exercised end-to-end by hand. Here
we pin the tolerant JSON extractor — the same helper that decides
whether a model response becomes a saved Lesson or is dropped.
"""

from __future__ import annotations

from silicon_pantheon.client.providers.anthropic import _parse_lesson_json


def test_networked_agent_constructs_without_nameerror():
    """Regression: __init__ referenced `logging.getLogger(...)` but
    the module didn't import `logging`, so every construction raised
    NameError at `self._prompt_log = ...` before the first turn."""
    import asyncio

    from silicon_pantheon.client.agent_bridge import NetworkedAgent

    class _StubClient:
        async def call(self, *a, **kw):
            return {"ok": True, "result": {}}

    class _StubAdapter:
        async def close(self) -> None:
            return None

    agent = NetworkedAgent(
        client=_StubClient(),
        model="grok-4",
        scenario="journey_to_the_west",
        adapter=_StubAdapter(),
    )
    # Prompt logger is attached and points at the expected namespace.
    assert agent._prompt_log.name == "silicon.agent.prompts"
    asyncio.run(agent.close())


def test_bare_json_object() -> None:
    out = _parse_lesson_json('{"title":"T","slug":"s","body":"B"}')
    assert out == {"title": "T", "slug": "s", "body": "B"}


def test_code_fence_json() -> None:
    text = '```json\n{"title":"T","slug":"s","body":"B"}\n```'
    assert _parse_lesson_json(text) == {"title": "T", "slug": "s", "body": "B"}


def test_surrounding_prose() -> None:
    text = "Here is my lesson:\n{\"title\":\"T\",\"slug\":\"s\",\"body\":\"B\"}\n\nthanks!"
    assert _parse_lesson_json(text) == {"title": "T", "slug": "s", "body": "B"}


def test_empty() -> None:
    assert _parse_lesson_json("") is None
    assert _parse_lesson_json("    ") is None


def test_unparseable() -> None:
    assert _parse_lesson_json("no braces") is None
    assert _parse_lesson_json("{ not json") is None


def test_rejects_array() -> None:
    assert _parse_lesson_json("[1, 2, 3]") is None


# ---- _detect_battlefield_changes tests ----


def _make_agent():
    """Build a minimal NetworkedAgent suitable for unit-testing
    _detect_battlefield_changes (no server connection needed)."""
    import asyncio
    from silicon_pantheon.client.agent_bridge import NetworkedAgent

    class _StubClient:
        async def call(self, *a, **kw):
            return {"ok": True, "result": {}}

    class _StubAdapter:
        async def close(self) -> None:
            return None

    agent = NetworkedAgent(
        client=_StubClient(),
        model="test-model",
        scenario="01_tiny_skirmish",
        adapter=_StubAdapter(),
    )
    return agent


def _state_with_units(units: list[dict]) -> dict:
    return {"units": units}


def _unit(uid: str, cls: str = "knight", x: int = 0, y: int = 0, hp: int = 20):
    return {"id": uid, "class": cls, "pos": {"x": x, "y": y}, "hp": hp, "alive": True}


class TestDetectBattlefieldChanges:
    """Tests for _detect_battlefield_changes fog-aware alert logic."""

    def test_first_call_no_alerts(self):
        """First call seeds _last_seen_unit_ids; no alerts emitted."""
        from silicon_pantheon.server.engine.state import Team
        agent = _make_agent()
        state = _state_with_units([
            _unit("u_b_knight_1"), _unit("u_r_archer_1"),
        ])
        agent._detect_battlefield_changes(state, Team.BLUE)
        assert agent._battlefield_alerts == []
        assert agent._last_seen_unit_ids == {"u_b_knight_1", "u_r_archer_1"}

    def test_friendly_reinforcement_blue_viewer(self):
        """Blue viewer sees a new u_b_ unit → 'NEW friendly reinforcement'."""
        from silicon_pantheon.server.engine.state import Team
        agent = _make_agent()
        state1 = _state_with_units([_unit("u_b_knight_1")])
        agent._detect_battlefield_changes(state1, Team.BLUE)

        state2 = _state_with_units([
            _unit("u_b_knight_1"),
            _unit("u_b_mage_1", cls="mage", x=3, y=4),
        ])
        agent._detect_battlefield_changes(state2, Team.BLUE)
        assert len(agent._battlefield_alerts) == 1
        alert = agent._battlefield_alerts[0]
        assert "NEW friendly reinforcement" in alert
        assert "u_b_mage_1" in alert
        assert "(mage)" in alert
        assert "(3,4)" in alert

    def test_enemy_spotted_blue_viewer(self):
        """Blue viewer sees a new u_r_ unit → 'ENEMY unit spotted'
        (could be reinforcement or fog reveal — we don't claim which)."""
        from silicon_pantheon.server.engine.state import Team
        agent = _make_agent()
        state1 = _state_with_units([_unit("u_b_knight_1")])
        agent._detect_battlefield_changes(state1, Team.BLUE)

        state2 = _state_with_units([
            _unit("u_b_knight_1"),
            _unit("u_r_archer_1", cls="archer", x=7, y=2),
        ])
        agent._detect_battlefield_changes(state2, Team.BLUE)
        assert len(agent._battlefield_alerts) == 1
        alert = agent._battlefield_alerts[0]
        assert "ENEMY unit spotted" in alert
        assert "u_r_archer_1" in alert
        assert "(archer)" in alert
        assert "(7,2)" in alert

    def test_friendly_eliminated_blue_viewer(self):
        """Blue viewer's own unit vanishes → 'friendly unit eliminated'."""
        from silicon_pantheon.server.engine.state import Team
        agent = _make_agent()
        state1 = _state_with_units([
            _unit("u_b_knight_1"), _unit("u_b_archer_1"),
        ])
        agent._detect_battlefield_changes(state1, Team.BLUE)

        state2 = _state_with_units([_unit("u_b_knight_1")])
        agent._detect_battlefield_changes(state2, Team.BLUE)
        assert len(agent._battlefield_alerts) == 1
        alert = agent._battlefield_alerts[0]
        assert "friendly unit eliminated" in alert
        assert "u_b_archer_1" in alert

    def test_enemy_lost_contact_blue_viewer(self):
        """Blue viewer's enemy vanishes → 'lost contact' (NOT 'eliminated')."""
        from silicon_pantheon.server.engine.state import Team
        agent = _make_agent()
        state1 = _state_with_units([
            _unit("u_b_knight_1"), _unit("u_r_knight_1"),
        ])
        agent._detect_battlefield_changes(state1, Team.BLUE)

        state2 = _state_with_units([_unit("u_b_knight_1")])
        agent._detect_battlefield_changes(state2, Team.BLUE)
        assert len(agent._battlefield_alerts) == 1
        alert = agent._battlefield_alerts[0]
        assert "lost contact" in alert
        assert "u_r_knight_1" in alert
        assert "eliminated" in alert or "moved out of sight" in alert

    def test_red_viewer_prefix_symmetry(self):
        """Red viewer's own units are u_r_; enemies are u_b_."""
        from silicon_pantheon.server.engine.state import Team
        agent = _make_agent()
        state1 = _state_with_units([_unit("u_r_knight_1")])
        agent._detect_battlefield_changes(state1, Team.RED)

        state2 = _state_with_units([
            _unit("u_r_knight_1"),
            _unit("u_r_mage_1", cls="mage", x=1, y=1),
            _unit("u_b_archer_1", cls="archer", x=5, y=5),
        ])
        agent._detect_battlefield_changes(state2, Team.RED)
        alerts = agent._battlefield_alerts
        assert len(alerts) == 2
        friendly_alert = [a for a in alerts if "u_r_mage_1" in a][0]
        enemy_alert = [a for a in alerts if "u_b_archer_1" in a][0]
        assert "NEW friendly reinforcement" in friendly_alert
        assert "ENEMY unit spotted" in enemy_alert

    def test_red_viewer_vanish_symmetry(self):
        """Red viewer: own unit gone = eliminated, blue unit gone = lost contact."""
        from silicon_pantheon.server.engine.state import Team
        agent = _make_agent()
        state1 = _state_with_units([
            _unit("u_r_knight_1"), _unit("u_b_knight_1"),
        ])
        agent._detect_battlefield_changes(state1, Team.RED)

        state2 = _state_with_units([])
        agent._detect_battlefield_changes(state2, Team.RED)
        alerts = agent._battlefield_alerts
        assert len(alerts) == 2
        red_alert = [a for a in alerts if "u_r_knight_1" in a][0]
        blue_alert = [a for a in alerts if "u_b_knight_1" in a][0]
        assert "friendly unit eliminated" in red_alert
        assert "lost contact" in blue_alert

    def test_dead_units_excluded_from_current(self):
        """Units with hp=0 or alive=False are excluded from current set."""
        from silicon_pantheon.server.engine.state import Team
        agent = _make_agent()
        state1 = _state_with_units([
            _unit("u_b_knight_1"), _unit("u_r_knight_1"),
        ])
        agent._detect_battlefield_changes(state1, Team.BLUE)

        state2 = _state_with_units([
            _unit("u_b_knight_1"),
            {"id": "u_r_knight_1", "class": "knight", "pos": {"x": 5, "y": 5},
             "hp": 0, "alive": False},
        ])
        agent._detect_battlefield_changes(state2, Team.BLUE)
        assert len(agent._battlefield_alerts) == 1
        assert "lost contact" in agent._battlefield_alerts[0]

    def test_mixed_appeared_and_vanished(self):
        """Both appearances and disappearances in the same transition."""
        from silicon_pantheon.server.engine.state import Team
        agent = _make_agent()
        state1 = _state_with_units([
            _unit("u_b_knight_1"), _unit("u_r_archer_1"),
        ])
        agent._detect_battlefield_changes(state1, Team.BLUE)

        state2 = _state_with_units([
            _unit("u_b_knight_1"),
            _unit("u_b_mage_1", cls="mage", x=2, y=2),
            _unit("u_r_cavalry_1", cls="cavalry", x=8, y=8),
        ])
        agent._detect_battlefield_changes(state2, Team.BLUE)
        alerts = agent._battlefield_alerts
        assert len(alerts) == 3
        texts = "\n".join(alerts)
        assert "NEW friendly reinforcement" in texts
        assert "u_b_mage_1" in texts
        assert "ENEMY unit spotted" in texts
        assert "u_r_cavalry_1" in texts
        assert "lost contact" in texts
        assert "u_r_archer_1" in texts

    def test_alerts_cleared_each_call(self):
        """Each call resets _battlefield_alerts — no accumulation."""
        from silicon_pantheon.server.engine.state import Team
        agent = _make_agent()
        state1 = _state_with_units([_unit("u_b_knight_1")])
        agent._detect_battlefield_changes(state1, Team.BLUE)

        state2 = _state_with_units([
            _unit("u_b_knight_1"), _unit("u_r_archer_1"),
        ])
        agent._detect_battlefield_changes(state2, Team.BLUE)
        assert len(agent._battlefield_alerts) == 1

        state3 = _state_with_units([
            _unit("u_b_knight_1"), _unit("u_r_archer_1"),
        ])
        agent._detect_battlefield_changes(state3, Team.BLUE)
        assert agent._battlefield_alerts == []
