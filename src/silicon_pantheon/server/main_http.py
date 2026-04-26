"""`silicon-serve` CLI entry — MCP over streamable HTTP.

Separate module from `server/main.py` (which hosts the legacy stdio
MCP server for `silicon-match` in-process use) so the two do not
conflict during the Phase 1 transition.

Usage:
    silicon-serve                      # listen on 127.0.0.1:8080
    silicon-serve --host 0.0.0.0 --port 9000
"""

from __future__ import annotations

import argparse
import datetime as _dt
import logging
import os
from pathlib import Path

from silicon_pantheon.server.app import App, build_mcp_server


def _configure_server_logging(level: str, log_file: Path | None) -> Path:
    """Install stderr + file handlers on the `clash` logger namespace.

    Why not the root logger: `mcp.run_streamable_http_async` -> uvicorn
    calls `logging.basicConfig()` which wipes handlers on root. Earlier
    runs lost every `clash.lobby` / `clash.game` log line because of
    this. Attaching to "silicon" with propagate=False keeps our
    instrumentation intact while still letting uvicorn / httpx run
    their own handlers however they like.
    """
    if log_file is None:
        log_dir = Path.home() / ".silicon-pantheon" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        ts = _dt.datetime.now().strftime("%Y%m%dT%H%M%S")
        log_file = log_dir / f"server-{os.getpid()}-{ts}.log"
    else:
        log_file.parent.mkdir(parents=True, exist_ok=True)

    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    stream = logging.StreamHandler()
    stream.setFormatter(fmt)
    fh = logging.FileHandler(log_file, mode="w", encoding="utf-8")
    fh.setFormatter(fmt)

    # Attach handlers directly to every logger we care about, rather
    # than relying on parent-propagation.  The previous attempt put
    # handlers on `clash` and relied on `clash.lobby` etc. to propagate
    # upward — but empirically `clash.lobby` lines never reached the
    # file even though `mcp.server.lowlevel.server` lines did. Direct
    # attachment plus propagate=False is the bulletproof version.
    for name in (
        "silicon",
        "silicon.lobby",
        "silicon.game",
        "silicon.engine",
        "silicon.heartbeat",
        "silicon.fog",
        "silicon.diag.sse",  # DIAG(sse) — even when off, harmless
        "silicon-serve",
        "uvicorn",
        "uvicorn.error",
        "uvicorn.access",
        "mcp",
        "mcp.server",
        "mcp.server.lowlevel.server",
    ):
        lg = logging.getLogger(name)
        lg.setLevel(getattr(logging, level))
        lg.addHandler(stream)
        lg.addHandler(fh)
        lg.propagate = False

    # Diagnostic: prove the handler is wired up before we hand off to
    # uvicorn/anyio. If this line is missing from the log file, the
    # issue is in configure_server_logging itself; if it's there but
    # subsequent clash.lobby lines aren't, some later code is detaching
    # / overriding the handler.
    logging.getLogger("silicon.lobby").info(
        "_configure_server_logging: clash.lobby wired up (pid=%d)", os.getpid()
    )
    logging.getLogger("silicon.game").info(
        "_configure_server_logging: clash.game wired up (pid=%d)", os.getpid()
    )
    return log_file


