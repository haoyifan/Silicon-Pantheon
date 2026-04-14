"""Smoke tests for ProviderAuthScreen — render + key navigation.

No SDK calls, no server. Mocks app.client and the credentials load
path via monkeypatch on the home directory."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from rich.console import Console

from clash_of_odin.client.tui.app import SharedState
from clash_of_odin.client.tui.screens.provider_auth import ProviderAuthScreen


class _FakeApp:
    def __init__(self) -> None:
        self.state = SharedState()
        self.client = None
        self.exited = False

    def exit(self) -> None:
        self.exited = True


def _render(screen) -> str:
    console = Console(record=True, width=120)
    console.print(screen.render())
    return console.export_text()


@pytest.fixture
def fresh_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    # credentials module imports Path.home() at call time, so setting
    # HOME is enough.
    yield tmp_path


def test_fresh_start_shows_provider_picker(fresh_home) -> None:
    app = _FakeApp()
    screen = ProviderAuthScreen(app)
    out = _render(screen)
    assert "Pick LLM provider" in out
    assert "Anthropic" in out
    assert "OpenAI" in out


def test_saved_defaults_shows_resume_prompt(fresh_home, monkeypatch) -> None:
    from clash_of_odin.client.credentials import (
        Credentials,
        ProviderCredential,
        save,
    )

    save(
        Credentials(
            default_provider="anthropic",
            default_model="claude-haiku-4-5",
            providers={
                "anthropic": ProviderCredential(auth_mode="subscription_cli")
            },
        )
    )
    app = _FakeApp()
    screen = ProviderAuthScreen(app)
    out = _render(screen)
    assert "saved defaults" in out.lower() or "Using saved" in out
    assert "claude-haiku-4-5" in out


def test_down_key_moves_focus_in_picker(fresh_home) -> None:
    app = _FakeApp()
    screen = ProviderAuthScreen(app)
    assert screen._step.focused == 0
    asyncio.run(screen.handle_key("down"))
    assert screen._step.focused == 1


def test_enter_on_openai_drills_to_api_key(fresh_home) -> None:
    app = _FakeApp()
    screen = ProviderAuthScreen(app)
    # Focus the second provider (OpenAI).
    asyncio.run(screen.handle_key("down"))
    asyncio.run(screen.handle_key("enter"))
    assert screen._step.kind == "api_key"
    assert screen._step.provider_id == "openai"


def test_quit_exits(fresh_home) -> None:
    app = _FakeApp()
    screen = ProviderAuthScreen(app)
    asyncio.run(screen.handle_key("q"))
    assert app.exited is True
