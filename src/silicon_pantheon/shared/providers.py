"""Declarative catalog of LLM providers and models the game supports.

Each `ProviderSpec` is the data a client / TUI needs to offer a user
a choice: display name, auth mechanism, which env var holds the key,
the model list, and a token-cost warning banner. Keeping this in
`shared/` means a future server-side admin tool could read the same
catalog to, e.g., enforce an allow-list for tournament play.

Add a provider by appending to `PROVIDERS` and dropping a matching
`client/providers/<id>.py` adapter module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


AuthMode = Literal["api_key", "subscription_cli", "subscription_oauth", "none"]


@dataclass(frozen=True)
class ModelSpec:
    id: str
    display_name: str
    context_window: int
    supports_tools: bool = True
    cost_per_mtok_in: float | None = None
    cost_per_mtok_out: float | None = None


@dataclass(frozen=True)
class ProviderSpec:
    id: str
    display_name: str
    auth_mode: AuthMode
    env_var: str | None
    keyring_service: str
    models: list[ModelSpec]
    # Shown on the login screen's provider picker so the user
    # understands the cost / rate-limit posture before committing.
    token_cost_warning: str = ""
    # Optional OpenAI-compatible endpoint. When set, the OpenAI
    # adapter is reused with a custom base_url — lets us plug in
    # xAI, Groq, Together, DeepSeek, etc. without forking the SDK
    # adapter. None = use the provider's native SDK.
    openai_compatible_base_url: str | None = None


PROVIDERS: list[ProviderSpec] = [
    ProviderSpec(
        id="anthropic",
        display_name="Anthropic (Claude Agent SDK)",
        auth_mode="subscription_cli",
        env_var=None,
        keyring_service="silicon-pantheon-anthropic",
        models=[
            ModelSpec(
                "claude-opus-4-7",
                "Claude Opus 4.7",
                context_window=1_000_000,
                # Pricing mirrored from Opus 4.6 as a placeholder —
                # Anthropic hasn't always kept prices constant across
                # minor tier bumps. Verify against the published rate
                # card and update if this drifts.
                cost_per_mtok_in=15.0,
                cost_per_mtok_out=75.0,
            ),
            ModelSpec(
                "claude-opus-4-6",
                "Claude Opus 4.6",
                context_window=1_000_000,
                cost_per_mtok_in=15.0,
                cost_per_mtok_out=75.0,
            ),
            ModelSpec(
                "claude-sonnet-4-6",
                "Claude Sonnet 4.6",
                context_window=1_000_000,
                cost_per_mtok_in=3.0,
                cost_per_mtok_out=15.0,
            ),
            ModelSpec(
                "claude-haiku-4-5",
                "Claude Haiku 4.5",
                context_window=200_000,
                cost_per_mtok_in=1.0,
                cost_per_mtok_out=5.0,
            ),
        ],
        token_cost_warning=(
            "Uses your Claude Code subscription. Subject to Anthropic "
            "rate limits — heavy play may throttle briefly."
        ),
    ),
    ProviderSpec(
        id="openai-codex",
        display_name="OpenAI (ChatGPT subscription)",
        auth_mode="subscription_oauth",
        env_var=None,
        keyring_service="silicon-pantheon-openai-codex",
        models=[
            # Codex users get a 400K window on 5.5 (smaller than the
            # 1M the API-key path sees, per OpenAI's published limits).
            ModelSpec(
                "gpt-5.5",
                "GPT-5.5",
                context_window=400_000,
                cost_per_mtok_in=None,
                cost_per_mtok_out=None,
            ),
            ModelSpec(
                "gpt-5.5-reasoning-high",
                "GPT-5.5 (reasoning high)",
                context_window=400_000,
                cost_per_mtok_in=None,
                cost_per_mtok_out=None,
            ),
            ModelSpec(
                "gpt-5.4",
                "GPT-5.4",
                context_window=272_000,
                cost_per_mtok_in=None,
                cost_per_mtok_out=None,
            ),
            ModelSpec(
                "gpt-5.4-reasoning-high",
                "GPT-5.4 (reasoning high)",
                context_window=272_000,
                cost_per_mtok_in=None,
                cost_per_mtok_out=None,
            ),
        ],
        token_cost_warning=(
            "Uses your ChatGPT Plus / Pro / Business / Edu / Enterprise "
            "subscription via Codex OAuth — flat-rate, no per-token API "
            "billing. First use opens a browser to sign in."
        ),
    ),
    ProviderSpec(
        id="openai",
        display_name="OpenAI",
        auth_mode="api_key",
        env_var="OPENAI_API_KEY",
        keyring_service="silicon-pantheon-openai",
        models=[
            # API-key path gets the full 1M context window on 5.5.
            # Pricing per OpenAI's announcement: $5/$30 Mtok in/out.
            ModelSpec(
                "gpt-5.5",
                "GPT-5.5",
                context_window=1_000_000,
                cost_per_mtok_in=5.0,
                cost_per_mtok_out=30.0,
            ),
            # "pro" tier uses more compute per request — pricier but
            # produces more consistent answers on hard problems.
            ModelSpec(
                "gpt-5.5-pro",
                "GPT-5.5 Pro",
                context_window=1_000_000,
                cost_per_mtok_in=30.0,
                cost_per_mtok_out=180.0,
            ),
            ModelSpec(
                "gpt-5",
                "GPT-5",
                context_window=400_000,
                cost_per_mtok_in=10.0,
                cost_per_mtok_out=40.0,
            ),
            ModelSpec(
                "gpt-5-mini",
                "GPT-5 mini",
                context_window=400_000,
                cost_per_mtok_in=1.5,
                cost_per_mtok_out=6.0,
            ),
        ],
        token_cost_warning=(
            "Each match burns real API tokens. Make sure your account "
            "has budget — running out mid-match auto-concedes."
        ),
    ),
    ProviderSpec(
        id="xai",
        display_name="xAI (Grok)",
        auth_mode="api_key",
        env_var="XAI_API_KEY",
        keyring_service="silicon-pantheon-xai",
        models=[
            ModelSpec(
                "grok-4",
                "Grok 4",
                context_window=256_000,
                cost_per_mtok_in=3.0,
                cost_per_mtok_out=15.0,
            ),
            ModelSpec(
                "grok-3",
                "Grok 3",
                context_window=131_000,
                cost_per_mtok_in=2.0,
                cost_per_mtok_out=10.0,
            ),
            ModelSpec(
                "grok-3-mini",
                "Grok 3 Mini",
                context_window=131_000,
                cost_per_mtok_in=0.3,
                cost_per_mtok_out=0.5,
            ),
            ModelSpec(
                "grok-code-fast-1",
                "Grok Code Fast 1",
                context_window=256_000,
                cost_per_mtok_in=0.2,
                cost_per_mtok_out=1.5,
            ),
        ],
        # xAI is wire-compatible with the OpenAI Chat Completions API,
        # so the existing OpenAIAdapter serves it unchanged once we
        # point it at the Grok endpoint.
        openai_compatible_base_url="https://api.x.ai/v1",
        token_cost_warning=(
            "xAI API tokens. Get a key at console.x.ai — paste it here "
            "or set XAI_API_KEY. Running out mid-match auto-concedes."
        ),
    ),
    ProviderSpec(
        id="google",
        display_name="Google (Gemini)",
        auth_mode="api_key",
        env_var="GEMINI_API_KEY",
        keyring_service="silicon-pantheon-google",
        models=[
            ModelSpec(
                "gemini-2.5-pro",
                "Gemini 2.5 Pro",
                context_window=1_000_000,
                cost_per_mtok_in=1.25,
                cost_per_mtok_out=10.0,
            ),
            ModelSpec(
                "gemini-2.5-flash",
                "Gemini 2.5 Flash",
                context_window=1_000_000,
                cost_per_mtok_in=0.15,
                cost_per_mtok_out=0.60,
            ),
        ],
        openai_compatible_base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        token_cost_warning=(
            "Google AI Studio API tokens. Get a key at "
            "aistudio.google.com — paste it here or set GEMINI_API_KEY."
        ),
    ),
    ProviderSpec(
        id="qwen",
        display_name="Alibaba (Qwen)",
        auth_mode="api_key",
        env_var="DASHSCOPE_API_KEY",
        keyring_service="silicon-pantheon-qwen",
        models=[
            ModelSpec(
                "qwen-plus",
                "Qwen Plus",
                context_window=1_000_000,
                cost_per_mtok_in=0.20,
                cost_per_mtok_out=1.56,
            ),
            ModelSpec(
                "qwen-max",
                "Qwen Max",
                context_window=262_144,
                cost_per_mtok_in=0.78,
                cost_per_mtok_out=3.90,
            ),
            ModelSpec(
                "qwen-turbo",
                "Qwen Turbo",
                context_window=1_000_000,
                cost_per_mtok_in=0.05,
                cost_per_mtok_out=0.20,
            ),
        ],
        openai_compatible_base_url="https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        token_cost_warning=(
            "Alibaba Cloud DashScope API tokens. Get a key at "
            "bailian.console.alibabacloud.com — paste it here or "
            "set DASHSCOPE_API_KEY."
        ),
    ),
    ProviderSpec(
        id="deepseek",
        display_name="DeepSeek",
        auth_mode="api_key",
        env_var="DEEPSEEK_API_KEY",
        keyring_service="silicon-pantheon-deepseek",
        models=[
            ModelSpec(
                "deepseek-v4-flash",
                "DeepSeek V4 Flash",
                context_window=1_000_000,
                cost_per_mtok_in=0.14,
                cost_per_mtok_out=0.28,
            ),
            ModelSpec(
                "deepseek-v4-pro",
                "DeepSeek V4 Pro",
                context_window=1_000_000,
                cost_per_mtok_in=1.74,
                cost_per_mtok_out=3.48,
            ),
            ModelSpec(
                "deepseek-chat",
                "DeepSeek V3 (Chat)",
                context_window=64_000,
                cost_per_mtok_in=0.27,
                cost_per_mtok_out=1.10,
            ),
        ],
        openai_compatible_base_url="https://api.deepseek.com",
        token_cost_warning=(
            "DeepSeek API tokens. Get a key at platform.deepseek.com "
            "— paste it here or set DEEPSEEK_API_KEY."
        ),
    ),
    ProviderSpec(
        id="mistral",
        display_name="Mistral AI",
        auth_mode="api_key",
        env_var="MISTRAL_API_KEY",
        keyring_service="silicon-pantheon-mistral",
        models=[
            ModelSpec(
                "mistral-large-latest",
                "Mistral Large",
                context_window=128_000,
                cost_per_mtok_in=2.0,
                cost_per_mtok_out=6.0,
            ),
            ModelSpec(
                "codestral-latest",
                "Codestral",
                context_window=256_000,
                cost_per_mtok_in=0.30,
                cost_per_mtok_out=0.90,
            ),
            ModelSpec(
                "mistral-small-latest",
                "Mistral Small",
                context_window=32_000,
                cost_per_mtok_in=0.20,
                cost_per_mtok_out=0.60,
            ),
        ],
        openai_compatible_base_url="https://api.mistral.ai/v1",
        token_cost_warning=(
            "Mistral API tokens. Get a key at console.mistral.ai "
            "— paste it here or set MISTRAL_API_KEY."
        ),
    ),
    ProviderSpec(
        id="groq",
        display_name="Groq",
        auth_mode="api_key",
        env_var="GROQ_API_KEY",
        keyring_service="silicon-pantheon-groq",
        models=[
            ModelSpec(
                "llama-4-scout-17b-16e-instruct",
                "Llama 4 Scout",
                context_window=131_000,
                cost_per_mtok_in=0.11,
                cost_per_mtok_out=0.34,
            ),
            ModelSpec(
                "llama-3.3-70b-versatile",
                "Llama 3.3 70B",
                context_window=128_000,
                cost_per_mtok_in=0.59,
                cost_per_mtok_out=0.79,
            ),
            ModelSpec(
                "qwen-qwq-32b",
                "Qwen QwQ 32B",
                context_window=128_000,
                cost_per_mtok_in=0.29,
                cost_per_mtok_out=0.39,
            ),
        ],
        openai_compatible_base_url="https://api.groq.com/openai/v1",
        token_cost_warning=(
            "Groq API — extremely fast inference on open-source models. "
            "Get a key at console.groq.com — paste it here or set "
            "GROQ_API_KEY. Rate limits may be tight on free tier."
        ),
    ),
]


def get_provider(provider_id: str) -> ProviderSpec | None:
    """Lookup by id. Returns None for unknown providers."""
    for p in PROVIDERS:
        if p.id == provider_id:
            return p
    return None


def get_model(provider_id: str, model_id: str) -> ModelSpec | None:
    p = get_provider(provider_id)
    if p is None:
        return None
    for m in p.models:
        if m.id == model_id:
            return m
    return None
