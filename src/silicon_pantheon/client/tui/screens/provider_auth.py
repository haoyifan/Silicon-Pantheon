"""Provider / model picker screen with API-key entry.

Sits between the login screen (where the user typed their display
name + server URL) and the lobby (where they see rooms). Reads
credentials.json; if a default provider+model pair is saved, offers
a one-line "use these?" shortcut. Otherwise walks the user through:

  1) pick a provider from the catalog
  2) if api-key mode: type / paste the key (or confirm env var), offer keyring save
  3) if subscription_cli mode: we check `claude --version`
  4) pick a model from that provider's list

On success, writes back to credentials.json and sets
app.state.provider / app.state.model so the rest of the session
builds the right adapter.

Navigation mirrors the RoomScreen pattern: button list with focus,
Enter to activate, Esc cancels to the previous step.
"""

from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass, field
from typing import Any

from rich.align import Align
from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.text import Text

from silicon_pantheon.client.credentials import (
    Credentials,
    CredentialsError,
    ProviderCredential,
    load,
    resolve_key,
    save,
)
from silicon_pantheon.client.tui.app import Screen, TUIApp
from silicon_pantheon.shared.providers import PROVIDERS, ProviderSpec, get_provider

log = logging.getLogger("silicon.tui.provider_auth")


async def _validate_api_key(provider_id: str, api_key: str) -> str | None:
    """Ping the provider's /v1/models endpoint with the key. Returns
    None on success, or a short error string on failure.

    Works for every OpenAI-compatible api-key provider we ship
    (OpenAI itself, xAI via the base_url in the catalog). Anthropic
    and any future subscription_cli providers never reach this path
    because they don't use a key.

    We use the OpenAI SDK rather than raw HTTP so the list of
    base-URL / timeout / transport quirks stays identical to what
    the adapter uses at match time — if models.list succeeds here,
    the agent's chat.completions.create won't fail on auth.
    """
    spec = get_provider(provider_id)
    if spec is None or spec.auth_mode != "api_key":
        return "unknown provider"
    if not api_key.strip():
        return "empty key"
    try:
        from openai import AsyncOpenAI
    except ImportError:
        return "openai SDK not installed"
    client = AsyncOpenAI(
        api_key=api_key,
        base_url=spec.openai_compatible_base_url,
        timeout=10.0,
    )
    try:
        await client.models.list()
        return None
    except Exception as e:  # noqa: BLE001 — surface anything to the user
        msg = str(e) or type(e).__name__
        # Many "Authentication" errors from openai-python have a
        # long prefix; keep the banner readable.
        if len(msg) > 160:
            msg = msg[:157] + "…"
        return msg
    finally:
        try:
            await client.close()
        except Exception:
            pass


@dataclass
class _Step:
    """Lightweight state machine for the picker."""

    kind: str
    # Steps in order of the auth flow:
    #   resume         initial shortcut: "Using saved defaults ..."
    #   pick_provider  pick which LLM provider to use
    #   confirm_auth   saved credential found — re-auth yes/no
    #   api_key        enter / paste / validate a new key
    #   pick_model     pick a model from the chosen provider
    provider_id: str | None = None
    model_id: str | None = None
    # For api_key entry.
    key_buffer: str = ""
    key_source_hint: str = ""
    focused: int = 0


_API_KEY_OPTIONS = ("use_env", "paste", "save_to_keyring_after_paste")


