"""Auto-host runner — spawns N workers and maintains them.

Usage::

    silicon-host config.toml
    silicon-host config.toml --log auto_host.log

The runner:
  1. Parses the TOML config.
  2. Sets up file logging.
  3. Spawns one async task per [[worker]].
  4. Prints a live status line to stdout (refreshed every second).
  5. On Ctrl-C, cancels all workers and exits cleanly.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from pathlib import Path

from silicon_pantheon.host.config import HostConfig, load_config
from silicon_pantheon.host.worker import BotWorker


def _setup_logging(log_file: str) -> None:
    """Configure logging to file only — stdout is for the status line."""
    handler = logging.FileHandler(log_file, encoding="utf-8")
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    # Suppress noisy httpx INFO lines.
    logging.getLogger("httpx").setLevel(logging.WARNING)


def _status_line(workers: list[BotWorker]) -> str:
    """Build a one-line status summary for all workers.

    Truncated to terminal width so \r\033[K clears correctly.
    Without truncation, long lines wrap to the next row and the
    carriage return only clears the second row — leaving ghost
    text from the previous update on the first row.
    """
    import shutil

    parts: list[str] = []
    for w in workers:
        tag = f"[{w.config.name}]"
        info = w.status
        if w.opponent:
            info += f" vs {w.opponent}"
        if w.turn_info and "turn" not in w.status:
            info += f" ({w.turn_info})"
        # While the adapter is waiting on the provider, append the
        # elapsed-since-request time so operators can tell a slow
        # provider (grok-4 reasoning, etc.) from a wedged worker.
        agent = getattr(w, "agent", None)
        elapsed = agent.adapter_elapsed_s() if agent is not None else None
        if elapsed is not None:
            info += f" [llm {elapsed:.0f}s]"
        parts.append(f"{tag} {info}")
    line = "  ".join(parts)
    cols = shutil.get_terminal_size((120, 24)).columns
    if len(line) > cols - 1:
        line = line[: cols - 4] + "…"
    return line


async def _run(config: HostConfig) -> None:
    """Spawn workers and maintain them until cancelled."""
    workers = [
        BotWorker(i, wc, config.server_url)
        for i, wc in enumerate(config.workers)
    ]
    tasks = [
        asyncio.create_task(w.run_forever())
        for w in workers
    ]

    # Status line refresh loop.
    try:
        while True:
            line = _status_line(workers)
            # Clear line and print status.
            sys.stdout.write(f"\r\033[K{line}")
            sys.stdout.flush()
            await asyncio.sleep(1.0)
    except asyncio.CancelledError:
        pass
    finally:
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except asyncio.CancelledError:
                pass
        sys.stdout.write("\n")
        sys.stdout.flush()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="SiliconPantheon auto-host — keep N bot rooms available.",
    )
    parser.add_argument(
        "config",
        type=Path,
        help="Path to the TOML config file.",
    )
    parser.add_argument(
        "--log",
        type=str,
        default=None,
        help="Log file path (overrides config).",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help=(
            "Debug mode: invariant violations crash instead of being "
            "swallowed. Equivalent to SILICON_DEBUG=1. Use for "
            "reproducing bugs; do not run as a default."
        ),
    )
    args = parser.parse_args()

    if args.debug:
        import os as _os
        _os.environ["SILICON_DEBUG"] = "1"
        print("DEBUG MODE: invariant violations will crash the bot.")

    config = load_config(args.config)
    log_file = args.log or config.log_file
    _setup_logging(log_file)

    print(
        f"SiliconPantheon auto-host: {len(config.workers)} workers, "
        f"server={config.server_url}, log={log_file}"
    )
    print("Press Ctrl+C to stop.\n")

    try:
        asyncio.run(_run(config))
    except KeyboardInterrupt:
        print("\nShutting down.")


if __name__ == "__main__":
    main()
