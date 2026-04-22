"""Temporary diagnostic harness for the "SSE response drops mid-write"
bug tracked from 2026-04-20 onward.

Observed symptom: silicon-serve closes its loopback TCP to Caddy after
3-25 ms into an SSE response. Caddy aborts forwarding with
``use of closed network connection``. Client sees
``incomplete chunked read``. Tool handler then finds the per-request
SSE stream already cleaned up → ``Request stream <N> not found`` in
the MCP SDK, response dropped, client hangs until its 90s watchdog.

This module bundles three diagnostics behind a single ``--diagnose-sse``
flag so we can rip it all out once the root cause is understood:

    1. Monkey-patch ``sse_starlette.EventSourceResponse.__call__`` to
       log entry / exit / exception with the MCP session id from headers
       so we know WHEN each SSE response started and HOW it ended.
    2. Monkey-patch
       ``StreamableHTTPServerTransport._clean_up_memory_streams`` to
       log each cleanup with a short call-stack, so we see WHO popped
       the request stream before the tool response arrived.
    3. Spawn tcpdump on the loopback interface with a rolling pcap
       buffer so we can diff the SSE close (FIN vs RST) against the
       Python-level events.

EVERYTHING here is tagged ``# DIAG(sse)`` so it's greppable for
removal. None of it runs unless ``--diagnose-sse`` is passed.
"""

from __future__ import annotations

import atexit
import logging
import os
import signal
import subprocess
import sys
import time
import traceback
from pathlib import Path
from typing import Any

_log = logging.getLogger("silicon.diag.sse")

# Where the rolling tcpdump writes. Under ~/.silicon-pantheon so
# the existing systemd ReadWritePaths directive covers it — /tmp is
# namespaced by PrivateTmp=true and unreachable from outside the
# unit. Operator SCPs this directory off the server after
# reproducing the bug.
_PCAP_DIR = Path.home() / ".silicon-pantheon" / "sse-diag"

# Module-level singleton so we only install patches once and own
# the tcpdump subprocess for the lifetime of the process.
_tcpdump_proc: subprocess.Popen | None = None


def enable(port: int) -> None:  # DIAG(sse)
    """Install all three diagnostics. Call once at silicon-serve startup,
    AFTER logging handlers are wired but BEFORE the MCP server builds.
    """
    _log.warning(
        "SSE diagnostic mode ENABLED — monkey-patching sse_starlette + "
        "MCP SDK and starting tcpdump on port %d. Remove --diagnose-sse "
        "to disable.",
        port,
    )
    _patch_event_source_response()
    _patch_cleanup_memory_streams()
    _start_tcpdump(port)


# ────────────────────────────── (1) EventSourceResponse ──

def _patch_event_source_response() -> None:  # DIAG(sse)
    """Wrap ``EventSourceResponse.__call__`` to log lifecycle events.

    We want to distinguish:
      - Normal exit (response body iterator finished)
      - Exception raised inside the ASGI call
      - Client disconnect mid-stream (ASGI ``http.disconnect``)

    All three paths currently close the TCP; only the first should,
    and it should also flush the chunked terminator first. Logging
    entry + exit reason at the Starlette layer pinpoints which path
    is taken on the failing requests.
    """
    import sse_starlette.sse as _sse_mod

    original_call = _sse_mod.EventSourceResponse.__call__

    async def _patched_call(self: Any, scope: Any, receive: Any, send: Any) -> None:
        # Best-effort correlation id: MCP session header if present.
        # Scope is an ASGI dict; headers is list of (bytes, bytes) pairs.
        mcp_sess = "?"
        try:
            headers = dict(scope.get("headers") or [])
            mcp_sess = (
                headers.get(b"mcp-session-id", b"?").decode("latin-1", "replace")
            )
        except Exception:
            pass
        client = scope.get("client") or ("?", "?")
        t0 = time.monotonic()
        _log.info(
            "esr ENTER mcp_session=%s client=%s:%s path=%s",
            mcp_sess, client[0], client[1], scope.get("path"),
        )
        try:
            await original_call(self, scope, receive, send)
        except Exception as e:
            dt_ms = (time.monotonic() - t0) * 1000
            _log.error(
                "esr EXIT-EXC mcp_session=%s dt=%.1fms exc=%s: %s",
                mcp_sess, dt_ms, type(e).__name__, e,
                exc_info=True,
            )
            raise
        else:
            dt_ms = (time.monotonic() - t0) * 1000
            _log.info(
                "esr EXIT-OK mcp_session=%s dt=%.1fms",
                mcp_sess, dt_ms,
            )

    _sse_mod.EventSourceResponse.__call__ = _patched_call
    # The MCP SDK imports EventSourceResponse by name at module load
    # time, so we also patch its binding inside the SDK module if it's
    # already imported.
    try:
        import mcp.server.streamable_http as _sh
        _sh.EventSourceResponse = _sse_mod.EventSourceResponse
    except Exception:
        pass
    _log.info("patched sse_starlette.EventSourceResponse.__call__")


# ────────────────────────────── (2) _clean_up_memory_streams ──

