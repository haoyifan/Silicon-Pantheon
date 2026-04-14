"""Login screen: gather server URL + player metadata, connect, transition.

Fields navigated via Tab (or Down) / Shift-Tab (Up). Enter submits.
On submit the ServerClient is constructed, initialized, and
set_player_metadata is called; success transitions to the Lobby.
Failures stay on this screen with an error banner.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from rich.align import Align
from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.text import Text

from clash_of_odin.client.tui.app import Screen, TUIApp

if TYPE_CHECKING:
    from clash_of_odin.client.tui.screens.lobby import LobbyScreen


_KIND_OPTIONS = ("ai", "human", "hybrid")


@dataclass
class _Field:
    label: str
    value: str
    hint: str = ""
    options: tuple[str, ...] | None = None  # if set, field cycles via left/right


class LoginScreen(Screen):
    def __init__(self, app: TUIApp):
        self.app = app
        self._fields: list[_Field] = [
            _Field("server URL", app.state.server_url, hint="http://host:port/mcp/"),
            _Field("display name", app.state.display_name, hint="required"),
            _Field(
                "kind",
                app.state.kind or "ai",
                hint="left/right to cycle",
                options=_KIND_OPTIONS,
            ),
            _Field("provider", app.state.provider or "", hint="optional"),
            _Field("model", app.state.model or "", hint="optional"),
        ]
        self._active = 0
        self._connecting = False

    def render(self) -> RenderableType:
        lines: list[Text] = []
        title = Text("Clash of Odin — login", style="bold yellow")
        lines.append(title)
        lines.append(Text(""))
        for i, f in enumerate(self._fields):
            is_active = i == self._active
            marker = "➤" if is_active else " "
            label = Text(f"{marker} {f.label:14}", style="bold" if is_active else "dim")
            value_text = Text(f.value or "(empty)", style="white" if f.value else "dim italic")
            line = Text.assemble(label, Text("  "), value_text)
            lines.append(line)
            if is_active and f.hint:
                hint = Text(f"      {f.hint}", style="dim italic")
                lines.append(hint)
        lines.append(Text(""))
        status = Text("")
        if self._connecting:
            status.append("connecting…", style="yellow")
        elif self.app.state.error_message:
            status.append(self.app.state.error_message, style="red")
        elif self.app.state.status_message:
            status.append(self.app.state.status_message, style="green")
        lines.append(status)
        lines.append(Text(""))
        keys = Text(
            "Tab/↓ next field   Shift-Tab/↑ prev   ←/→ cycle options   "
            "Enter submit   q quit",
            style="dim",
        )
        lines.append(keys)

        body = Group(*lines)
        return Align.center(Panel(body, title="login", border_style="yellow"), vertical="middle")

    async def handle_key(self, key: str) -> Screen | None:
        if self._connecting:
            return None
        f = self._fields[self._active]
        if key == "q":
            self.app.exit()
            return None
        if key == "enter":
            # Submit if required fields are populated.
            return await self._submit()
        if key == "down" or key == "\t":
            self._active = (self._active + 1) % len(self._fields)
            return None
        if key == "up":
            self._active = (self._active - 1) % len(self._fields)
            return None
        if key in ("left", "right") and f.options is not None:
            idx = f.options.index(f.value) if f.value in f.options else 0
            step = 1 if key == "right" else -1
            f.value = f.options[(idx + step) % len(f.options)]
            return None
        if key == "backspace":
            f.value = f.value[:-1]
            return None
        # Ignore non-printable control keys; accept printable.
        if len(key) == 1 and key.isprintable():
            f.value = (f.value + key)[:120]
        return None

    async def _submit(self) -> Screen | None:
        name = self._fields[1].value.strip()
        if not name:
            self.app.state.error_message = "display name is required"
            return None
        self._connecting = True
        self.app.state.error_message = ""
        self.app.state.status_message = "connecting…"

        # Copy field values into shared state.
        self.app.state.server_url = self._fields[0].value.strip()
        self.app.state.display_name = name
        self.app.state.kind = self._fields[2].value.strip() or "ai"
        self.app.state.provider = self._fields[3].value.strip() or None
        self.app.state.model = self._fields[4].value.strip() or None

        # Late-import to avoid circular imports.
        from clash_of_odin.client.tui.screens.provider_auth import ProviderAuthScreen

        try:
            await _connect_and_declare(self.app)
        except Exception as e:
            self._connecting = False
            self.app.state.error_message = f"connect failed: {e}"
            return None
        # Pipeline: login -> provider-auth -> lobby. ProviderAuthScreen
        # handles the resume-or-pick-fresh logic for LLM credentials.
        return ProviderAuthScreen(self.app)


async def _connect_and_declare(app: TUIApp) -> None:
    """Open the ServerClient context, call set_player_metadata, start
    heartbeat. The client is retained on the app for later screens."""
    from clash_of_odin.client.transport import ServerClient

    # We need the ServerClient's context to stay open for the whole app
    # lifetime. The `ServerClient.connect` classmethod is an async ctx
    # manager that opens the SSE connection; we enter it manually and
    # rely on TUIApp.run to clean up on exit.
    ctx = ServerClient.connect(app.state.server_url)
    client = await ctx.__aenter__()
    app.client = client

    async def _cleanup() -> None:
        await ctx.__aexit__(None, None, None)

    app._transport_cleanup = _cleanup  # type: ignore[attr-defined]

    from clash_of_odin.shared.protocol import PROTOCOL_VERSION

    r = await client.call(
        "set_player_metadata",
        display_name=app.state.display_name,
        kind=app.state.kind,
        provider=app.state.provider,
        model=app.state.model,
        client_protocol_version=PROTOCOL_VERSION,
    )
    if not r.get("ok"):
        raise RuntimeError(r.get("error", {}).get("message", "metadata rejected"))
    app.state.connection_id = client.connection_id
    await client.start_heartbeat()