def main() -> int:
    p = argparse.ArgumentParser(description="Run the silicon-pantheon backend")
    p.add_argument(
        "--stdio",
        action="store_true",
        help="run over stdio transport instead of HTTP (for mcp-proxy / Glama)",
    )
    p.add_argument("--host", default="127.0.0.1", help="bind address (default: 127.0.0.1)")
    p.add_argument("--port", type=int, default=8080, help="bind port (default: 8080)")
    p.add_argument(
        "--log-level",
        default="INFO",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        help="server log level",
    )
    p.add_argument(
        "--log-file",
        type=Path,
        default=None,
        help=(
            "path for the server log file. Defaults to "
            "~/.silicon-pantheon/logs/server-<pid>-<timestamp>.log"
        ),
    )
    p.add_argument(
        "--allowed-host",
        action="append",
        default=[],
        help=(
            "extra hostname allowed in the HTTP Host header (FastMCP "
            "rejects unknown hosts with 421 by default to mitigate "
            "DNS-rebinding). Pass once per host you'll be reached at "
            "via a reverse proxy, e.g. --allowed-host game.example.com. "
            "127.0.0.1 / localhost / [::1] are always allowed."
        ),
    )
    p.add_argument(
        "--debug",
        action="store_true",
        help=(
            "Debug mode: invariant violations (fog leaks, hook "
            "failures, plugin exceptions, etc.) crash immediately "
            "instead of being logged and swallowed. Equivalent to "
            "setting env var SILICON_DEBUG=1. Do NOT use in "
            "production — a single bad scenario plugin will kill "
            "live matches. Note: this does NOT raise logging "
            "verbosity — use --log-level DEBUG or "
            "--log-debug-mcp-http for that."
        ),
    )
    p.add_argument(
        "--log-debug-mcp-http",
        action="store_true",
        help=(
            "Bump MCP streamable-HTTP SDK loggers to DEBUG while "
            "keeping the rest of the process at --log-level. Surfaces "
            "per-request SSE stream lifecycle (connection open / "
            "close, response write failures) without the firehose of "
            "full --log-level DEBUG. Use to diagnose 'response was "
            "dispatched but never reached the client' hangs."
        ),
    )
    p.add_argument(
        "--diagnose-sse",
        action="store_true",
        help=(
            "TEMPORARY: enable the bundled SSE-drop diagnostic — "
            "(1) log EventSourceResponse lifecycle, (2) log every "
            "MCP per-request stream cleanup with caller stack, "
            "(3) spawn tcpdump on loopback with a rolling pcap "
            "buffer in ~/.silicon-pantheon/sse-diag/. Under systemd, "
            "the unit needs 'AmbientCapabilities=CAP_NET_RAW "
            "CAP_NET_ADMIN' so tcpdump can open the raw socket "
            "without sudo (NoNewPrivileges=true blocks setuid). "
            "Remove once the 'SSE closes after 3-25 ms' bug is "
            "root-caused."
        ),
    )
    args = p.parse_args()

    if args.stdio:
        app = App()
        mcp = build_mcp_server(app)
        mcp.run(transport="stdio")
        return 0

    if args.debug:
        # The shared.debug module reads SILICON_DEBUG on every call,
        # so setting it here is enough to flip every invariant check
        # in this process to crash-mode. Deliberately separate from
        # --log-level: an operator debugging a fog leak often wants
        # crash-on-violation WITHOUT also drowning in DEBUG logs.
        os.environ["SILICON_DEBUG"] = "1"

    log_file = _configure_server_logging(args.log_level, args.log_file)
    log = logging.getLogger("silicon-serve")
    if args.debug:
        log.warning(
            "DEBUG MODE ENABLED — invariant violations will crash "
            "the server. Do not use in production."
        )
    if args.log_debug_mcp_http:
        # Child loggers inherit handlers via the `mcp` / `mcp.server`
        # root we wired up in _configure_server_logging (propagate=False
        # only blocks upward propagation to the logging root, not
        # downward inheritance). Explicitly bumping their level is
        # enough — the handler on `mcp.server` receives their records
        # because that's where propagation stops.
        for name in (
            "mcp.server.streamable_http",
            "mcp.server.streamable_http_manager",
            "mcp.server.session",
        ):
            logging.getLogger(name).setLevel(logging.DEBUG)
        log.warning(
            "MCP streamable-HTTP debug logging ENABLED — SSE stream "
            "lifecycle will be verbose."
        )
    log.info("server log file: %s", log_file)
    # Keep a single stderr line identical to the old UX for quick discovery.
    print(f"server log: {log_file}", flush=True)

    # DIAG(sse) — temporary bundled diagnostic for the "SSE TCP closes
    # after 3-25 ms" bug. Must run AFTER logging is wired so the patch
    # logs land in our log file, and BEFORE mcp is built so the
    # EventSourceResponse patch is picked up. The silicon.diag.sse
    # logger is wired by _configure_server_logging and re-attached
    # at T+3s alongside every other silicon.* logger, so no per-diag
    # handler setup is needed here.
    if args.diagnose_sse:
        from silicon_pantheon.server import sse_diagnostic
        sse_diagnostic.enable(args.port)

    # ── SIGUSR1 → thread-stack dump ──
    # Operator workflow when the server appears hung (tool handlers
    # silently stop completing, clients see HUNG heartbeats, etc.):
    #
    #     kill -USR1 $(pidof silicon-serve)
    #
    # dumps every Python thread's current stack trace to stderr, which
    # uvicorn + our logging redirects to the server log file. Pinpoints
    # exactly where each async task is parked (which lock, which
    # await, which library call) without needing to attach py-spy or
    # restart the process.
    #
    # Zero runtime cost: the signal handler is only installed; nothing
    # runs until SIGUSR1 fires.
    import faulthandler
    import signal
    faulthandler.register(signal.SIGUSR1)
    log.info(
        "SIGUSR1 handler installed — `kill -USR1 %d` to dump "
        "all thread stacks to stderr/log",
        os.getpid(),
    )

    # Route sync @mcp.tool() handlers through the anyio thread pool
    # so they don't block the event loop. Patch the MCP SDK's
    # dispatch function before building the server (FastMCP captures
    # the function reference during tool registration, so the patch
    # must land first). See server/async_tools_patch.py for details.
    from silicon_pantheon.server.async_tools_patch import (
        install as install_async_tools_patch,
    )
    install_async_tools_patch()

    app = App()
    mcp = build_mcp_server(app)

    # FastMCP owns the Starlette app + uvicorn lifecycle; it reads host/port
    # from its settings object.
    mcp.settings.host = args.host
    mcp.settings.port = args.port

    # When silicon-serve binds to localhost (typical: a reverse proxy
    # like Caddy fronts it), FastMCP auto-enables DNS-rebinding
    # protection that rejects any HTTP Host header outside
    # 127.0.0.1 / localhost / [::1]. The proxy forwards the original
    # public Host (game.example.com), which then gets
    # rejected with 421 "Invalid Host header" — silicon-join logs this
    # as "connect failed: unhandled errors in a TaskGroup".
    #
    # If the operator passed --allowed-host, append those to the
    # protection list so the public hostname is accepted while the
    # baseline localhost protection stays in place.
    if args.allowed_host:
        from mcp.server.transport_security import TransportSecuritySettings

        baseline = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
        baseline_origins = [
            "http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*",
        ]
        extra_hosts: list[str] = []
        extra_origins: list[str] = []
        for h in args.allowed_host:
            extra_hosts.extend([h, f"{h}:*"])
            extra_origins.extend([f"http://{h}", f"https://{h}",
                                  f"http://{h}:*", f"https://{h}:*"])
        mcp.settings.transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=baseline + extra_hosts,
            allowed_origins=baseline_origins + extra_origins,
        )
        log.info(
            "transport_security: allowed_hosts=%s allowed_origins=%s",
            mcp.settings.transport_security.allowed_hosts,
            mcp.settings.transport_security.allowed_origins,
        )

    # Health endpoint for fast client pre-flight validation.
    # Returns a tiny JSON body that identifies this as a Silicon Pantheon
    # server. Clients probe GET /health before attempting the full MCP
    # handshake — catches wrong URLs in ~100ms.
    from starlette.responses import JSONResponse

    @mcp.custom_route("/health", methods=["GET"])
    @mcp.custom_route("/ping", methods=["GET"])
    async def _health(request):
        return JSONResponse({
            "server": "silicon-pantheon",
            "status": "ok",
        })

    @mcp.custom_route("/api/leaderboard", methods=["GET"])
    async def _api_leaderboard(request):
        from silicon_pantheon.server.leaderboard import query_leaderboard

        _FIGHTER_NAMES = {
            "anthropic": "Claude",
            "openai": "GPT-5",
            "openai-codex": "GPT-5",
            "xai": "Grok",
            "google": "Gemini",
            "qwen": "Qwen",
            "deepseek": "DeepSeek",
            "mistral": "Mistral",
            "groq": "Groq",
        }

        rows = query_leaderboard()

        total_matches = sum(r["games"] for r in rows) // 2
        models = []
        for r in rows:
            games = r["games"]
            win_rate = r["wins"] / games if games else 0.0
            avg_think_ms = round(
                r["avg_think_time_s"] * 1000
            ) if r["avg_think_time_s"] else 0
            fighter = _FIGHTER_NAMES.get(r["provider"], r["provider"])
            models.append({
                "id": f"{r['provider']}:{r['model']}",
                "name": r["model"],
                "provider": fighter,
                "wins": r["wins"],
                "losses": r["losses"],
                "draws": r["draws"],
                "games": games,
                "win_rate": round(win_rate, 4),
                "avg_think_ms": avg_think_ms,
            })
        models.sort(key=lambda m: (-m["win_rate"], -m["games"]))

        resp = JSONResponse({
            "updated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
            "total_matches": total_matches,
            "models": models,
        })
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Cache-Control"] = "public, max-age=15"
        return resp

    log.info("silicon-serve starting on http://%s:%d", args.host, args.port)

    # Launch the heartbeat sweeper as a background task on the same
    # asyncio loop FastMCP will use. We do this via a one-off anyio
    # shim so it runs for the lifetime of mcp.run().
    import anyio

    from silicon_pantheon.server.heartbeat import run_sweep_loop

    # uvicorn's dictConfig() destroys all loggers and handlers we set
    # up above. The only reliable fix: create a BRAND NEW FileHandler
    # after uvicorn has finished its startup, and attach it to every
    # logger we care about. We save the log file path (not the handler
    # object) since the path survives dictConfig.
    _log_file_path = str(log_file)

    async def _reattach_handlers() -> None:
        """Attach our file handler to every logger we care about
        after uvicorn/mcp has finished its own logging setup.

        Runs once at T+3s. If we observe the file handler going
        stale mid-process (silicon.game stops emitting despite the
        logger still existing), the right response is to *investigate
        the root cause*, not to periodically re-attach and mask it.
        An earlier patch added a 5-minute re-attach loop; we reverted
        that so a future failure stays visible as a real silence
        rather than a rolling 5-minute recovery."""
        await asyncio.sleep(3.0)
        fmt = logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        fh = logging.FileHandler(_log_file_path, mode="a", encoding="utf-8")
        fh.setFormatter(fmt)
        target_loggers = (
            "silicon", "silicon.lobby", "silicon.game", "silicon.engine",
            "silicon.fog", "silicon.heartbeat",
            "silicon.diag.sse",  # DIAG(sse)
            "silicon-serve", "silicon.leaderboard", "silicon.host",
            "silicon.transport",
            "mcp", "mcp.server", "mcp.server.lowlevel.server",
        )
        for name in target_loggers:
            lg = logging.getLogger(name)
            lg.addHandler(fh)
            lg.setLevel(logging.INFO)
            lg.propagate = False
        logging.getLogger("silicon-serve").info(
            "re-attached NEW FileHandler to %d loggers (file=%s)",
            len(target_loggers), _log_file_path,
        )

    import asyncio  # for the reattach sleep

    # Supervisor wrappers for sibling tasks in _serve's task group.
    # anyio cancels the whole group on any one task's exception, which
    # would take the MCP server down with it. The sweeper and
    # reattach-handlers are auxiliary — a bug in either should not
    # kill live matches. Log and return instead. mcp.run_streamable_http_async
    # is NOT wrapped: if the main server crashes, that's a real
    # outage and should propagate so systemd restarts us.
    async def _supervised_sweeper() -> None:
        try:
            await run_sweep_loop(app)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("heartbeat sweeper crashed: %s — the MCP "
                          "server will continue but evictions are now "
                          "disabled until restart", e)

    async def _supervised_reattach() -> None:
        try:
            await _reattach_handlers()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("log reattach crashed: %s — logging may be "
                          "partial but the server continues", e)

    async def _serve() -> None:
        async with anyio.create_task_group() as tg:
            tg.start_soon(_supervised_sweeper)
            tg.start_soon(_supervised_reattach)
            tg.start_soon(mcp.run_streamable_http_async)

    anyio.run(_serve)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
