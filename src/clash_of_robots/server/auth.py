"""Opaque-token registry for per-match authorization.

Phase 1 uses a plain in-memory dict keyed by randomly generated opaque
strings. Each token maps to a `TokenIdentity(room_id, slot)`. The
registry is the *only* place tool handlers consult to resolve
"which player is this request coming from" — swap the implementation
for JWT or OAuth later without touching handlers.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass
from threading import Lock


@dataclass(frozen=True)
class TokenIdentity:
    """Who a token resolves to."""

    room_id: str
    slot: str  # "a" or "b"


class TokenRegistry:
    """Thread-safe in-memory token store with optional expiry.

    Tokens are 32-byte hex strings (effectively unforgeable for Phase 1
    threat model). Expiry is a wall-clock Unix timestamp; None means
    no expiry until `revoke` is called.
    """

    def __init__(self) -> None:
        self._lock = Lock()
        self._tokens: dict[str, tuple[TokenIdentity, float | None]] = {}

    def issue(
        self,
        identity: TokenIdentity,
        *,
        ttl_seconds: float | None = None,
    ) -> str:
        token = secrets.token_hex(32)
        expires_at = (time.time() + ttl_seconds) if ttl_seconds else None
        with self._lock:
            self._tokens[token] = (identity, expires_at)
        return token

    def resolve(self, token: str) -> TokenIdentity | None:
        """Return the identity for a token, or None if missing/expired.

        Expired tokens are purged on first post-expiry resolve.
        """
        if not token:
            return None
        with self._lock:
            entry = self._tokens.get(token)
            if entry is None:
                return None
            identity, expires_at = entry
            if expires_at is not None and time.time() >= expires_at:
                self._tokens.pop(token, None)
                return None
            return identity

    def revoke(self, token: str) -> bool:
        """Invalidate a token. Returns True if it existed, False otherwise."""
        with self._lock:
            return self._tokens.pop(token, None) is not None

    def revoke_all_for(self, *, room_id: str) -> int:
        """Drop every token pointing at a given room. Returns count revoked."""
        with self._lock:
            stale = [t for t, (ident, _) in self._tokens.items() if ident.room_id == room_id]
            for t in stale:
                self._tokens.pop(t, None)
            return len(stale)

    def set_ttl(self, token: str, ttl_seconds: float) -> bool:
        """Replace a token's expiry. Returns True on success."""
        with self._lock:
            entry = self._tokens.get(token)
            if entry is None:
                return None  # type: ignore[return-value]
            identity, _ = entry
            self._tokens[token] = (identity, time.time() + ttl_seconds)
            return True

    def __len__(self) -> int:
        with self._lock:
            return len(self._tokens)
