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
import logging

from clash_of_robots.server.app import App, build_mcp_server


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
    args = p.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    log = logging.getLogger("clash-serve")

    app = App()
    mcp = build_mcp_server(app)

    # FastMCP owns the Starlette app + uvicorn lifecycle; it reads host/port
    # from its settings object.
    mcp.settings.host = args.host
    mcp.settings.port = args.port

    log.info("clash-serve starting on http://%s:%d", args.host, args.port)
    mcp.run(transport="streamable-http")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
