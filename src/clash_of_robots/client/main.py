"""`clash-join` CLI entry — connects to a backend and runs a smoke flow.

Phase 1a scope: connect, declare player metadata, call whoami, send a
heartbeat, print results, exit. The full lobby/game loop lands in
later sub-phases.

Usage:
    clash-join --url http://localhost:8080/mcp/ \\
               --name alice --kind ai --provider anthropic --model claude-haiku-4-5
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from clash_of_robots.client.transport import ServerClient


async def _smoke(url: str, display_name: str, kind: str, provider: str | None, model: str | None) -> int:
    async with ServerClient.connect(url) as client:
        print(f"connected: connection_id={client.connection_id}")
        r = await client.call(
            "whoami",
        )
        print(f"whoami (pre): {json.dumps(r)}")
        r = await client.call(
            "set_player_metadata",
            display_name=display_name,
            kind=kind,
            provider=provider,
            model=model,
        )
        print(f"set_player_metadata: {json.dumps(r)}")
        r = await client.call("heartbeat")
        print(f"heartbeat: {json.dumps(r)}")
        r = await client.call("whoami")
        print(f"whoami (post): {json.dumps(r)}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Connect to clash-serve and run a smoke flow")
    p.add_argument(
        "--url",
        default="http://127.0.0.1:8080/mcp/",
        help="MCP streamable-HTTP endpoint (default: http://127.0.0.1:8080/mcp/)",
    )
    p.add_argument("--name", required=True, help="display name")
    p.add_argument("--kind", default="ai", choices=("ai", "human", "hybrid"))
    p.add_argument("--provider", default=None)
    p.add_argument("--model", default=None)
    args = p.parse_args()

    try:
        return asyncio.run(
            _smoke(
                url=args.url,
                display_name=args.name,
                kind=args.kind,
                provider=args.provider,
                model=args.model,
            )
        )
    except (KeyboardInterrupt, SystemExit):
        return 130
    except Exception as e:
        print(f"smoke flow failed: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
