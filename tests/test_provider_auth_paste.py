"""API-key paste handling — bracketed-paste filter + key validation."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from silicon_pantheon.client.tui.app import SharedState
from silicon_pantheon.client.tui.screens.provider_auth import (
    ProviderAuthScreen,
    _Step,
    _validate_api_key,
)


class _FakeApp:
    def __init__(self) -> None:
        self.state = SharedState()
        self.client = None
        self.exited = False

    def exit(self) -> None:
        self.exited = True


@pytest.fixture
def fresh_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    for var in ("XAI_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    yield tmp_path


def test_long_paste_accumulates_into_key_buffer(fresh_home):
    """Regression: bracketed-paste markers used to decode as 'esc'
    which kicked the user out of paste mode and truncated the buffer
    to a handful of chars. The fix (reader returns '' for the
    delimiters) means all pasted chars flow into the handler as
    normal keys and accumulate correctly."""
    app = _FakeApp()
    screen = ProviderAuthScreen(app)
    screen._step = _Step(kind="api_key", provider_id="xai", focused=1)

    full_key = "sk-xai-" + "a" * 80  # plausible 87-char xAI key
    for ch in full_key:
        asyncio.run(screen.handle_key(ch))

    assert screen._step.key_buffer == full_key


def test_bracketed_paste_event_dumps_into_buffer(fresh_home):
    """The key reader emits `paste:<content>` as a single event for
    clipboard pastes (bracketed-paste mode ?2004). The screen should
    absorb the whole thing in one shot, preserving case and surviving
    any 'q' chars inside the key."""
    app = _FakeApp()
    screen = ProviderAuthScreen(app)
    screen._step = _Step(kind="api_key", provider_id="xai", focused=1)

    # A plausible xAI key: mixed case, includes a 'q' (used to exit
    # the app under the old per-char path).
    full_key = "sk-xai-qWeRtY" + "Z" * 60 + "abc"
    asyncio.run(screen.handle_key(f"paste:{full_key}"))

    assert screen._step.key_buffer == full_key
    assert not app.exited


def test_bracketed_paste_auto_switches_to_paste_row(fresh_home):
    """User lands on the 'use env var' row by default. Pasting from
    there should flip focus to the paste row so the key lands visibly
    rather than being silently dropped."""
    app = _FakeApp()
    screen = ProviderAuthScreen(app)
    screen._step = _Step(kind="api_key", provider_id="xai", focused=0)

    asyncio.run(screen.handle_key("paste:sk-xai-abc123"))

    assert screen._step.focused == 1
    assert screen._step.key_buffer == "sk-xai-abc123"


def test_q_during_api_key_paste_does_not_exit(fresh_home):
    """Regression: the global 'q' → quit handler used to fire even
    while the user was in the api_key paste row, so any 'q' inside
    a pasted key exited the app. Now it's gated by `not in_paste`."""
    app = _FakeApp()
    screen = ProviderAuthScreen(app)
    screen._step = _Step(kind="api_key", provider_id="xai", focused=1)

    asyncio.run(screen.handle_key("q"))

    assert not app.exited
    assert screen._step.key_buffer == "q"


def test_empty_key_from_reader_is_filtered_out():
    """The bracketed-paste delimiters decode to '' in the key reader;
    the reader's main loop has `if key:` before queuing, so empty
    strings never reach the screen handlers at all. Regression guard
    — check that the filter is still there."""
    import inspect

    from silicon_pantheon.client.tui import app as tui_app

    src = inspect.getsource(tui_app.TUIApp._key_reader)
    assert "if key:" in src, "key_reader must guard against empty keys"


def test_validate_api_key_returns_none_on_200(fresh_home):
    """Fake the openai SDK to simulate a successful /v1/models call."""
    client = MagicMock()
    client.models = MagicMock()
    client.models.list = AsyncMock(return_value={"data": []})
    client.close = AsyncMock()

    with patch(
        "openai.AsyncOpenAI",
        return_value=client,
    ):
        err = asyncio.run(_validate_api_key("xai", "sk-xai-fake"))
    assert err is None
    client.models.list.assert_awaited_once()


def test_validate_api_key_surfaces_auth_error(fresh_home):
    """Rejected key → readable error string, not None."""
    client = MagicMock()
    client.models = MagicMock()
    client.models.list = AsyncMock(side_effect=Exception("401 invalid_api_key"))
    client.close = AsyncMock()

    with patch("openai.AsyncOpenAI", return_value=client):
        err = asyncio.run(_validate_api_key("xai", "sk-xai-bad"))
    assert err is not None
    assert "401" in err or "invalid" in err.lower()


def test_validate_rejects_empty_key(fresh_home):
    assert asyncio.run(_validate_api_key("xai", "")) == "empty key"
    assert asyncio.run(_validate_api_key("xai", "   ")) == "empty key"


def test_validate_unknown_provider(fresh_home):
    assert asyncio.run(_validate_api_key("nonexistent", "key")) == "unknown provider"
