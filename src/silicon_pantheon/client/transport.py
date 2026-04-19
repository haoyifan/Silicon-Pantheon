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

# Watchdog + timeout for call_tool inside the lock.
#
# Root cause (confirmed): server restarts kill the SSE connection
# but the MCP SDK doesn't detect the dead streams. call_tool hangs
# forever. The watchdog logs diagnostics every 30s (ws_closed,
# rs_closed, pending_requests) to confirm the streams are dead.
# After CALL_TOOL_TIMEOUT_S, the call is cancelled so the client
# can reconnect instead of hanging indefinitely.
WATCHDOG_INTERVAL_S = 30.0
CALL_TOOL_TIMEOUT_S = 90.0

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
        # Serialize all call_tool requests on this session. The MCP
        # SDK demuxes SSE responses by JSON-RPC ID — concurrent
        # requests cause responses to get lost/misrouted and one or
        # more call_tool futures hang forever. Even 2 concurrent
        # calls (TUI poll + agent tool) can trigger this.
        self._call_lock = asyncio.Lock()

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
        import time as _time

        # Acquire the call lock to ensure only one call_tool is in
        # flight at a time. The MCP SDK's SSE response demuxer loses
        # responses under concurrent requests, causing permanent hangs.
        async with self._call_lock:
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
            _t0 = _time.monotonic()
            # Watchdog: periodically log diagnostics if the call is
            # still pending. Does NOT cancel — we want the hang to be
            # visible so we can find the root cause.
            async def _watchdog():
                while True:
                    await asyncio.sleep(WATCHDOG_INTERVAL_S)
                    elapsed = _time.monotonic() - _t0
                    ws2 = getattr(self._session, "_write_stream", None)
                    rs2 = getattr(self._session, "_read_stream", None)
                    ws2_closed = getattr(ws2, "_closed", "?") if ws2 is not None else "?"
                    rs2_closed = getattr(rs2, "_closed", "?") if rs2 is not None else "?"
                    # Try to inspect MCP SDK internal state.
                    pending_requests = "?"
                    try:
                        # ClientSession tracks pending requests internally.
                        pending_requests = str(len(getattr(
                            self._session, "_response_streams", {}
                        )))
                    except Exception:
                        pass
                    log.error(
                        "call HUNG %s cid=%s pending=%.0fs phase=%s — "
                        "ws_closed=%s rs_closed=%s pending_requests=%s "
                        "sess=%s ws=%s rs=%s",
                        tool_name, self.connection_id, elapsed, _phase,
                        ws2_closed, rs2_closed, pending_requests,
                        id(self._session),
                        id(ws2) if ws2 else None,
                        id(rs2) if rs2 else None,
                    )
            wd_task = asyncio.create_task(_watchdog())
            try:
                # ── Granular diagnostics: instrument the MCP SDK call ──
                # Phase 1: write the request to the SDK's write stream
                # Phase 2: wait for the response from the SSE demuxer
                # This tells us whether the hang is in sending or receiving.
                _phase = "pre-call"
                async def _instrumented_call():
                    nonlocal _phase
                    _phase = "call_tool:entered"
                    log.log(
                        level,
                        "call PHASE %s %s cid=%s",
                        _phase, tool_name, self.connection_id,
                    )
                    # Inspect the write stream state right before we use it
                    _ws = getattr(self._session, "_write_stream", None)
                    _ws_closed = getattr(_ws, "_closed", "?") if _ws else "?"
                    _req_id = getattr(self._session, "_request_id", "?")
                    _resp_count = len(getattr(self._session, "_response_streams", {}))
                    log.log(
                        level,
                        "call PHASE pre-send %s cid=%s req_id=%s "
                        "pending_responses=%s ws_closed=%s",
                        tool_name, self.connection_id,
                        _req_id, _resp_count, _ws_closed,
                    )
                    _phase = "call_tool:sending"
                    result = await self._session.call_tool(tool_name, args)
                    _phase = "call_tool:done"
                    return result

                result = await asyncio.wait_for(
                    _instrumented_call(),
                    timeout=CALL_TOOL_TIMEOUT_S,
                )
            except asyncio.TimeoutError:
                elapsed = _time.monotonic() - _t0
                ws2 = getattr(self._session, "_write_stream", None)
                rs2 = getattr(self._session, "_read_stream", None)
                log.error(
                    "call TIMEOUT %s cid=%s dt=%.1fs phase=%s — "
                    "ws_closed=%s rs_closed=%s.",
                    tool_name, self.connection_id, elapsed, _phase,
                    getattr(ws2, "_closed", "?") if ws2 else "?",
                    getattr(rs2, "_closed", "?") if rs2 else "?",
                )
                raise
            except Exception as e:
                ws2 = getattr(self._session, "_write_stream", None)
                rs2 = getattr(self._session, "_read_stream", None)
                log.error(
                    "call !! %s cid=%s exc_type=%s exc=%r dt=%.1fs "
                    "sess_now=%s ws_now=%s ws_closed_now=%s rs_now=%s",
                    tool_name, self.connection_id, type(e).__name__, e,
                    _time.monotonic() - _t0,
                    id(self._session),
                    id(ws2) if ws2 is not None else None,
                    getattr(ws2, "_closed", "?") if ws2 is not None else "?",
                    id(rs2) if rs2 is not None else None,
                )
                raise
            finally:
                wd_task.cancel()
        _dt = _time.monotonic() - _t0
        if _dt > 5.0:
            log.warning(
                "call SLOW %s cid=%s dt=%.1fs",
                tool_name, self.connection_id, _dt,
            )
        for block in result.content:
            text = getattr(block, "text", None)
            if text is not None:
                parsed = json.loads(text)
                log.log(
                    level,
                    "call <- %s ok=%s dt=%.2fs keys=%s",
                    tool_name,
                    parsed.get("ok"),
                    _dt,
                    list(parsed.keys())[:8],
                )
                return parsed
        log.error(
            "call NO_TEXT_BLOCK %s cid=%s dt=%.1fs content=%r",
            tool_name, self.connection_id, _dt, result.content,
        )
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
