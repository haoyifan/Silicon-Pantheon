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

from silicon_pantheon.client.locale import t
from silicon_pantheon.client.tui.app import Screen, TUIApp

if TYPE_CHECKING:
    from silicon_pantheon.client.tui.screens.lobby import LobbyScreen


class VersionMismatchError(Exception):
    """Raised by _connect_and_declare when the server/client version
    gap is too wide to play. The login screen catches this and routes
    to a dedicated upgrade-prompt screen rather than rendering a raw
    exception string in the status bar."""

    def __init__(self, *, kind: str, message: str, data: dict) -> None:
        super().__init__(message)
        self.kind = kind  # "client_too_old" | "server_too_old"
        self.data = data


import unicodedata


def _pad_label(s: str, width: int) -> str:
    """Pad to visual width, accounting for CJK double-width chars."""
    vw = sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in s)
    return s + " " * max(0, width - vw)


@dataclass
class _Field:
    label: str
    value: str


class LoginScreen(Screen):
    def __init__(self, app: TUIApp):
        self.app = app
        lc = app.state.locale
        self._fields: list[_Field] = [
            _Field(t("login_fields.server_url", lc), app.state.server_url),
            _Field(t("login_fields.display_name", lc), app.state.display_name),
        ]
        self._active = 0
        self._connecting = False
        self._confirm = None

    def render(self) -> RenderableType:
        if self._confirm is not None:
            return self._confirm.render()
        lines: list[Text] = []
        title = Text(f"SiliconPantheon — {t('login_screen.title', self.app.state.locale)}", style="bold yellow")
        lines.append(title)
        lines.append(Text(""))
        for i, f in enumerate(self._fields):
            is_active = i == self._active
            marker = "➤" if is_active else " "
            label = Text(f"{marker} {_pad_label(f.label, 14)}", style="bold" if is_active else "dim")
            value_text = Text(f.value or t("login_empty", self.app.state.locale), style="white" if f.value else "dim italic")
            line = Text.assemble(label, Text("  "), value_text)
            lines.append(line)
        lines.append(Text(""))
        status = Text("")
        lc = self.app.state.locale
        if self._connecting:
            status.append(t("login_screen.connecting", lc), style="yellow")
        elif self.app.state.error_message:
            status = Text(
                self.app.state.error_message,
                style="red", no_wrap=False, overflow="fold",
            )
        elif self.app.state.status_message:
            status.append(self.app.state.status_message, style="green")
        lines.append(status)
        lines.append(Text(""))
        keys = Text(t("login_screen.submit", lc), style="dim")
        lines.append(keys)

        body = Group(*lines)
        return Align.center(
            Panel(body, title=t("login_screen.title", lc), border_style="yellow", width=70),
            vertical="middle",
        )

    async def handle_key(self, key: str) -> Screen | None:
        if self._confirm is not None:
            close = await self._confirm.handle_key(key)
            if close:
                self._confirm = None
            return None
        if self._connecting:
            return None
        f = self._fields[self._active]
        if key == "esc":
            from silicon_pantheon.client.tui.screens.language_picker import LanguagePickerScreen
            return LanguagePickerScreen(self.app)
        if key == "q":
            from silicon_pantheon.client.tui.widgets import ConfirmModal
            from silicon_pantheon.client.locale import t
            async def _quit(yes: bool) -> None:
                if yes:
                    self.app.exit()
            self._confirm = ConfirmModal(
                prompt=t("lobby_quit.confirm", self.app.state.locale),
                on_confirm=_quit,
                locale=self.app.state.locale,
            )
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
        if key == "backspace":
            f.value = f.value[:-1]
            return None
        # Ignore non-printable control keys; accept printable.
        if len(key) == 1 and key.isprintable():
            f.value = (f.value + key)[:120]
        return None

    async def _submit(self) -> Screen | None:
        lc = self.app.state.locale
        url = self._fields[0].value.strip()
        name = self._fields[1].value.strip()
        # Validate URL format before connecting.
        import re
        if not url or not re.match(
            r'^https?://[a-zA-Z0-9._-]+(:\d+)?(/[a-zA-Z0-9._/-]*)?/?$', url
        ):
            self.app.state.error_message = (
                f"{t('login_fields.url_invalid', lc)} "
                f"(expected: http://host:port/mcp/)"
            )
            return None
        if not name:
            self.app.state.error_message = t("login_fields.display_name_required", lc)
            return None
        self._connecting = True
        self.app.state.error_message = ""
        self.app.state.status_message = t("login_screen.connecting", lc)

        # Copy field values into shared state.
        self.app.state.server_url = url
        self.app.state.display_name = name
        # kind defaults to "ai"; provider and model are set later by
        # the ProviderAuthScreen (no longer collected on the login form).
        self.app.state.kind = "ai"

        # Late-import to avoid circular imports.
        from silicon_pantheon.client.tui.screens.provider_auth import ProviderAuthScreen

        try:
            await _connect_and_declare(self.app)
        except VersionMismatchError as e:
            self._connecting = False
            self.app.state.status_message = ""
            self.app.state.error_message = ""
            from silicon_pantheon.client.tui.screens.upgrade_required import (
                UpgradeRequiredScreen,
            )
            return UpgradeRequiredScreen(
                self.app, kind=e.kind, message=str(e), data=e.data,
            )
        except Exception as e:
            self._connecting = False
            self.app.state.status_message = ""
            self.app.state.error_message = f"{t('login_fields.connect_failed', self.app.state.locale)}: {e}"
            return None
        # Pipeline: login -> provider-auth -> lobby. ProviderAuthScreen
        # handles the resume-or-pick-fresh logic for LLM credentials.
        # Clear the transient "connecting…" status so it doesn't bleed
        # through to the downstream screens' status bars.
        self.app.state.status_message = ""
        return ProviderAuthScreen(self.app)


