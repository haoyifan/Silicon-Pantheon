"""`clash-join` CLI entry.

Default mode is the full TUI (login → lobby → room → game → post-match).
The original Phase 1a smoke flow is available under `--smoke` for
testing raw connectivity without driving the TUI.

Usage:
    clash-join                              # interactive TUI
    clash-join --url http://host:8080/mcp/  # preseed the URL field
    clash-join --name alice                 # preseed the display-name field
    clash-join --smoke --name alice         # legacy smoke flow
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from clash_of_robots.client.transport import ServerClient


async def _smoke(
    url: str,
    display_name: str,
    kind: str,
    provider: str | None,
    model: str | None,
) -> int:
    async with ServerClient.connect(url) as client:
        print(f"connected: connection_id={client.connection_id}")
        r = await client.call("whoami")
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


def _run_tui(
    url: str | None,
    name: str | None,
    kind: str,
    provider: str | None,
    model: str | None,
) -> int:
    from clash_of_robots.client.tui.app import TUIApp
    from clash_of_robots.client.tui.screens.login import LoginScreen

    app = TUIApp(initial_screen_factory=LoginScreen)
    if url:
        app.state.server_url = url
    if name:
        app.state.display_name = name
    if kind:
        app.state.kind = kind
    if provider:
        app.state.provider = provider
    if model:
        app.state.model = model
    return asyncio.run(app.run())


def main() -> int:
    p = argparse.ArgumentParser(
        description="Connect to clash-serve (TUI by default; --smoke for a connectivity probe)"
    )
    p.add_argument(
        "--url",
        default=None,
        help="MCP streamable-HTTP endpoint (default: http://127.0.0.1:8080/mcp/)",
    )
    p.add_argument("--name", default=None, help="display name")
    p.add_argument("--kind", default="ai", choices=("ai", "human", "hybrid"))
    p.add_argument("--provider", default=None)
    p.add_argument("--model", default=None)
    p.add_argument(
        "--smoke",
        action="store_true",
        help="skip the TUI and run a non-interactive connectivity probe",
    )
    args = p.parse_args()

    if args.smoke:
        if not args.name:
            print("--smoke requires --name", file=sys.stderr)
            return 2
        try:
            return asyncio.run(
                _smoke(
                    url=args.url or "http://127.0.0.1:8080/mcp/",
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

    try:
        return _run_tui(
            url=args.url,
            name=args.name,
            kind=args.kind,
            provider=args.provider,
            model=args.model,
        )
    except (KeyboardInterrupt, SystemExit):
        return 130
    except Exception as e:
        print(f"TUI error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
