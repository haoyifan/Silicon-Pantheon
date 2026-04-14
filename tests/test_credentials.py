"""Tests for the credentials store."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from clash_of_odin.client.credentials import (
    Credentials,
    CredentialsError,
    ProviderCredential,
    load,
    resolve_key,
    save,
)


def test_load_returns_empty_when_file_missing(tmp_path: Path) -> None:
    c = load(tmp_path / "nope.json")
    assert c.default_provider is None
    assert c.providers == {}


def test_save_and_load_round_trip(tmp_path: Path) -> None:
    p = tmp_path / "c.json"
    c = Credentials(
        default_provider="anthropic",
        default_model="claude-sonnet-4-6",
        providers={
            "anthropic": ProviderCredential(auth_mode="subscription_cli"),
            "openai": ProviderCredential(
                auth_mode="api_key",
                key_ref="env:OPENAI_API_KEY",
            ),
        },
    )
    save(c, p)
    loaded = load(p)
    assert loaded.default_provider == "anthropic"
    assert loaded.providers["openai"].key_ref == "env:OPENAI_API_KEY"
    assert loaded.providers["anthropic"].auth_mode == "subscription_cli"


def test_save_writes_with_0600_perms(tmp_path: Path) -> None:
    p = tmp_path / "c.json"
    save(Credentials(), p)
    mode = stat.S_IMODE(p.stat().st_mode)
    assert mode == stat.S_IRUSR | stat.S_IWUSR


def test_schema_version_too_new_refuses(tmp_path: Path) -> None:
    p = tmp_path / "c.json"
    p.write_text('{"schema_version": 99, "providers": {}}', encoding="utf-8")
    with pytest.raises(CredentialsError):
        load(p)


def test_resolve_key_from_env(monkeypatch) -> None:
    monkeypatch.setenv("TEST_KEY_VAR", "sk-abcd")
    cred = ProviderCredential(auth_mode="api_key", key_ref="env:TEST_KEY_VAR")
    assert resolve_key(cred) == "sk-abcd"


def test_resolve_key_missing_env(monkeypatch) -> None:
    monkeypatch.delenv("TEST_KEY_VAR", raising=False)
    cred = ProviderCredential(auth_mode="api_key", key_ref="env:TEST_KEY_VAR")
    with pytest.raises(CredentialsError):
        resolve_key(cred)


def test_resolve_key_inline() -> None:
    cred = ProviderCredential(auth_mode="api_key", inline_key="sk-inline")
    assert resolve_key(cred) == "sk-inline"


def test_resolve_key_no_ref() -> None:
    cred = ProviderCredential(auth_mode="api_key")
    with pytest.raises(CredentialsError):
        resolve_key(cred)


def test_resolve_key_bad_scheme() -> None:
    cred = ProviderCredential(auth_mode="api_key", key_ref="weirdformat:x")
    with pytest.raises(CredentialsError):
        resolve_key(cred)
