"""MCP streamable-HTTP client wrapper.

Wraps the raw mcp SDK client bits into a small async-only API that
matches how our tools look from the caller's perspective: pass a
tool name + kwargs, get back the structured dict the server returned.

All transport errors bubble up as-is so the caller can distinguish
"network said no" from "tool returned {ok: false, error: ...}".
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

log = logging.getLogger("clash.transport")

CLIENT_HEARTBEAT_INTERVAL_S = 10.0

# Tool calls we log each roundtrip at INFO. Everything else logs at DEBUG
# so the file doesn't get overwhelmed with per-second heartbeats.
_VERBOSE_TOOLS = frozenset({
    "set_player_metadata",
    "create_room",
    "join_room",
    "leave_room",
    "set_ready",
    "get_room_state",
    "concede",
    "download_replay",
})


class RemoteToolError(RuntimeError):
    """Raised when a tool call returned no parseable structured body."""


class ServerClient:
    """Connected MCP client. Use via `ServerClient.connect(url)` as an
    async context manager."""

    def __init__(self, session: ClientSession, *, connection_id: str):
        self._session = session
        self.connection_id = connection_id
        self._heartbeat_task: asyncio.Task | None = None

    @classmethod
    @asynccontextmanager
    async def connect(
        cls,
        url: str,
        *,
        connection_id: str | None = None,
    ) -> AsyncIterator["ServerClient"]:
        """Open an MCP+SSE connection to the server and initialize it.

        Yields a ServerClient ready for tool calls.
        """
        cid = connection_id or secrets.token_hex(8)
        async with streamablehttp_client(url) as (read, write, _get_session_id):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield cls(session, connection_id=cid)

    async def call(self, tool_name: str, **kwargs: Any) -> dict:
        """Call a server tool, returning the structured response dict.

        The server always returns JSON wrapped in a TextContent block;
        this helper parses that back out. `connection_id` is injected
        automatically so callers can focus on tool-specific args.
        """
        args = {"connection_id": self.connection_id, **kwargs}
        level = logging.INFO if tool_name in _VERBOSE_TOOLS else logging.DEBUG
        log.log(level, "call -> %s args=%s", tool_name, {k: v for k, v in kwargs.items()})
        result = await self._session.call_tool(tool_name, args)
        for block in result.content:
            text = getattr(block, "text", None)
            if text is not None:
                parsed = json.loads(text)
                log.log(
                    level,
                    "call <- %s ok=%s keys=%s",
                    tool_name,
                    parsed.get("ok"),
                    list(parsed.keys())[:8],
                )
                return parsed
        raise RemoteToolError(
            f"tool {tool_name} returned no text block: {result!r}"
        )

    async def start_heartbeat(
        self, interval_s: float = CLIENT_HEARTBEAT_INTERVAL_S
    ) -> None:
        """Launch a background task that calls `heartbeat` every N seconds.

        Callers that stay connected for a while (lobby, in-game) should
        start this right after `set_player_metadata` so the server's
        soft-disconnect sweeper doesn't evict them.
        """
        if self._heartbeat_task is not None and not self._heartbeat_task.done():
            return

        async def loop() -> None:
            try:
                while True:
                    await asyncio.sleep(interval_s)
                    try:
                        await self.call("heartbeat")
                    except Exception:
                        # Swallow transient errors — the server-side
                        # sweeper will evict us if we stop heartbeating.
                        pass
            except asyncio.CancelledError:
                return

        self._heartbeat_task = asyncio.create_task(loop())

    async def stop_heartbeat(self) -> None:
        if self._heartbeat_task is not None and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
        self._heartbeat_task = None
