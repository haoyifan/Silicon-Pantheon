"""Provider registry and factory."""

from __future__ import annotations

from .base import Provider
from .random import RandomProvider


def make_provider(spec: str, **kwargs) -> Provider:
    """Instantiate a provider from a spec string.

    Known specs:
      - "random" — RandomProvider
      - "claude-opus-4-6", "claude-sonnet-4-6", etc. — Claude via Agent SDK (Phase 5)
      - "gpt-*" — OpenAI (Phase 7)

    Unknown kwargs are filtered so each provider only sees what it expects.
    """
    spec = spec.strip()
    if spec == "random":
        seed = kwargs.get("seed")
        return RandomProvider(seed=seed)
    if spec.startswith("claude"):
        from .anthropic import AnthropicProvider  # local import; Phase 5

        return AnthropicProvider(
            model=spec,
            **{
                k: v
                for k, v in kwargs.items()
                if k in {"strategy_path", "token_budget", "time_budget"}
            },
        )
    if spec.startswith("gpt"):
        from .openai import OpenAIProvider  # Phase 7

        return OpenAIProvider(model=spec, **kwargs)
    raise ValueError(f"unknown provider spec: {spec!r}")


__all__ = ["Provider", "RandomProvider", "make_provider"]