def _patch_cleanup_memory_streams() -> None:  # DIAG(sse)
    """Wrap the MCP SDK's per-request stream cleanup to log its caller.

    The SDK logs ``Closing SSE writer`` at DEBUG inside the sse_writer
    finally. But when the request stream is popped due to an SSE-writer
    error OR the outer transport's clean-up loop, we don't currently
    see that distinction. This patch logs a compact call-stack for
    every cleanup, so we can correlate each ``Request stream X not
    found`` with the code path that just popped X.
    """
    try:
        from mcp.server.streamable_http import StreamableHTTPServerTransport
    except Exception as e:
        _log.warning("could not import StreamableHTTPServerTransport: %s", e)
        return

    original = StreamableHTTPServerTransport._clean_up_memory_streams

    async def _patched_cleanup(self: Any, request_id: Any) -> None:
        # Abbreviate stack to the most informative 4 frames ABOVE this
        # wrapper (skip the patched frame itself).
        frames = traceback.extract_stack()[-6:-1]
        brief = " <- ".join(
            f"{Path(fr.filename).name}:{fr.lineno}:{fr.name}" for fr in frames
        )
        in_registry = request_id in getattr(self, "_request_streams", {})
        _log.info(
            "cleanup_stream req_id=%s present=%s caller=%s",
            request_id, in_registry, brief,
        )
        return await original(self, request_id)

    StreamableHTTPServerTransport._clean_up_memory_streams = _patched_cleanup
    _log.info("patched StreamableHTTPServerTransport._clean_up_memory_streams")


# ────────────────────────────── (3) tcpdump on loopback ──

def _start_tcpdump(port: int) -> None:  # DIAG(sse)
    """Spawn tcpdump on ``lo`` with a rolling pcap buffer.

    Rolling 10 × 50 MB files (size-based rotation; ~500 MB ceiling).
    Size-based is used because ``tcpdump -G <sec> -W <count>`` EXITS
    after ``count`` rotations rather than rotating (2026-04-22 repro
    hit this: tcpdump died 10 min after server start, capturing
    nothing for every real-world incident after that). ``-C <MB>``
    with ``-W <count>`` gives the rolling-buffer behaviour we want.

    Output goes to ``~/.silicon-pantheon/sse-diag/pid<P>/``.

    Does NOT use sudo — systemd's ``NoNewPrivileges=true`` blocks
    that. Requires the operator to grant ``CAP_NET_RAW CAP_NET_ADMIN``
    to silicon-serve via systemd ``AmbientCapabilities``; see the
    --diagnose-sse help text for the drop-in snippet.
    """
    global _tcpdump_proc

    _PCAP_DIR.mkdir(parents=True, exist_ok=True)
    # Unique per-pid subdir so multiple silicon-serve processes don't
    # stomp on each other.
    out_dir = _PCAP_DIR / f"pid{os.getpid()}"
    out_dir.mkdir(parents=True, exist_ok=True)
    # Ubuntu's tcpdump auto-drops to the `tcpdump` system user (uid 105)
    # after opening the raw socket. That user can't write inside
    # silicon's home, so mark this dir world-writable as a fallback.
    # It's a diagnostic-only dir; widen-perm risk is acceptable.
    try:
        os.chmod(out_dir, 0o777)
    except PermissionError:
        pass
    # -Z <user> tells tcpdump which user to drop privs to after opening
    # the raw socket. Without it, Ubuntu's build auto-drops to the
    # `tcpdump` system user (uid 105), which can't write inside silicon's
    # home. Passing the current user makes it a no-op setuid that stays
    # as us, so pcap writes succeed.
    current_user = os.environ.get("USER") or "silicon"
    # With size-based rotation (-C) + count (-W), tcpdump uses filenames
    # of the form <template>N (N = 0..W-1) cycling. Strip the %H%M%S so
    # tcpdump doesn't interpret it; let it append its own counter.
    size_template = str(out_dir / "capture.pcap")
    cmd = [
        "tcpdump",
        "-i", "lo",
        "-U",                       # packet-buffered; no 4KB cache
        "-w", size_template,
        "-C", "50",                 # rotate every 50 MB
        "-W", "10",                 # rolling buffer of 10 files (~500 MB)
        "-Z", current_user,         # stay as current user, not `tcpdump`
        "-n",
        f"port {port}",
    ]
    _log.info("spawning tcpdump: %s", " ".join(cmd))
    try:
        # stdout + stderr to the pcap dir so tcpdump complaints are
        # visible alongside the capture files.
        tcpdump_log = out_dir / "tcpdump.log"
        fh = open(tcpdump_log, "ab", buffering=0)
        _tcpdump_proc = subprocess.Popen(
            cmd,
            stdout=fh, stderr=fh,
            # New session so SIGINT to silicon-serve doesn't also kill
            # tcpdump before we finalize the last pcap file.
            start_new_session=True,
        )
    except FileNotFoundError:
        _log.error("tcpdump not installed — cannot capture loopback traffic")
        return
    except PermissionError as e:
        _log.error(
            "tcpdump spawn failed: %s — systemd drop-in needs "
            "AmbientCapabilities=CAP_NET_RAW CAP_NET_ADMIN", e,
        )
        return

    atexit.register(_stop_tcpdump)
    # SIGTERM from systemd also triggers atexit, but be explicit.
    try:
        signal.signal(signal.SIGTERM, _sigterm_handler)
    except ValueError:
        # Only main thread can install signal handlers.
        pass

    _log.warning(
        "tcpdump running → %s  (10 × 60s rolling; SCP whole dir off "
        "the box when reproducing the bug)", out_dir,
    )


def _stop_tcpdump() -> None:  # DIAG(sse)
    global _tcpdump_proc
    if _tcpdump_proc is None:
        return
    proc = _tcpdump_proc
    _tcpdump_proc = None
    if proc.poll() is not None:
        _log.info("tcpdump already exited rc=%s", proc.returncode)
        return
    _log.info("stopping tcpdump pid=%d", proc.pid)
    # Direct kill — we spawned the process; no privilege elevation
    # needed to signal our own child.
    try:
        proc.terminate()  # SIGTERM
    except Exception:
        pass
    try:
        proc.wait(timeout=3.0)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except Exception:
            pass


def _sigterm_handler(signum: int, frame: Any) -> None:  # DIAG(sse)
    _stop_tcpdump()
    # Chain to default so systemd's TERM still terminates us.
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    os.kill(os.getpid(), signum)
