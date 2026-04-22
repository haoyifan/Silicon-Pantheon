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
        # Layer 1 of transport-resilience (see
        # ~/dev/transport-resilience-plan.md): when the MCP SDK's
        # internal anyio streams go closed (observed as ws_closed=True
        # / rs_closed=True on tool calls after a silent network blip),
        # any in-flight session.call_tool parks forever because
        # nothing wakes the awaiter. This event fires as soon as we
        # detect stream death; the worker's run_forever races its
        # game loop against this event so it can force a reconnect
        # instead of staying wedged for 90s+ behind wait_for.
        self._transport_dead_event = asyncio.Event()
        self._stream_monitor_task: asyncio.Task | None = None

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

        ── Instrumentation ──
        Enables DEBUG-level logging on ``httpx`` and ``httpcore`` and
        ``mcp`` so the per-request pool / connection / stream lifecycle
        is visible in the log. This is how we diagnose "hung call_tool
        with no httpx POST log" scenarios — root cause of those is
        usually an httpx pool or connect/write timeout, and the debug
        lines show exactly which phase stalled.

        The log volume is substantial (tens of lines per tool call),
        but these logs are critical for reproducing intermittent
        transport hangs. See docs/THREADING.md for the corresponding
        server-side instrumentation.
        """
        _configure_transport_diagnostics()
        cid = connection_id or secrets.token_hex(8)
        # Strip trailing slash to avoid 307 redirects on every call.
        url = url.rstrip("/")
        log.info(
            "transport connecting: url=%s cid=%s httpx_defaults=%s",
            url, cid, _describe_httpx_defaults(),
        )
        async with streamablehttp_client(url) as (read, write, _get_session_id):
            async with ClientSession(read, write) as session:
                await session.initialize()
                client = cls(session, connection_id=cid)
                client._start_stream_monitor()
                try:
                    yield client
                finally:
                    await client._stop_stream_monitor()

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
        # FastMCP signals tool-level exceptions by returning
        # `isError=True` with a PLAIN-TEXT error message in the text
        # block — NOT JSON. Our envelope always uses {ok, result|error}
        # JSON, so FastMCP's error shape only appears when our handler
        # raised an uncaught exception (e.g. SILICON_DEBUG=1 caught a
        # fog-leak invariant). Detect that case explicitly and raise
        # RemoteToolError so callers get a structured failure, not
        # a `json.loads("Error executing tool ...")` crash.
        is_error = bool(getattr(result, "isError", False))
        for block in result.content:
            text = getattr(block, "text", None)
            if text is None:
                continue
            if is_error:
                # FastMCP error: text is a human-readable string.
                log.error(
                    "call REMOTE_ERROR %s cid=%s dt=%.2fs text=%r",
                    tool_name, self.connection_id, _dt, text[:500],
                )
                raise RemoteToolError(
                    f"tool {tool_name} raised on the server: {text[:300]}"
                )
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError as e:
                # Non-error payload that isn't JSON — should never
                # happen under our envelope contract. Dump the raw
                # text so we can diagnose. isError was already False.
                log.error(
                    "call JSON_DECODE_ERROR %s cid=%s dt=%.2fs "
                    "text_len=%d text_repr=%r err=%s full_result=%r",
                    tool_name, self.connection_id, _dt,
                    len(text), text[:500], e, result,
                )
                raise RemoteToolError(
                    f"tool {tool_name} returned invalid JSON "
                    f"(len={len(text)}): {text[:200]!r}"
                ) from e
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

    # ─── Layer 1 — transport-dead detection ───

    def _start_stream_monitor(self) -> None:
        """Poll the MCP session's anyio streams for silent closure.

        When the SSE transport dies (network blip, proxy close,
        server-side cascade close — any of several causes we've
        chased), the MCP SDK's ``_write_stream`` / ``_read_stream``
        transition to closed locally but nothing wakes existing
        awaiters on them. In-flight ``session.call_tool`` parks
        forever because the future is tied to a stream that will
        never deliver another event.

        This monitor runs as a background task, polls the two
        streams' internal ``_closed`` flag once a second, and fires
        ``_transport_dead_event`` as soon as either goes closed.
        Workers / TUIs race this event against their game-loop task
        so they can force reconnect within ~1 s instead of waiting
        for the 90 s ``wait_for`` timeout that currently catches the
        condition.

        Polling is deliberately used instead of a logging-filter
        hook on ``mcp.client.streamable_http`` because the relevant
        log line ("SSE stream ended: …") is at DEBUG level and
        depends on the caller having bumped that logger; introspection
        of the stream attribute is more robust and self-contained.
        """
        if self._stream_monitor_task is not None and not self._stream_monitor_task.done():
            return

        async def _monitor() -> None:
            try:
                while not self._transport_dead_event.is_set():
                    await asyncio.sleep(1.0)
                    ws = getattr(self._session, "_write_stream", None)
                    rs = getattr(self._session, "_read_stream", None)
                    ws_closed = getattr(ws, "_closed", False) if ws is not None else True
                    rs_closed = getattr(rs, "_closed", False) if rs is not None else True
                    if ws_closed or rs_closed:
                        log.error(
                            "transport DEAD detected: cid=%s ws_closed=%s "
                            "rs_closed=%s — signalling reconnect",
                            self.connection_id, ws_closed, rs_closed,
                        )
                        self._transport_dead_event.set()
                        # Best-effort close of the other side too, so
                        # any awaiter on either stream sees a consistent
                        # ClosedResourceError and unwedges immediately
                        # instead of waiting 90s on wait_for.
                        for stream in (ws, rs):
                            if stream is None:
                                continue
                            try:
                                await stream.aclose()
                            except Exception:
                                pass
                        return
            except asyncio.CancelledError:
                return

        self._stream_monitor_task = asyncio.create_task(_monitor())

    async def _stop_stream_monitor(self) -> None:
        if self._stream_monitor_task is None:
            return
        task = self._stream_monitor_task
        self._stream_monitor_task = None
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    @property
    def transport_dead(self) -> asyncio.Event:
        """Event that fires when the MCP SDK's streams have gone dead.

        Public API for workers / TUIs to race against their main
        loops. Set by ``_start_stream_monitor`` (Layer 1) OR by the
        heartbeat loop after N consecutive ``ClosedResourceError``
        (Layer 2). Cleared by building a fresh ``ServerClient``
        (i.e., reconnecting).
        """
        return self._transport_dead_event

    # ─── heartbeat ───

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

        # Layer 2 of transport-resilience (see
        # ~/dev/transport-resilience-plan.md). The previous
        # implementation swallowed every exception; when the MCP SDK
        # streams went dead silently, heartbeats emitted
        # ``ClosedResourceError`` every 10s forever with no signal
        # upstream. Now we count consecutive failures and escalate:
        # after MAX_CONSECUTIVE_FAILURES in a row we fire
        # ``_transport_dead_event`` so the outer worker can race
        # against it and force a reconnect, and we stop heartbeating
        # (pointless once the transport is dead).
        #
        # A single transient error still gets swallowed — keep the
        # old tolerance for one-off slow calls / server restarts.
        MAX_CONSECUTIVE_FAILURES = 3
        from anyio import ClosedResourceError

        async def loop() -> None:
            consecutive_failures = 0
            try:
                while True:
                    await asyncio.sleep(interval_s)
                    try:
                        await self.call("heartbeat")
                        consecutive_failures = 0
                    except ClosedResourceError:
                        consecutive_failures += 1
                        if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                            log.error(
                                "heartbeat: %d consecutive ClosedResourceError "
                                "— transport is dead, signalling reconnect "
                                "(cid=%s)",
                                consecutive_failures, self.connection_id,
                            )
                            self._transport_dead_event.set()
                            return
                    except Exception:
                        # Non-transport errors (RemoteToolError, etc.)
                        # don't indicate a dead transport — keep
                        # heartbeating, but cap iterations so we don't
                        # spin forever if something weird keeps failing.
                        consecutive_failures += 1
                        if consecutive_failures >= 10:
                            log.error(
                                "heartbeat: %d consecutive failures "
                                "(non-Closed) — giving up, reconnecting "
                                "(cid=%s)",
                                consecutive_failures, self.connection_id,
                            )
                            self._transport_dead_event.set()
                            return
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


# ---- transport diagnostics ----

def _configure_transport_diagnostics() -> None:
    """Crank transport loggers to DEBUG so we capture httpx pool +
    connect + write + read lifecycle per request.

    Called once by ``ServerClient.connect``. Idempotent — re-running
    is a no-op because handlers are already attached by the CLI's
    logging setup (which inherits our log file by propagation).
    """
    import logging as _logging
    # We want DEBUG lines from httpx, httpcore, and the MCP SDK
    # itself — they reveal exactly where a hung POST is stalled.
    # Keep uvicorn/asyncio at INFO — those are high-volume and noisy.
    for name in ("httpx", "httpcore", "mcp", "mcp.client.streamable_http"):
        _logging.getLogger(name).setLevel(_logging.DEBUG)


def _describe_httpx_defaults() -> str:
    """Return a short string describing the httpx.Timeout + Limits
    the MCP SDK applies by default. Used in the connect log line so
    operators see what timeouts are in effect.
    """
    try:
        from mcp.shared._httpx_utils import (
            MCP_DEFAULT_SSE_READ_TIMEOUT,
            MCP_DEFAULT_TIMEOUT,
        )
        return (
            f"connect/write/pool={MCP_DEFAULT_TIMEOUT}s "
            f"read={MCP_DEFAULT_SSE_READ_TIMEOUT}s"
        )
    except Exception:  # noqa: BLE001 — diagnostic must never crash
        return "unknown"
