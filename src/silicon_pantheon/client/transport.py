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

log = logging.getLogger("silicon.transport")

CLIENT_HEARTBEAT_INTERVAL_S = 10.0

# Tools that are purely noisy (heartbeat / state polls) are logged at
# DEBUG so the file stays readable; everything else at INFO.
# IMPORTANT: keep get_state at INFO — it's how we see agents / screens
# interact mid-game. We can always tune this down once the stuck-state
# bug is nailed.
_QUIET_TOOLS = frozenset({"heartbeat"})


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
        level = logging.DEBUG if tool_name in _QUIET_TOOLS else logging.INFO
        # Diagnostic identity for the underlying anyio streams. We're
        # chasing a "ClosedResourceError after end_turn" repro: knowing
        # whether the stream object identity changes between calls (vs.
        # the SAME stream object getting closed mid-flight) tells us
        # whether the MCP SDK rotated the session under us, or whether
        # something on the wire / server killed the stream we still
        # hold a reference to.
        sess_id = id(self._session)
        ws = getattr(self._session, "_write_stream", None)
        rs = getattr(self._session, "_read_stream", None)
        ws_id = id(ws) if ws is not None else None
        rs_id = id(rs) if rs is not None else None
        ws_closed = getattr(ws, "_closed", "?") if ws is not None else "?"
        log.log(
            level,
            "call -> %s cid=%s sess=%s ws=%s ws_closed=%s rs=%s args=%s",
            tool_name, self.connection_id, sess_id, ws_id, ws_closed, rs_id,
            {k: v for k, v in kwargs.items()},
        )
        try:
            result = await self._session.call_tool(tool_name, args)
        except Exception as e:
            # Re-snapshot stream state at failure time so we can see
            # whether the close happened between request issue and the
            # exception raise (e.g. server reset, or our own side
            # tearing down). Log type explicitly because anyio's
            # ClosedResourceError vs. EndOfStream vs. transport-level
            # CancelledError each have different root causes.
            ws2 = getattr(self._session, "_write_stream", None)
            rs2 = getattr(self._session, "_read_stream", None)
            log.error(
                "call !! %s cid=%s exc_type=%s exc=%r "
                "sess_now=%s ws_now=%s ws_closed_now=%s rs_now=%s",
                tool_name, self.connection_id, type(e).__name__, e,
                id(self._session),
                id(ws2) if ws2 is not None else None,
                getattr(ws2, "_closed", "?") if ws2 is not None else "?",
                id(rs2) if rs2 is not None else None,
            )
            raise
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