async def _fetch_scenario_bundle(app: TUIApp) -> None:
    """Fetch all scenario descriptions in one server call.

    Uses get_scenario_bundle which returns every scenario in a single
    response (~200ms). The server includes a content hash; the client
    caches the bundle + hash on disk so repeat logins skip the
    download entirely (hash match → no data transfer).

    Non-fatal: errors are logged but don't block login.
    """
    import json as _json
    import logging as _logging
    from pathlib import Path as _Path

    from silicon_pantheon.client.locale.scenario import localize_scenario

    _log = _logging.getLogger("silicon.tui.login")
    cache_path = _Path.home() / ".silicon-pantheon" / "scenario_bundle_cache.json"

    # Load cached hash from disk.
    cached_hash: str | None = None
    try:
        if cache_path.is_file():
            cached = _json.loads(cache_path.read_text(encoding="utf-8"))
            cached_hash = cached.get("hash")
            # Pre-populate from disk cache immediately.
            lc = app.state.locale
            for name, data in (cached.get("scenarios") or {}).items():
                if name not in app.state.scenario_cache:
                    app.state.scenario_cache[name] = localize_scenario(data, lc)
            _log.info(
                "scenario bundle: loaded %d from disk (hash=%s)",
                len(app.state.scenario_cache), cached_hash,
            )
    except Exception:
        pass

    # Ask server for the bundle.
    if app.client is None:
        return
    try:
        r = await app.client.call(
            "get_scenario_bundle",
            cached_hash=cached_hash,
        )
        if not r.get("ok"):
            _log.warning("get_scenario_bundle rejected: %s", r.get("error"))
            return
        result = r.get("result") or r
    except Exception:
        _log.debug("get_scenario_bundle failed — using disk cache", exc_info=True)
        return

    if result.get("match"):
        _log.info("scenario bundle: hash match — cached data is current")
        return

    # New bundle — populate cache and save to disk.
    bundle_hash = result.get("hash", "")
    scenarios = result.get("scenarios") or {}
    _log.info("scenario bundle: received %d scenarios (hash=%s)", len(scenarios), bundle_hash)
    lc = app.state.locale
    for name, data in scenarios.items():
        app.state.scenario_cache[name] = localize_scenario(data, lc)

    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(
            _json.dumps({"hash": bundle_hash, "scenarios": scenarios}),
            encoding="utf-8",
        )
    except Exception:
        pass


