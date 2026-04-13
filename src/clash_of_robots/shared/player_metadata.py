"""Self-declared player metadata.

Sent by the client immediately after connecting; stored on the
connection and surfaced in room previews and the replay's match_start
event. No validation — if a client lies about being an LLM, it lies.
Later auth / attestation layers attach to this without a schema change.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal

PlayerKind = Literal["ai", "human", "hybrid"]

VALID_KINDS: frozenset[str] = frozenset(("ai", "human", "hybrid"))


@dataclass(frozen=True)
class PlayerMetadata:
    display_name: str
    kind: PlayerKind
    provider: str | None = None  # e.g. "anthropic"
    model: str | None = None  # e.g. "claude-opus-4-6"
    version: str = "1"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "PlayerMetadata":
        display_name = str(raw.get("display_name", "")).strip()
        if not display_name:
            raise ValueError("player metadata: display_name is required")
        kind = str(raw.get("kind", "")).strip().lower()
        if kind not in VALID_KINDS:
            raise ValueError(
                f"player metadata: kind must be one of {sorted(VALID_KINDS)}, got {kind!r}"
            )
        provider = raw.get("provider")
        model = raw.get("model")
        version = str(raw.get("version", "1"))
        return cls(
            display_name=display_name,
            kind=kind,  # type: ignore[arg-type]
            provider=str(provider) if provider else None,
            model=str(model) if model else None,
            version=version,
        )
