"""Smoke tests for ProviderAuthScreen — render + key navigation.

No SDK calls, no server. Mocks app.client and the credentials load
path via monkeypatch on the home directory."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from rich.console import Console

from silicon_pantheon.client.tui.app import SharedState
from silicon_pantheon.client.tui.screens.provider_auth import ProviderAuthScreen


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


def _focus_provider(screen, provider_id: str) -> None:
    """Press `down` until the picker's focused provider matches.
    Insulates tests from PROVIDERS list reordering."""
    from silicon_pantheon.shared.providers import PROVIDERS

    target = next(i for i, p in enumerate(PROVIDERS) if p.id == provider_id)
    while screen._step.focused != target:
        asyncio.run(screen.handle_key("down"))


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
    from silicon_pantheon.client.credentials import (
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
    """Picking the api-key OpenAI provider should land on the
    paste-key step. We look up its index by id rather than by
    keypress count so the test survives reordering of the provider
    catalog (e.g. inserting openai-codex above openai)."""
    from silicon_pantheon.shared.providers import PROVIDERS

    app = _FakeApp()
    screen = ProviderAuthScreen(app)
    target_idx = next(i for i, p in enumerate(PROVIDERS) if p.id == "openai")
    for _ in range(target_idx):
        asyncio.run(screen.handle_key("down"))
    asyncio.run(screen.handle_key("enter"))
    assert screen._step.kind == "api_key"
    assert screen._step.provider_id == "openai"


def test_saved_key_routes_through_confirm_auth(fresh_home) -> None:
    """Picking a provider that already has stored credentials lands
    on the confirm_auth step (keep vs re-enter), not directly on the
    model picker. 'Keep' advances to pick_model; 're-auth' drops
    into the paste step."""
    from silicon_pantheon.client.credentials import (
        Credentials,
        ProviderCredential,
        save,
    )

    save(
        Credentials(
            default_provider=None,
            default_model=None,
            providers={
                "openai": ProviderCredential(
                    auth_mode="api_key", inline_key="sk-already-saved"
                )
            },
        )
    )
    app = _FakeApp()
    screen = ProviderAuthScreen(app)
    _focus_provider(screen, "openai")
    asyncio.run(screen.handle_key("enter"))
    assert screen._step.kind == "confirm_auth"
    assert screen._step.provider_id == "openai"
    # Default focus = "keep" → Enter advances to model picker.
    asyncio.run(screen.handle_key("enter"))
    assert screen._step.kind == "pick_model"


def test_confirm_auth_reauth_branch_opens_paste(fresh_home) -> None:
    """Selecting 're-auth' on confirm_auth drops into the api_key
    step with the paste row pre-focused."""
    from silicon_pantheon.client.credentials import (
        Credentials,
        ProviderCredential,
        save,
    )

    save(
        Credentials(
            providers={
                "openai": ProviderCredential(
                    auth_mode="api_key", inline_key="sk-old"
                )
            }
        )
    )
    app = _FakeApp()
    screen = ProviderAuthScreen(app)
    _focus_provider(screen, "openai")
    asyncio.run(screen.handle_key("enter"))
    assert screen._step.kind == "confirm_auth"
    # Move to "re-auth", then Enter.
    asyncio.run(screen.handle_key("j"))
    asyncio.run(screen.handle_key("enter"))
    assert screen._step.kind == "api_key"
    assert screen._step.focused == 1  # paste row


def test_r_rotates_key_from_model_picker(fresh_home) -> None:
    """After accepting saved credentials via confirm_auth, the user
    lands on pick_model. Pressing 'r' there jumps back to the paste
    step so users can replace the stored key without editing
    credentials.json."""
    from silicon_pantheon.client.credentials import (
        Credentials,
        ProviderCredential,
        save,
    )

    save(
        Credentials(
            default_provider=None,
            default_model=None,
            providers={
                "openai": ProviderCredential(
                    auth_mode="api_key", inline_key="sk-old-key"
                )
            },
        )
    )
    app = _FakeApp()
    screen = ProviderAuthScreen(app)
    _focus_provider(screen, "openai")
    asyncio.run(screen.handle_key("enter"))
    # New flow: saved cred → confirm_auth, Enter on "keep" → pick_model.
    assert screen._step.kind == "confirm_auth"
    asyncio.run(screen.handle_key("enter"))
    assert screen._step.kind == "pick_model"
    # Pressing r should take us to the paste step, focused on the
    # paste row (focused=1) not the env-var row.
    asyncio.run(screen.handle_key("r"))
    assert screen._step.kind == "api_key"
    assert screen._step.provider_id == "openai"
    assert screen._step.focused == 1


def test_no_saved_key_still_goes_to_api_key_step(fresh_home) -> None:
    """Regression guard for the skip path: providers WITHOUT a stored
    cred still land on the api_key prompt."""
    app = _FakeApp()
    screen = ProviderAuthScreen(app)
    _focus_provider(screen, "openai")
    asyncio.run(screen.handle_key("enter"))
    assert screen._step.kind == "api_key"


def test_quit_exits(fresh_home) -> None:
    app = _FakeApp()
    screen = ProviderAuthScreen(app)
    asyncio.run(screen.handle_key("q"))
    assert app.exited is True
