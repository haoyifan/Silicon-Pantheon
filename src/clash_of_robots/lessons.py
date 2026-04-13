"""Post-match lessons: per-team written reflections on what worked or didn't.

Each completed match can produce one lesson per team (via
`Provider.summarize_match`). Lessons are stored as markdown files with YAML
frontmatter, named by a human-readable slug so agents can grep the folder
for relevant priors before playing a similar scenario.

Layout:

    lessons/
      02_basic_mirror/
        dont-chase-healers-into-the-fort.md
        archer-positioning-in-forest.md

Slug collisions within the same scenario are disambiguated with a `-2`,
`-3`, ... suffix on the filename (but the stored `slug` field stays the
agent-chosen one — the filename is just a uniqueness detail).
"""

from __future__ import annotations

import datetime as _dt
import re
from dataclasses import asdict, dataclass
from pathlib import Path

import yaml

FRONTMATTER_KEYS = (
    "title",
    "slug",
    "scenario",
    "team",
    "model",
    "outcome",
    "reason",
    "created_at",
)


@dataclass
class Lesson:
    slug: str
    title: str
    scenario: str
    team: str  # "blue" | "red"
    model: str
    outcome: str  # "win" | "loss" | "draw"
    reason: str  # "seize" | "elimination" | "max_turns" | ""
    created_at: str  # ISO-8601
    body: str  # markdown

    @staticmethod
    def now_iso() -> str:
        return _dt.datetime.now(tz=_dt.timezone.utc).replace(microsecond=0).isoformat()


_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


def slugify(text: str, *, max_len: int = 60) -> str:
    """Turn arbitrary text into a filesystem-safe slug.

    Lowercases, collapses non-alphanumerics to single dashes, trims, and
    caps length. Empty input yields 'lesson'.
    """
    s = _SLUG_STRIP.sub("-", text.lower()).strip("-")
    if not s:
        return "lesson"
    return s[:max_len].rstrip("-")


class LessonStore:
    """Filesystem-backed store of Lessons, sharded by scenario."""

    def __init__(self, root: Path | str):
        self.root = Path(root)

    # ---- writes ----

    def save(self, lesson: Lesson) -> Path:
        """Write the lesson to disk, returning the final path.

        Creates the scenario directory if needed. If a file with the slug's
        filename already exists (collision), appends -2, -3, ... until free.
        """
        scenario_dir = self.root / lesson.scenario
        scenario_dir.mkdir(parents=True, exist_ok=True)
        path = self._unique_path(scenario_dir, lesson.slug)
        path.write_text(_serialize(lesson), encoding="utf-8")
        return path

    @staticmethod
    def _unique_path(scenario_dir: Path, slug: str) -> Path:
        candidate = scenario_dir / f"{slug}.md"
        if not candidate.exists():
            return candidate
        i = 2
        while True:
            candidate = scenario_dir / f"{slug}-{i}.md"
            if not candidate.exists():
                return candidate
            i += 1

    # ---- reads ----

    def load(self, path: Path | str) -> Lesson:
        return _deserialize(Path(path).read_text(encoding="utf-8"))

    def list_for_scenario(
        self, scenario: str, *, limit: int | None = None
    ) -> list[Lesson]:
        """Return lessons for a scenario, most recent (by created_at) first."""
        scenario_dir = self.root / scenario
        if not scenario_dir.is_dir():
            return []
        lessons: list[Lesson] = []
        for p in scenario_dir.glob("*.md"):
            try:
                lessons.append(self.load(p))
            except Exception:
                # Never let a malformed file break the read path.
                continue
        lessons.sort(key=lambda le: le.created_at, reverse=True)
        if limit is not None:
            lessons = lessons[:limit]
        return lessons


# ---- serialization ----


def _serialize(lesson: Lesson) -> str:
    data = asdict(lesson)
    body = data.pop("body")
    frontmatter = {k: data[k] for k in FRONTMATTER_KEYS if k in data}
    return (
        "---\n"
        + yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True)
        + "---\n\n"
        + body.rstrip()
        + "\n"
    )


_FM_RE = re.compile(r"\A---\n(.*?)\n---\n?(.*)\Z", re.DOTALL)


def _deserialize(text: str) -> Lesson:
    m = _FM_RE.match(text)
    if not m:
        raise ValueError("file is missing YAML frontmatter")
    fm = yaml.safe_load(m.group(1)) or {}
    # Strip the blank line the serializer injects after the frontmatter and
    # the trailing newline it appends, so save→load round-trips cleanly.
    body = m.group(2).lstrip("\n").rstrip("\n")
    return Lesson(
        slug=str(fm.get("slug", "")),
        title=str(fm.get("title", "")),
        scenario=str(fm.get("scenario", "")),
        team=str(fm.get("team", "")),
        model=str(fm.get("model", "")),
        outcome=str(fm.get("outcome", "")),
        reason=str(fm.get("reason", "")),
        created_at=str(fm.get("created_at", "")),
        body=body,
    )
