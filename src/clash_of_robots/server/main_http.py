"""`clash-serve` CLI entry — MCP over streamable HTTP.

Separate module from `server/main.py` (which hosts the legacy stdio
MCP server for `clash-match` in-process use) so the two do not
conflict during the Phase 1 transition.

Usage:
    clash-serve                      # listen on 127.0.0.1:8080
    clash-serve --host 0.0.0.0 --port 9000
"""

from __future__ import annotations

import argparse
import datetime as _dt
import logging
import os
from pathlib import Path

from clash_of_robots.server.app import App, build_mcp_server


def _configure_server_logging(level: str, log_file: Path | None) -> Path:
    """Install stderr + file handlers on the `clash` logger namespace.

    Why not the root logger: `mcp.run_streamable_http_async` -> uvicorn
    calls `logging.basicConfig()` which wipes handlers on root. Earlier
    runs lost every `clash.lobby` / `clash.game` log line because of
    this. Attaching to "clash" with propagate=False keeps our
    instrumentation intact while still letting uvicorn / httpx run
    their own handlers however they like.
    """
    if log_file is None:
        log_dir = Path.home() / ".clash-of-robots" / "logs"
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
        "clash",
        "clash.lobby",
        "clash.game",
        "clash-serve",
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
    logging.getLogger("clash.lobby").info(
        "_configure_server_logging: clash.lobby wired up (pid=%d)", os.getpid()
    )
    logging.getLogger("clash.game").info(
        "_configure_server_logging: clash.game wired up (pid=%d)", os.getpid()
    )
    return log_file


def main() -> int:
    p = argparse.ArgumentParser(description="Run the clash-of-robots backend")
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
            "~/.clash-of-robots/logs/server-<pid>-<timestamp>.log"
        ),
    )
    args = p.parse_args()

    log_file = _configure_server_logging(args.log_level, args.log_file)
    log = logging.getLogger("clash-serve")
    log.info("server log file: %s", log_file)
    # Keep a single stderr line identical to the old UX for quick discovery.
    print(f"server log: {log_file}", flush=True)

    app = App()
    mcp = build_mcp_server(app)

    # FastMCP owns the Starlette app + uvicorn lifecycle; it reads host/port
    # from its settings object.
    mcp.settings.host = args.host
    mcp.settings.port = args.port

    log.info("clash-serve starting on http://%s:%d", args.host, args.port)

    # Launch the heartbeat sweeper as a background task on the same
    # asyncio loop FastMCP will use. We do this via a one-off anyio
    # shim so it runs for the lifetime of mcp.run().
    import anyio

    from clash_of_robots.server.heartbeat import run_sweep_loop

    async def _serve() -> None:
        async with anyio.create_task_group() as tg:
            tg.start_soon(run_sweep_loop, app)
            tg.start_soon(mcp.run_streamable_http_async)

    anyio.run(_serve)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
