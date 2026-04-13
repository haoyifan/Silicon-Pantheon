"""Tests for the lesson-JSON parser used by NetworkedAgent.

The full play_turn / summarize_match paths involve the Claude SDK
and a running server; those are exercised end-to-end by hand. Here
we pin the tolerant JSON extractor — the same helper that decides
whether a model response becomes a saved Lesson or is dropped.
"""

from __future__ import annotations

from clash_of_robots.client.agent_bridge import _parse_lesson_json


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