async def _validate_url(url: str) -> None:
    """Fast pre-flight check via the /health endpoint (~100ms).

    The Silicon Pantheon server exposes GET /health which returns
    {"server": "silicon-pantheon", "status": "ok"}. We derive the
    health URL from the MCP URL (strip /mcp/ path, append /health).
    """
    import httpx
    from urllib.parse import urlparse, urlunparse

    _fmt = "Expected: http://host:port/mcp/"

    # Derive health URL: https://host:port/mcp/ → https://host:port/health
    parsed = urlparse(url)
    health_url = urlunparse((
        parsed.scheme, parsed.netloc, "/health", "", "", "",
    ))

    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.get(health_url)
            if r.status_code == 200:
                try:
                    body = r.json()
                    if body.get("server") == "silicon-pantheon":
                        # Server confirmed. Now check that the MCP path
                        # is correct — the server expects /mcp/ (or /mcp).
                        mcp_path = parsed.path.rstrip("/")
                        if mcp_path and mcp_path != "/mcp":
                            raise ConnectionError(
                                f"Server found, but path '{parsed.path}' "
                                f"is wrong. Use: {parsed.scheme}://"
                                f"{parsed.netloc}/mcp/"
                            )
                        return  # confirmed Silicon Pantheon server
                except ConnectionError:
                    raise
                except Exception:
                    pass
            # /health not found or unexpected content — older server
            # without the endpoint. Let MCP handshake decide.
            return
    except httpx.ConnectError as e:
        raise ConnectionError(
            f"Cannot connect — check host and port. {_fmt}"
        ) from e
    except httpx.ConnectTimeout:
        raise ConnectionError(
            f"Connection timed out. {_fmt}"
        )
    except ConnectionError:
        raise
    except Exception:
        return


async def _connect_and_declare(app: TUIApp) -> None:
    """Open the ServerClient context, call set_player_metadata, start
    heartbeat. The client is retained on the app for later screens."""
    import asyncio as _aio

    from silicon_pantheon.client.transport import ServerClient

    # Fast pre-flight: catch bad URLs in ~1s instead of hanging.
    await _validate_url(app.state.server_url)

    # We need the ServerClient's context to stay open for the whole app
    # lifetime. The `ServerClient.connect` classmethod is an async ctx
    # manager that opens the SSE connection; we enter it manually and
    # rely on TUIApp.run to clean up on exit.
    ctx = ServerClient.connect(app.state.server_url)
    try:
        client = await _aio.wait_for(ctx.__aenter__(), timeout=10.0)
    except _aio.TimeoutError:
        raise ConnectionError(
            "MCP handshake timed out after 10s — server may not "
            "support MCP. Expected format: http://host:port/mcp/"
        )
    app.client = client

    async def _cleanup() -> None:
        await ctx.__aexit__(None, None, None)

    app._transport_cleanup = _cleanup  # type: ignore[attr-defined]

    from silicon_pantheon.shared.protocol import (
        ErrorCode,
        MINIMUM_SERVER_PROTOCOL_VERSION,
        PROTOCOL_VERSION,
        UPGRADE_COMMAND_HINT,
    )

    r = await client.call(
        "set_player_metadata",
        display_name=app.state.display_name,
        kind=app.state.kind,
        provider=app.state.provider,
        model=app.state.model,
        client_protocol_version=PROTOCOL_VERSION,
    )
    if not r.get("ok"):
        err = r.get("error") or {}
        code = err.get("code", "")
        msg = err.get("message", "metadata rejected")
        # Raise a typed error for version mismatch so the caller can
        # route to the upgrade screen instead of rendering a raw
        # "metadata rejected" in the login status bar.
        if code == ErrorCode.CLIENT_TOO_OLD.value:
            raise VersionMismatchError(kind="client_too_old", message=msg, data=err.get("data") or {})
        raise RuntimeError(msg)
    # Server reachable; also verify server isn't too old for THIS client.
    # A missing server_protocol_version is treated as v0 (pre-v1 — the
    # ancient past) and rejected for any MIN_SERVER >= 1. Don't guard
    # this with `> 0` — a silent pass would let a future server that
    # forgot to include the field bypass the gate entirely.
    result = r.get("result") or r
    try:
        server_version = int(result.get("server_protocol_version") or 0)
    except (TypeError, ValueError):
        server_version = 0
    if server_version < MINIMUM_SERVER_PROTOCOL_VERSION:
        raise VersionMismatchError(
            kind="server_too_old",
            message=(
                f"Server is on protocol v{server_version} but this client "
                f"requires at least v{MINIMUM_SERVER_PROTOCOL_VERSION}. "
                "Ask the server operator to update."
            ),
            data={
                "server_protocol_version": server_version,
                "minimum_server_protocol_version": MINIMUM_SERVER_PROTOCOL_VERSION,
            },
        )
    app.state.connection_id = client.connection_id
    await client.start_heartbeat()

    # Fetch the scenario bundle in one call (~200ms). This blocks the
    # login transition but is fast enough that users don't notice.
    # The bundle is cached on disk with a hash — repeat logins skip
    # the download if the hash matches.
    await _fetch_scenario_bundle(app)
