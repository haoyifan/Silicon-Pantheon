"""Shared helpers for extracting chain-of-thought reasoning from LLM responses."""

from __future__ import annotations

from typing import Any


def _extract_reasoning(msg: Any) -> str | None:
    """Dig chain-of-thought text out of a chat-completions message.

    Providers disagree on where reasoning lives:
      - xAI Grok 3/4:       `reasoning_content`  (str)
      - OpenAI o-series:    `reasoning_content`  (str)
      - xAI (some builds):  `reasoning`          (str)
      - DeepSeek R1 etc.:   `reasoning`          (str)
      - Anthropic via OAI-compat: `thinking`     (str)

    Pydantic models in newer openai SDKs stash unknown fields in
    `model_extra`; older versions expose them as attributes directly.
    Walk both.
    """
    for name in ("reasoning_content", "reasoning", "thinking"):
        val = getattr(msg, name, None)
        if isinstance(val, str) and val.strip():
            return val
        # List-of-blocks form (rare but used by some OAI-compat proxies).
        if isinstance(val, list):
            parts = [
                b.get("text") if isinstance(b, dict) else str(b)
                for b in val
            ]
            joined = "\n".join(p for p in parts if p)
            if joined.strip():
                return joined
    extra = getattr(msg, "model_extra", None) or {}
    for name in ("reasoning_content", "reasoning", "thinking"):
        val = extra.get(name)
        if isinstance(val, str) and val.strip():
            return val
    return None
