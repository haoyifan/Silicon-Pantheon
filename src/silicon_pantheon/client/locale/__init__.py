"""Locale registry — the single lookup layer between core logic and
user-visible text.

Usage:
    from silicon_pantheon.client.locale import t

    panel_title = t("panel.player")          # English (default)
    panel_title = t("panel.player", "zh")    # Chinese

Keys are dot-separated paths into a nested YAML structure loaded
from `en.yaml`, `zh.yaml`, etc. in this package's directory.

Fallback chain:
    1. Exact key in the requested locale
    2. Exact key in English ("en")
    3. The key itself (so a missing translation renders the key
       path rather than crashing)

Adding a new language = adding one YAML file here. Zero code changes.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

_LOCALE_DIR = Path(__file__).parent
_cache: dict[str, dict[str, Any]] = {}


def _load(locale: str) -> dict[str, Any]:
    """Load and cache a locale YAML file."""
    if locale in _cache:
        return _cache[locale]
    path = _LOCALE_DIR / f"{locale}.yaml"
    if not path.exists():
        _cache[locale] = {}
        return _cache[locale]
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    _cache[locale] = data
    return data


def _resolve(data: dict, key: str) -> str | None:
    """Walk a dot-separated key path through nested dicts."""
    parts = key.split(".")
    node: Any = data
    for part in parts:
        if not isinstance(node, dict):
            return None
        node = node.get(part)
        if node is None:
            return None
    return str(node) if node is not None else None


def t(key: str, locale: str = "en") -> str:
    """Look up a user-visible string by dot-separated key.

    Fallback: locale → English → the raw key.
    """
    if locale != "en":
        val = _resolve(_load(locale), key)
        if val is not None:
            return val
    val = _resolve(_load("en"), key)
    if val is not None:
        return val
    return key


def clear_cache() -> None:
    """For testing — force reload on next access."""
    _cache.clear()


def available_locales() -> list[str]:
    """Return locale codes for which a YAML file exists."""
    return sorted(
        p.stem for p in _LOCALE_DIR.glob("*.yaml")
        if p.stem not in ("__init__",)
    )
