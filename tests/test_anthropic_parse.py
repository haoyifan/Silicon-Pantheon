"""Unit tests for the lesson JSON extractor used by AnthropicProvider.

No SDK calls — just the pure parser that pulls {title, slug, body} out of
model text, tolerating code fences and surrounding prose.
"""

from __future__ import annotations

from clash_of_robots.harness.providers.anthropic import _parse_lesson_json


def test_bare_json_object() -> None:
    out = _parse_lesson_json('{"title": "A", "slug": "a", "body": "B"}')
    assert out == {"title": "A", "slug": "a", "body": "B"}


def test_tolerates_code_fence() -> None:
    text = '```json\n{"title": "A", "slug": "a", "body": "B"}\n```'
    out = _parse_lesson_json(text)
    assert out == {"title": "A", "slug": "a", "body": "B"}


def test_tolerates_surrounding_prose() -> None:
    text = 'Here is my lesson:\n{"title": "A", "slug": "a", "body": "B"}\n\nHope this helps!'
    out = _parse_lesson_json(text)
    assert out == {"title": "A", "slug": "a", "body": "B"}


def test_returns_none_on_empty() -> None:
    assert _parse_lesson_json("") is None
    assert _parse_lesson_json("    ") is None


def test_returns_none_on_unparseable() -> None:
    assert _parse_lesson_json("no braces here") is None
    assert _parse_lesson_json("{ not json") is None


def test_returns_none_on_non_object() -> None:
    # A JSON array, not an object.
    assert _parse_lesson_json("[1, 2, 3]") is None
