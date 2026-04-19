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
    args = p.parse_args()

    log_file = _configure_server_logging(args.log_level, args.log_file)
    log = logging.getLogger("silicon-serve")
    log.info("server log file: %s", log_file)
    # Keep a single stderr line identical to the old UX for quick discovery.
    print(f"server log: {log_file}", flush=True)

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
    async def _health(request):
        return JSONResponse({
            "server": "silicon-pantheon",
            "status": "ok",
        })

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
        await asyncio.sleep(3.0)
        fmt = logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        fh = logging.FileHandler(_log_file_path, mode="a", encoding="utf-8")
        fh.setFormatter(fmt)
        target_loggers = (
            "silicon", "silicon.lobby", "silicon.game", "silicon.engine",
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

    async def _serve() -> None:
        async with anyio.create_task_group() as tg:
            tg.start_soon(run_sweep_loop, app)
            tg.start_soon(_reattach_handlers)
            tg.start_soon(mcp.run_streamable_http_async)

    anyio.run(_serve)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
