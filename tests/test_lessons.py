"""Tests for the LessonStore: round-trip, slug collisions, scenario listing."""

from __future__ import annotations

from pathlib import Path

from clash_of_robots.lessons import Lesson, LessonStore, slugify


def _sample(slug: str = "foo", scenario: str = "01_tiny_skirmish") -> Lesson:
    return Lesson(
        slug=slug,
        title="Foo",
        scenario=scenario,
        team="blue",
        model="claude-haiku-4-5",
        outcome="win",
        reason="elimination",
        created_at="2026-04-12T14:30:00+00:00",
        body="Body paragraph.\n\nAnother line.",
    )


def test_slugify_basics() -> None:
    assert slugify("Hello World") == "hello-world"
    assert slugify("  Don't chase healers!!!  ") == "don-t-chase-healers"
    assert slugify("") == "lesson"
    assert slugify("-----") == "lesson"
    assert len(slugify("a" * 200)) <= 60


def test_save_and_load_roundtrip(tmp_path: Path) -> None:
    store = LessonStore(tmp_path)
    le = _sample()
    path = store.save(le)
    assert path.exists()
    loaded = store.load(path)
    assert loaded == le


def test_slug_collision_appends_suffix(tmp_path: Path) -> None:
    store = LessonStore(tmp_path)
    p1 = store.save(_sample(slug="dupe"))
    p2 = store.save(_sample(slug="dupe"))
    p3 = store.save(_sample(slug="dupe"))
    assert p1.name == "dupe.md"
    assert p2.name == "dupe-2.md"
    assert p3.name == "dupe-3.md"


def test_list_for_scenario_sorted_newest_first(tmp_path: Path) -> None:
    store = LessonStore(tmp_path)
    older = _sample(slug="older")
    older.created_at = "2026-01-01T00:00:00+00:00"
    newer = _sample(slug="newer")
    newer.created_at = "2026-04-01T00:00:00+00:00"
    store.save(older)
    store.save(newer)
    lessons = store.list_for_scenario("01_tiny_skirmish")
    assert [le.slug for le in lessons] == ["newer", "older"]


def test_list_for_missing_scenario_returns_empty(tmp_path: Path) -> None:
    store = LessonStore(tmp_path)
    assert store.list_for_scenario("nope") == []


def test_list_respects_limit(tmp_path: Path) -> None:
    store = LessonStore(tmp_path)
    for i in range(5):
        le = _sample(slug=f"l{i}")
        le.created_at = f"2026-04-0{i + 1}T00:00:00+00:00"
        store.save(le)
    limited = store.list_for_scenario("01_tiny_skirmish", limit=2)
    assert [le.slug for le in limited] == ["l4", "l3"]


def test_malformed_file_is_skipped(tmp_path: Path) -> None:
    store = LessonStore(tmp_path)
    store.save(_sample(slug="good"))
    # Drop a malformed file alongside.
    bad = tmp_path / "01_tiny_skirmish" / "bad.md"
    bad.write_text("no frontmatter here", encoding="utf-8")
    lessons = store.list_for_scenario("01_tiny_skirmish")
    assert [le.slug for le in lessons] == ["good"]
