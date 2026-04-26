"""`silicon-join` CLI entry.

Default mode is the full TUI (login → lobby → room → game → post-match).
The original Phase 1a smoke flow is available under `--smoke` for
testing raw connectivity without driving the TUI.

Usage:
    silicon-join                              # interactive TUI
    silicon-join --url http://host:8080/mcp/  # preseed the URL field
    silicon-join --name alice                 # preseed the display-name field
    silicon-join --smoke --name alice         # legacy smoke flow
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from silicon_pantheon.client.transport import ServerClient


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
        from silicon_pantheon.shared.protocol import PROTOCOL_VERSION

        r = await client.call(
            "set_player_metadata",
            display_name=display_name,
            kind=kind,
            provider=provider,
            model=model,
            client_protocol_version=PROTOCOL_VERSION,
        )
        print(f"set_player_metadata: {json.dumps(r)}")
        r = await client.call("heartbeat")
        print(f"heartbeat: {json.dumps(r)}")
        r = await client.call("whoami")
        print(f"whoami (post): {json.dumps(r)}")
    return 0


def _configure_client_logging(display_name_hint: str | None) -> Path:
    """TUI takes over the terminal (screen=True), so logs must go to a
    file instead of stderr or they'd be invisible.

    Each silicon-join process gets its own file so two concurrent clients
    never interleave their output. Filename is
      client-<slug>-<pid>-<YYYYMMDDTHHMMSS>.log
    where <slug> comes from the --name flag (if provided) or "anon".
    Returns the chosen path so the caller can print it before the TUI
    takes over the terminal.
    """
    import datetime as _dt
    import logging
    import os
    import re
    from pathlib import Path

    log_dir = Path.home() / ".silicon-pantheon" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    slug_src = (display_name_hint or "anon").strip().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", slug_src).strip("-") or "anon"
    ts = _dt.datetime.now().strftime("%Y%m%dT%H%M%S")
    log_path = log_dir / f"client-{slug}-{os.getpid()}-{ts}.log"

    handler = logging.FileHandler(log_path, mode="w", encoding="utf-8")
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )

    # Attach to the `clash` namespace rather than root — mirrors the
    # server's logging setup and means any `logging.basicConfig()` call
    # made by a library (the MCP client, httpx, asyncio) cannot wipe
    # our handler.
    silicon_logger = logging.getLogger("silicon")
    if not any(
        getattr(h, "baseFilename", None) == str(log_path) for h in silicon_logger.handlers
    ):
        silicon_logger.addHandler(handler)
    silicon_logger.setLevel(logging.INFO)
    silicon_logger.propagate = False

    # Also tee library-level diagnostics into our file so tracebacks from
    # the MCP client / httpx / asyncio end up alongside our lines.
    for name in ("mcp", "httpx", "asyncio", "client"):
        lg = logging.getLogger(name)
        lg.setLevel(logging.INFO)
        lg.propagate = False
        if not any(
            getattr(h, "baseFilename", None) == str(log_path) for h in lg.handlers
        ):
            lg.addHandler(handler)

    silicon_logger.info(
        "---- silicon-join session started pid=%d log=%s ----", os.getpid(), log_path
    )
    return log_path


def _run_tui(
    url: str | None,
    name: str | None,
    kind: str,
    provider: str | None,
    model: str | None,
    strategy: str | None,
) -> int:
    from pathlib import Path

    from silicon_pantheon.client.tui.app import TUIApp
    from silicon_pantheon.client.tui.screens.language_picker import LanguagePickerScreen
    from silicon_pantheon.harness.prompts import load_strategy

    log_path = _configure_client_logging(display_name_hint=name)
    # Print BEFORE the TUI takes the terminal so the user can grep/tail.
    print(f"client log: {log_path}", flush=True)

    # Language picker fires first; it transitions to the login/provider
    # screen after the user picks a locale.
    app = TUIApp(initial_screen_factory=LanguagePickerScreen)
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
    if strategy:
        path = Path(strategy)
        app.state.strategy_path = path
        app.state.strategy_text = load_strategy(path)
    return asyncio.run(app.run())


def main() -> int:
    p = argparse.ArgumentParser(
        description="Connect to silicon-serve (TUI by default; --smoke for a connectivity probe)"
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
        "--strategy",
        default=None,
        help="path to a STRATEGY.md playbook injected into the agent's system prompt",
    )
    p.add_argument(
        "--smoke",
        action="store_true",
        help="skip the TUI and run a non-interactive connectivity probe",
    )
    p.add_argument(
        "--debug",
        action="store_true",
        help=(
            "Debug mode: invariant violations crash instead of being "
            "swallowed. Equivalent to SILICON_DEBUG=1. Useful for "
            "reproducing reported bugs; do not use as a default."
        ),
    )
    args = p.parse_args()

    if args.debug:
        import os as _os
        _os.environ["SILICON_DEBUG"] = "1"
        print(
            "DEBUG MODE: invariant violations will crash the client.",
            file=sys.stderr,
        )

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
            strategy=args.strategy,
        )
    except (KeyboardInterrupt, SystemExit):
        return 130
    except Exception as e:
        print(f"TUI error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