class ProviderAuthScreen(Screen):
    def __init__(self, app: TUIApp):
        self.app = app
        self._creds: Credentials = load()
        self._step: _Step = self._initial_step()

    def _initial_step(self) -> _Step:
        """Shortcut to 'resume' if credentials already have a default."""
        creds = self._creds
        if creds.default_provider and creds.default_model:
            return _Step(
                kind="resume",
                provider_id=creds.default_provider,
                model_id=creds.default_model,
            )
        return _Step(kind="pick_provider", focused=0)

    # ---- render ----

    def render(self) -> RenderableType:
        if self._step.kind == "resume":
            return self._render_resume()
        if self._step.kind == "pick_provider":
            return self._render_pick_provider()
        if self._step.kind == "confirm_auth":
            return self._render_confirm_auth()
        if self._step.kind == "api_key":
            return self._render_api_key()
        if self._step.kind == "pick_model":
            return self._render_pick_model()
        return Text("(provider-auth: unknown step)", style="red")

    def _render_resume(self) -> RenderableType:
        p = get_provider(self._step.provider_id or "")
        provider_label = p.display_name if p else (self._step.provider_id or "?")
        body = Group(
            Text("Pick LLM provider & model", style="bold yellow"),
            Text(""),
            Text(
                f"Using saved defaults: {provider_label} / {self._step.model_id}",
                style="green",
            ),
            Text(""),
            Text("[Enter] continue   [c] change   [q] quit", style="dim"),
        )
        return Align.center(
            Panel(body, title="provider", border_style="yellow", padding=(1, 3)),
            vertical="middle",
        )

    def _render_pick_provider(self) -> RenderableType:
        lines: list[Text] = [Text("Pick LLM provider", style="bold yellow"), Text("")]
        for i, p in enumerate(PROVIDERS):
            marker = "➤ " if i == self._step.focused else "  "
            style = "bold cyan" if i == self._step.focused else "white"
            lines.append(Text(f"{marker}{p.display_name}", style=style))
            lines.append(
                Text(f"      {p.token_cost_warning}", style="dim italic")
            )
        lines.append(Text(""))
        lines.append(Text("↑/k ↓/j navigate   Enter pick   q quit", style="dim"))
        if self.app.state.error_message:
            lines.append(Text(self.app.state.error_message, style="red"))
        return Align.center(
            Panel(Group(*lines), title="provider", border_style="yellow", padding=(1, 3)),
            vertical="middle",
        )

    def _render_confirm_auth(self) -> RenderableType:
        """Two-choice screen shown when the picked provider already
        has stored credentials. Default (focused=0) keeps the saved
        auth; focused=1 rotates it.

        Keeping the saved auth advances straight to the model step
        (or, if a default model is also saved, straight to apply for
        a one-Enter fast path). Re-auth drops into api_key paste
        mode with the paste row pre-focused."""
        p = get_provider(self._step.provider_id or "")
        provider_label = p.display_name if p else (self._step.provider_id or "?")
        cred = self._creds.providers.get(self._step.provider_id or "")
        auth_mode = cred.auth_mode if cred else "?"
        if auth_mode == "subscription_cli":
            summary = f"Stored auth: {provider_label} CLI subscription"
            re_label = "Re-authenticate (run claude login again)"
        else:
            # Show a short tail of the resolved key so the user can
            # tell at a glance which saved key they're about to use —
            # useful if they've rotated between keys recently.
            tail = "…"
            try:
                k = resolve_key(cred) if cred else ""
                if k:
                    tail = "…" + k[-4:]
            except CredentialsError:
                tail = "(unresolvable)"
            summary = f"Stored key: {provider_label}  {tail}"
            re_label = "Enter a different key (validate + save)"
        options = [
            ("keep", "Use the saved credentials"),
            ("reauth", re_label),
        ]
        lines: list[Text] = [
            Text(f"{provider_label} — authentication", style="bold yellow"),
            Text(""),
            Text(summary, style="green"),
            Text(""),
        ]
        for i, (_opt, label) in enumerate(options):
            marker = "➤ " if i == self._step.focused else "  "
            style = "bold cyan" if i == self._step.focused else "white"
            lines.append(Text(f"{marker}{label}", style=style))
        lines.append(Text(""))
        lines.append(
            Text(
                "↑/k ↓/j switch   Enter confirm   Esc back   q quit",
                style="dim",
            )
        )
        if self.app.state.error_message:
            lines.append(Text(self.app.state.error_message, style="red"))
        return Align.center(
            Panel(Group(*lines), title="auth", border_style="yellow", padding=(1, 3)),
            vertical="middle",
        )

    def _render_api_key(self) -> RenderableType:
        p = get_provider(self._step.provider_id or "")
        env_var = p.env_var if p else None
        env_present = bool(env_var and os.environ.get(env_var))
        options = [
            (
                "use_env",
                f"Use {env_var} (detected)" if env_present else f"Use {env_var} (not set)",
                env_present,
            ),
            ("paste", "Paste an API key (saves to keyring)", True),
        ]
        lines: list[Text] = [
            Text(
                f"{p.display_name if p else '?'} — auth",
                style="bold yellow",
            ),
            Text(""),
        ]
        for i, (_opt, label, enabled) in enumerate(options):
            marker = "➤ " if i == self._step.focused else "  "
            if not enabled:
                style = "dim strike" if i == self._step.focused else "dim"
            elif i == self._step.focused:
                style = "bold cyan"
            else:
                style = "white"
            lines.append(Text(f"{marker}{label}", style=style))
        if self._step.focused == 1:  # paste mode
            lines.append(Text(""))
            n = len(self._step.key_buffer)
            # Show a fixed-width mask so huge pastes don't blow out
            # the modal, plus a live char count and a head/tail peek
            # so the user can confirm their paste actually landed.
            if n == 0:
                mask = ""
            elif n <= 12:
                mask = "*" * n
            else:
                head = self._step.key_buffer[:4]
                tail = self._step.key_buffer[-4:]
                mask = f"{head}{'*' * (n - 8)}{tail}"
                # Collapse the stars if there are a lot of them.
                if n > 40:
                    mask = f"{head}{'*' * 32}…{tail}"
            lines.append(
                Text.assemble(
                    ("key: ", "yellow bold"),
                    (mask, "white"),
                    ("▌", "yellow"),
                    (f"   ({n} chars)", "dim"),
                )
            )
            lines.append(
                Text(
                    "(paste or type the key, Enter to validate + save, Esc cancels)",
                    style="dim italic",
                )
            )
        lines.append(Text(""))
        lines.append(
            Text(
                "↑/k ↓/j switch option   Enter confirm   Esc back   q quit",
                style="dim",
            )
        )
        if self.app.state.error_message:
            lines.append(Text(self.app.state.error_message, style="red"))
        return Align.center(
            Panel(Group(*lines), title="auth", border_style="yellow", padding=(1, 3)),
            vertical="middle",
        )

    def _render_pick_model(self) -> RenderableType:
        p = get_provider(self._step.provider_id or "")
        if p is None:
            return Text("(missing provider)", style="red")
        lines: list[Text] = [
            Text(f"{p.display_name} — pick model", style="bold yellow"),
            Text(""),
        ]
        for i, m in enumerate(p.models):
            marker = "➤ " if i == self._step.focused else "  "
            style = "bold cyan" if i == self._step.focused else "white"
            cost = ""
            if m.cost_per_mtok_in is not None:
                cost = f" — ${m.cost_per_mtok_in}/${m.cost_per_mtok_out} per MTok"
            lines.append(
                Text(
                    f"{marker}{m.display_name} ({m.id}){cost}",
                    style=style,
                )
            )
        lines.append(Text(""))
        # Show the [r] rotate-key hint only when it's actually
        # actionable (api-key providers with a saved credential).
        hint = "↑/k ↓/j navigate   Enter pick   Esc back   q quit"
        if p.auth_mode == "api_key" and self._has_usable_cred(p.id):
            hint = (
                "↑/k ↓/j navigate   Enter pick   r re-enter key   "
                "Esc back   q quit"
            )
        lines.append(Text(hint, style="dim"))
        if self.app.state.error_message:
            lines.append(Text(self.app.state.error_message, style="red"))
        return Align.center(
            Panel(Group(*lines), title="model", border_style="yellow", padding=(1, 3)),
            vertical="middle",
        )

    # ---- input ----

    async def handle_key(self, key: str) -> Screen | None:
        # Don't treat 'q' as quit while the user is typing / pasting
        # an API key — a literal 'q' in the key would otherwise exit
        # the app mid-paste. (The terminal-side bracketed-paste wrap
        # usually delivers paste as a single `paste:...` event, but
        # terminals without mode ?2004 fall back to per-char input,
        # and this guard is what keeps that path safe too.)
        in_paste = (
            self._step.kind == "api_key" and self._step.focused == 1
        )
        if key == "q" and not in_paste:
            self.app.exit()
            return None
        if self._step.kind == "resume":
            return await self._handle_resume_key(key)
        if self._step.kind == "pick_provider":
            return await self._handle_pick_provider_key(key)
        if self._step.kind == "confirm_auth":
            return self._handle_confirm_auth_key(key)
        if self._step.kind == "api_key":
            return await self._handle_api_key_key(key)
        if self._step.kind == "pick_model":
            return await self._handle_pick_model_key(key)
        return None

    async def _handle_resume_key(self, key: str) -> Screen | None:
        if key == "enter":
            return await self._apply_selection(
                self._step.provider_id or "", self._step.model_id or ""
            )
        if key == "c":
            self._step = _Step(kind="pick_provider", focused=0)
            return None
        return None

    async def _handle_pick_provider_key(self, key: str) -> Screen | None:
        if key in ("down", "j"):
            self._step.focused = (self._step.focused + 1) % len(PROVIDERS)
        elif key in ("up", "k"):
            self._step.focused = (self._step.focused - 1) % len(PROVIDERS)
        elif key == "enter":
            p = PROVIDERS[self._step.focused]
            # Branch on stored-credential state:
            #   - any provider with a stored creds → confirm_auth step
            #     (keep / rotate)
            #   - no credential, api_key mode → paste step
            #   - no credential, subscription_oauth → run OAuth login
            #     before reaching the model picker
            #   - no credential, subscription_cli → straight to model
            #     picker (validation happens in _apply via shutil.which)
            if self._has_usable_cred(p.id):
                next_kind = "confirm_auth"
                focused = 0
            elif p.auth_mode == "api_key":
                next_kind = "api_key"
                focused = 0
            elif p.auth_mode == "subscription_oauth":
                # OAuth flow needs to actually run — return a coroutine
                # to the dispatcher to await, then advance.
                return await self._run_oauth_login(p.id)
            else:
                next_kind = "pick_model"
                focused = 0
            self._step = _Step(
                kind=next_kind,
                provider_id=p.id,
                focused=focused,
            )
            self.app.state.error_message = ""
        return None

    async def _run_oauth_login(self, provider_id: str) -> Screen | None:
        """Trigger PKCE browser login for a subscription_oauth provider.

        Currently only used by the openai-codex provider. Blocks the
        TUI for the duration of the OAuth flow (typically 10-30 s)
        which is fine — the user is in the browser anyway.
        """
        if provider_id != "openai-codex":
            self.app.state.error_message = (
                f"unsupported subscription_oauth provider: {provider_id}"
            )
            return None
        from silicon_pantheon.client.providers.codex import (
            CodexAuthError,
            login_interactive,
        )

        self.app.state.error_message = (
            "opening browser to sign in to ChatGPT…"
        )
        try:
            await login_interactive()
        except CodexAuthError as e:
            self.app.state.error_message = f"Codex login failed: {e}"
            return None
        except Exception as e:  # defensive
            self.app.state.error_message = f"Codex login error: {e}"
            return None
        # Mirror what _save_api_key_then_pick_model does for api-key:
        # write a credential pointer so the resume / has_usable_cred
        # path picks it up next time.
        self._creds.providers[provider_id] = ProviderCredential(
            auth_mode="subscription_oauth",
        )
        save(self._creds)
        self._step = _Step(
            kind="pick_model", provider_id=provider_id, focused=0,
        )
        self.app.state.error_message = ""
        return None

    def _handle_confirm_auth_key(self, key: str) -> Screen | None:
        p = get_provider(self._step.provider_id or "")
        if p is None:
            return None
        if key == "esc":
            self._step = _Step(kind="pick_provider", focused=0)
            return None
        if key in ("down", "j"):
            self._step.focused = (self._step.focused + 1) % 2
            return None
        if key in ("up", "k"):
            self._step.focused = (self._step.focused - 1) % 2
            return None
        if key == "enter":
            if self._step.focused == 0:
                # Keep saved credentials → advance to model selection,
                # pre-focused on the stored default model if any (so
                # the user can one-Enter through, or arrow to change).
                focused = 0
                if self._creds.default_model:
                    try:
                        focused = [m.id for m in p.models].index(
                            self._creds.default_model
                        )
                    except ValueError:
                        focused = 0
                self._step = _Step(
                    kind="pick_model",
                    provider_id=p.id,
                    focused=focused,
                )
                self.app.state.error_message = ""
                return None
            # Re-auth: drop into the paste step with the paste row
            # pre-focused (the common case; the env-var row is still
            # one arrow-key away).
            if p.auth_mode == "api_key":
                self._step = _Step(
                    kind="api_key",
                    provider_id=p.id,
                    focused=1,
                )
            else:
                # subscription_cli: re-apply re-checks `claude --version`
                # and refreshes the stored auth record.
                self._step = _Step(
                    kind="pick_model", provider_id=p.id, focused=0
                )
            self.app.state.error_message = ""
            return None
        return None

    def _has_usable_cred(self, provider_id: str) -> bool:
        """True if credentials.json already records an auth for this
        provider that we can act on. For api_key providers that means
        a resolvable key; for subscription_cli it means an
        acknowledged auth record exists (the runtime shutil.which
        check happens later at apply time).

        We intentionally do NOT re-validate against the server here —
        that would block the UI on a network round-trip every time
        the user scrolls through providers. Stored+resolvable is
        good enough; if auth fails later the agent surfaces the
        error and the user can press Esc to back out and re-auth."""
        cred = self._creds.providers.get(provider_id)
        if cred is None:
            return False
        if cred.auth_mode == "subscription_cli":
            return True
        if cred.auth_mode == "subscription_oauth":
            # OAuth creds live in their own file (~/.silicon-pantheon/
            # credentials/codex-oauth.json), not credentials.json. Check
            # there for a usable token.
            if provider_id == "openai-codex":
                from silicon_pantheon.client.providers.codex import (
                    load_credentials as _load_codex,
                )
                return _load_codex() is not None
            return False
        try:
            key = resolve_key(cred)
        except CredentialsError:
            return False
        return bool(key)

    async def _handle_api_key_key(self, key: str) -> Screen | None:
        if key == "esc":
            self._step = _Step(kind="pick_provider", focused=0)
            return None
        # When in paste mode, the buffer absorbs printable chars.
        in_paste = self._step.focused == 1
        # Bracketed-paste delivers the whole clipboard as a single
        # `paste:<content>` event. Dump the content into the buffer
        # verbatim (filtering newlines / control bytes — a pasted
        # key never legitimately contains these) regardless of which
        # focus row we're on; that way the user doesn't have to hit
        # "paste key" first.
        if key.startswith("paste:"):
            pasted = key[len("paste:"):]
            clean = "".join(
                c for c in pasted if c.isprintable() and c not in ("\r", "\n")
            )
            if clean:
                # Auto-switch to the paste row if they pasted from the
                # env-var row so the key lands visibly.
                if self._step.focused != 1:
                    self._step.focused = 1
                self._step.key_buffer += clean
            return None
        if in_paste and key == "enter":
            if not self._step.key_buffer:
                self.app.state.error_message = "key is empty"
                return None
            return await self._save_api_key_then_pick_model()
        if in_paste and key == "backspace":
            self._step.key_buffer = self._step.key_buffer[:-1]
            return None
        if in_paste and len(key) == 1 and key.isprintable():
            self._step.key_buffer += key
            return None
        if key in ("down", "j"):
            self._step.focused = (self._step.focused + 1) % 2
            self._step.key_buffer = ""
            return None
        if key in ("up", "k"):
            self._step.focused = (self._step.focused - 1) % 2
            self._step.key_buffer = ""
            return None
        if key == "enter" and self._step.focused == 0:
            # Use env var.
            p = get_provider(self._step.provider_id or "")
            if p is None or not p.env_var:
                self.app.state.error_message = "provider has no env var"
                return None
            if not os.environ.get(p.env_var):
                self.app.state.error_message = f"{p.env_var} not set; use the paste option"
                return None
            cred = ProviderCredential(
                auth_mode="api_key", key_ref=f"env:{p.env_var}"
            )
            self._creds.providers[p.id] = cred
            # Verify now so bad envs don't leak to play-time.
            try:
                resolve_key(cred)
            except CredentialsError as e:
                self.app.state.error_message = str(e)
                return None
            self._step = _Step(
                kind="pick_model", provider_id=p.id, focused=0
            )
            self.app.state.error_message = ""
            return None
        return None

    async def _save_api_key_then_pick_model(self) -> Screen | None:
        p = get_provider(self._step.provider_id or "")
        if p is None:
            self.app.state.error_message = "provider missing"
            return None
        key = self._step.key_buffer
        # Validate the key against the provider BEFORE persisting —
        # saves users from a silent failure when the first game starts
        # and the agent can't authenticate.
        self.app.state.error_message = "validating key with provider…"
        err = await _validate_api_key(p.id, key)
        if err:
            self.app.state.error_message = f"key rejected: {err}"
            # Keep the user in paste mode with their buffer intact so
            # they can retry without retyping.
            return None
        try:
            import keyring  # type: ignore[import-not-found]

            keyring.set_password(p.keyring_service, "default", key)
            cred = ProviderCredential(
                auth_mode="api_key",
                key_ref=f"keyring:{p.keyring_service}/default",
            )
        except ImportError:
            # Fall back to inline storage with a prominent warning.
            log.warning(
                "keyring not available — inlining key in credentials.json "
                "(consider installing silicon-pantheon[keyring])"
            )
            cred = ProviderCredential(auth_mode="api_key", inline_key=key)
        except Exception as e:
            self.app.state.error_message = f"keyring save failed: {e}"
            return None
        self._creds.providers[p.id] = cred
        self._step = _Step(kind="pick_model", provider_id=p.id, focused=0)
        self.app.state.error_message = ""
        return None

    async def _handle_pick_model_key(self, key: str) -> Screen | None:
        p = get_provider(self._step.provider_id or "")
        if p is None:
            return None
        if key == "esc":
            self._step = _Step(kind="pick_provider", focused=0)
            return None
        if key in ("down", "j"):
            self._step.focused = (self._step.focused + 1) % len(p.models)
        elif key in ("up", "k"):
            self._step.focused = (self._step.focused - 1) % len(p.models)
        elif key == "enter":
            m = p.models[self._step.focused]
            return await self._apply_selection(p.id, m.id)
        elif key == "r" and p.auth_mode == "api_key":
            # Explicit key rotation: jump back to the paste step even
            # though a saved credential already exists. Defaults to
            # the paste row (focused=1) since that's what rotation
            # actually is.
            self._step = _Step(
                kind="api_key", provider_id=p.id, focused=1
            )
            self.app.state.error_message = ""
        return None

    # ---- apply ----

    async def _apply_selection(self, provider_id: str, model_id: str) -> Screen | None:
        # For subscription_cli providers, verify the CLI is present.
        p = get_provider(provider_id)
        if p is None:
            self.app.state.error_message = f"unknown provider {provider_id!r}"
            return None
        if p.auth_mode == "subscription_cli":
            if not shutil.which("claude"):
                self.app.state.error_message = (
                    "claude CLI not found in PATH. Install Claude Code "
                    "(https://docs.claude.com/claude-code) and try again."
                )
                self._step = _Step(kind="pick_provider", focused=0)
                return None
            # Record that we're using the subscription.
            self._creds.providers.setdefault(
                provider_id,
                ProviderCredential(auth_mode="subscription_cli"),
            )
        self._creds.default_provider = provider_id
        self._creds.default_model = model_id
        try:
            save(self._creds)
        except Exception as e:
            self.app.state.error_message = f"failed to save credentials: {e}"
            return None

        self.app.state.provider = provider_id
        self.app.state.model = model_id
        self.app.state.error_message = ""

        # Transition to the lobby (the login screen used to do this;
        # now we're the waypoint between login and lobby).
        from silicon_pantheon.client.tui.screens.lobby import LobbyScreen

        return LobbyScreen(self.app)
