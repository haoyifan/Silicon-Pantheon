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

from clash_of_odin.client.credentials import (
    Credentials,
    CredentialsError,
    ProviderCredential,
    load,
    resolve_key,
    save,
)
from clash_of_odin.client.tui.app import Screen, TUIApp
from clash_of_odin.shared.providers import PROVIDERS, ProviderSpec, get_provider

log = logging.getLogger("clash.tui.provider_auth")


@dataclass
class _Step:
    """Lightweight state machine for the picker."""

    kind: str  # "resume" | "pick_provider" | "api_key" | "pick_model"
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
            lines.append(
                Text.assemble(
                    ("key: ", "yellow bold"),
                    ("*" * len(self._step.key_buffer), "white"),
                    ("▌", "yellow"),
                )
            )
            lines.append(
                Text(
                    "(type the key, Enter to save to keyring + continue, Esc cancels)",
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
        lines.append(
            Text("↑/k ↓/j navigate   Enter pick   Esc back   q quit", style="dim")
        )
        if self.app.state.error_message:
            lines.append(Text(self.app.state.error_message, style="red"))
        return Align.center(
            Panel(Group(*lines), title="model", border_style="yellow", padding=(1, 3)),
            vertical="middle",
        )

    # ---- input ----

    async def handle_key(self, key: str) -> Screen | None:
        if key == "q":
            self.app.exit()
            return None
        if self._step.kind == "resume":
            return await self._handle_resume_key(key)
        if self._step.kind == "pick_provider":
            return self._handle_pick_provider_key(key)
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

    def _handle_pick_provider_key(self, key: str) -> Screen | None:
        if key in ("down", "j"):
            self._step.focused = (self._step.focused + 1) % len(PROVIDERS)
        elif key in ("up", "k"):
            self._step.focused = (self._step.focused - 1) % len(PROVIDERS)
        elif key == "enter":
            p = PROVIDERS[self._step.focused]
            self._step = _Step(
                kind="api_key" if p.auth_mode == "api_key" else "pick_model",
                provider_id=p.id,
                focused=0,
            )
            self.app.state.error_message = ""
        return None

    async def _handle_api_key_key(self, key: str) -> Screen | None:
        if key == "esc":
            self._step = _Step(kind="pick_provider", focused=0)
            return None
        # When in paste mode, the buffer absorbs printable chars.
        in_paste = self._step.focused == 1
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
                "(consider installing clash-of-odin[keyring])"
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
        from clash_of_odin.client.tui.screens.lobby import LobbyScreen

        return LobbyScreen(self.app)
