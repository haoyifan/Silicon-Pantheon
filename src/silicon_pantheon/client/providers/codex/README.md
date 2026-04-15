# Codex (ChatGPT subscription) provider

Lets users with a ChatGPT Plus / Pro / Business / Edu / Enterprise
subscription play SiliconPantheon **without paying per-token API
fees** by talking directly to OpenAI's Codex backend (the one
`@openai/codex` CLI uses) over a PKCE-OAuth bearer token.

This subpackage is **isolated**:

  - all OAuth + transport code lives here
  - the only external touchpoints are the two integration shims in
    `agent_bridge._build_default_adapter` and the provider catalog
    in `shared/providers.py`
  - replacing or removing this provider doesn't touch any other
    file in the project

## Files

  - `oauth.py`          PKCE flow, browser handoff, callback listener,
                        `~/.silicon-pantheon/credentials/codex-oauth.json`
                        token storage, refresh-with-file-lock
  - `responses_api.py`  shape conversion between our tool-spec /
                        chat-completions style and OpenAI's
                        Responses API request/response schema
  - `adapter.py`        `ProviderAdapter` implementation; talks to
                        `chatgpt.com/backend-api/codex/responses`
                        with the OAuth bearer
  - `catalog.py`        models exposed via the codex subscription
                        (gpt-5-codex, gpt-5-codex-mini, …)
  - `__init__.py`       public re-exports for the integration shim

## What this is NOT

  - Not a fork of `openai.py`. The Responses API differs from Chat
    Completions in shape, streaming protocol, and tool schema.
  - Not a wrapper around the `codex` CLI binary. We speak the same
    backend protocol directly — no Node, no extra install.

## Risk profile (read this before relying on it for real money)

  - The endpoint `chatgpt.com/backend-api/codex/responses` is
    **NOT a publicly documented OpenAI API**. It works because it's
    the same endpoint the official codex CLI uses. OpenAI could
    change the schema, require additional headers, or block
    third-party clients at any time.
  - Anthropic shut down the equivalent flow for Claude in April 2026
    citing subsidy economics. OpenAI could follow.
  - Bug reports in OpenClaw's tracker show the schema drifts
    occasionally (e.g., `responses` vs `codex-responses`,
    Cloudflare 403s after upgrades). Expect periodic fixups.

If any of these become a real outage, fall back to the API-key
path (already supported in `providers/openai.py`).

## OAuth constants

Sourced from the publicly-readable codex CLI source (Apache-2.0
licensed, github.com/openai/codex):

  - client_id: `app_EMoamEEZ73f0CkXaXp7hrann`
  - authorize: `https://auth.openai.com/oauth/authorize`
  - token:     `https://auth.openai.com/oauth/token`
  - scope:     `openid profile email offline_access`

These are the same constants the codex CLI ships with — no
reverse engineering of secret values, just using OpenAI's public
client identity.
