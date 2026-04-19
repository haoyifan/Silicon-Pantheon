"""Scenario-level localization — merges locale/xx.yaml overrides
on top of a scenario's describe_scenario bundle.

The locale file mirrors the config.yaml structure but only contains
translatable strings (display_name, description, narrative). Stats
(hp_max, atk, etc.) come from the base config and are never in the
locale file.

Usage:
    from silicon_pantheon.client.locale.scenario import localize_scenario

    bundle = ... # from describe_scenario
    localized = localize_scenario(bundle, "zh")

Adding a new language for a scenario = adding one YAML file at
`games/<scenario>/locale/<lang>.yaml`. Zero code changes.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

_RE_LOCALE = re.compile(r"^[a-z]{2,5}$")
_RE_SCENARIO = re.compile(r"^[a-zA-Z0-9_-]+$")


def _deep_merge(base: dict, overrides: dict) -> dict:
    """Recursively merge overrides into base. Override values win
    for leaf nodes; dicts are merged recursively; lists from
    overrides replace base lists entirely (narrative events, win
    descriptions, etc. are authored as a complete set per locale)."""
    result = dict(base)
    for key, val in overrides.items():
        if (
            key in result
            and isinstance(result[key], dict)
            and isinstance(val, dict)
        ):
            result[key] = _deep_merge(result[key], val)
        else:
            result[key] = val
    return result


def _find_scenario_dir(scenario_name: str) -> Path | None:
    """Locate the games/<scenario>/ directory. Walks a few common
    roots so this works from the repo root, from tests, and from
    installed packages."""
    candidates = [
        Path("games") / scenario_name,
        Path(__file__).resolve().parents[4] / "games" / scenario_name,
    ]
    for c in candidates:
        if c.is_dir():
            return c
    return None


def load_scenario_locale(scenario_name: str, locale: str) -> dict | None:
    """Load the locale override YAML for a scenario, or None if it
    doesn't exist. Does NOT merge — the caller does that."""
    if not _RE_SCENARIO.fullmatch(scenario_name) or not _RE_LOCALE.fullmatch(locale):
        return None
    if locale == "en":
        return None
    d = _find_scenario_dir(scenario_name)
    if d is None:
        return None
    path = d / "locale" / f"{locale}.yaml"
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _load_base_locale(locale: str) -> dict | None:
    """Load the shared base locale that covers built-in terrain types
    and unit classes. Located at client/locale/scenario_base/<locale>.yaml."""
    if not _RE_LOCALE.fullmatch(locale):
        return None
    if locale == "en":
        return None
    path = Path(__file__).parent / "scenario_base" / f"{locale}.yaml"
    if not path.exists():
        return None
    if locale not in _base_cache:
        with open(path, encoding="utf-8") as f:
            _base_cache[locale] = yaml.safe_load(f) or {}
    return _base_cache[locale]


_base_cache: dict[str, dict] = {}


def localize_scenario(
    bundle: dict[str, Any],
    locale: str,
) -> dict[str, Any]:
    """Apply locale overrides to a describe_scenario bundle.

    The bundle is the dict returned by the server's describe_scenario
    tool (or cached on SharedState.scenario_description). The locale
    file is merged on top — translatable string fields override, stats
    stay from the base.

    Three layers are merged (later wins):
      1. Original bundle (English from the server)
      2. Shared base locale (built-in terrain/unit class translations)
      3. Per-scenario locale (scenario-specific overrides)

    Returns a NEW dict (base is not mutated).
    """
    if locale == "en":
        return bundle
    # Layer 1: apply shared base translations (terrain, built-in units)
    base_overrides = _load_base_locale(locale)
    result = _deep_merge(bundle, base_overrides) if base_overrides else dict(bundle)
    # Layer 2: apply per-scenario translations
    name = bundle.get("name") or ""
    overrides = load_scenario_locale(name, locale)
    if overrides is None:
        for key in ("scenario_slug", "slug"):
            slug = bundle.get(key)
            if slug:
                overrides = load_scenario_locale(slug, locale)
                if overrides is not None:
                    break
    if overrides is not None:
        result = _deep_merge(result, overrides)
    return result
