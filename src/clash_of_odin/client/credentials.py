"""Credentials store for client-side provider selection.

Layout
------

    ~/.clash-of-odin/credentials.json

    {
      "schema_version": 1,
      "default_provider": "anthropic",
      "default_model": "claude-sonnet-4-6",
      "providers": {
        "anthropic": {"auth_mode": "subscription_cli"},
        "openai": {
          "auth_mode": "api_key",
          "key_ref": "env:OPENAI_API_KEY"    or    "keyring:clash-of-odin-openai/default"
        }
      }
    }

Secret material is **not** stored inline. `key_ref` resolves at call
time — env vars take one shape, OS keyring takes another. Inline
`"key": "sk-..."` is also tolerated for single-user developer use
(warns at save time, written with 0600 perms) but the recommended
path stores refs.
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path

SCHEMA_VERSION = 1

DEFAULT_PATH = Path.home() / ".clash-of-odin" / "credentials.json"


class CredentialsError(RuntimeError):
    """Raised when a credential cannot be resolved (missing env var,
    missing keyring entry, malformed key_ref)."""


@dataclass
class ProviderCredential:
    """One provider's stored credential.

    Exactly one of `inline_key` / `key_ref` is set for api_key mode;
    subscription_cli / none modes have both empty.
    """

    auth_mode: str
    # If set, an "env:VAR" or "keyring:service/user" string.
    key_ref: str | None = None
    # Plaintext key — only for developer-friendly single-user setups.
    # File is 0600; we warn when writing this form.
    inline_key: str | None = None
    # Free-form extras (e.g. base_url for Ollama).
    extras: dict[str, str] = field(default_factory=dict)


@dataclass
class Credentials:
    default_provider: str | None = None
    default_model: str | None = None
    providers: dict[str, ProviderCredential] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "schema_version": SCHEMA_VERSION,
            "default_provider": self.default_provider,
            "default_model": self.default_model,
            "providers": {
                pid: {
                    "auth_mode": pc.auth_mode,
                    **({"key_ref": pc.key_ref} if pc.key_ref else {}),
                    **({"key": pc.inline_key} if pc.inline_key else {}),
                    **({"extras": pc.extras} if pc.extras else {}),
                }
                for pid, pc in self.providers.items()
            },
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Credentials":
        version = int(d.get("schema_version", SCHEMA_VERSION))
        if version > SCHEMA_VERSION:
            raise CredentialsError(
                f"credentials file declares schema_version={version}; "
                f"this client supports {SCHEMA_VERSION}."
            )
        providers: dict[str, ProviderCredential] = {}
        for pid, entry in (d.get("providers") or {}).items():
            if not isinstance(entry, dict):
                continue
            providers[pid] = ProviderCredential(
                auth_mode=str(entry.get("auth_mode", "api_key")),
                key_ref=entry.get("key_ref"),
                inline_key=entry.get("key"),
                extras=dict(entry.get("extras") or {}),
            )
        return cls(
            default_provider=d.get("default_provider"),
            default_model=d.get("default_model"),
            providers=providers,
        )


# ---- file I/O ----


def load(path: Path | None = None) -> Credentials:
    """Return the stored Credentials, or a fresh empty instance if
    the file doesn't exist."""
    p = path or DEFAULT_PATH
    if not p.exists():
        return Credentials()
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise CredentialsError(f"credentials file at {p} is not valid JSON: {e}")
    return Credentials.from_dict(d)


def save(creds: Credentials, path: Path | None = None) -> Path:
    """Write the credentials file with 0600 perms."""
    p = path or DEFAULT_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(creds.to_dict(), indent=2), encoding="utf-8")
    # 0600 regardless of umask.
    os.chmod(p, stat.S_IRUSR | stat.S_IWUSR)
    return p


# ---- key resolution ----


def resolve_key(cred: ProviderCredential) -> str:
    """Return the actual API key for a credential, or raise.

    Accepts four paths:
      - inline_key   (dev-only; returned as-is)
      - key_ref = "env:VAR"         → os.environ[VAR]
      - key_ref = "keyring:service/user"  → keyring.get_password(...)
      - raw env var in the provider's `env_var` field  (handled by
        a higher-level helper; this fn only knows about the cred itself)
    """
    if cred.inline_key:
        return cred.inline_key
    if cred.key_ref is None:
        raise CredentialsError("no key_ref and no inline key on credential")

    ref = cred.key_ref
    if ref.startswith("env:"):
        var = ref.removeprefix("env:")
        value = os.environ.get(var)
        if not value:
            raise CredentialsError(
                f"environment variable {var} not set (referenced by credential)"
            )
        return value

    if ref.startswith("keyring:"):
        target = ref.removeprefix("keyring:")
        if "/" not in target:
            raise CredentialsError(
                f"malformed keyring ref {ref!r}; expected 'keyring:service/user'"
            )
        service, user = target.split("/", 1)
        try:
            import keyring  # type: ignore[import-not-found]
        except ImportError:
            raise CredentialsError(
                "keyring package not installed; either install "
                "`clash-of-odin[keyring]` or migrate to an env-var ref"
            )
        value = keyring.get_password(service, user)
        if not value:
            raise CredentialsError(
                f"no keyring entry for service={service!r} user={user!r}"
            )
        return value

    raise CredentialsError(f"unrecognized key_ref scheme: {ref!r}")
