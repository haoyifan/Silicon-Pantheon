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


# Set by _shutdown_exception_handler when MCP/anyio raises a
# cross-task cancel-scope error during loop teardown. main() reads it
# after asyncio.run returns and promotes the process exit code so
# the systemtest orchestrator (and any other supervisor) doesn't
# count the run as "clean exit" when teardown actually crashed.
_unclean_shutdown_detected = False


def _install_shutdown_handler() -> None:
    """Trap anyio cancel-scope mismatches that fire on loop shutdown.

    Background: BotWorker._disconnect uses ``asyncio.wait_for(
    transport_ctx.__aexit__(...), timeout=5.0)`` to bound how long a
    dead transport can hold up reconnect (the underlying SSE stream
    may already be wedged when teardown starts; without the bound we
    hang forever in __aexit__). When the timeout fires the
    streamablehttp_client async generator is left half-closed.
    Asyncio's loop shutdown then runs ``athrow`` on it from its own
    cleanup task, but the generator's anyio cancel scope was entered
    in our worker task, so anyio raises:

        RuntimeError: Attempted to exit cancel scope in a different
        task than it was entered in

    The damage at that point is cosmetic — the leave_room / DELETE
    /mcp calls already succeeded server-side before teardown began.
    But asyncio's default handler logs a multi-frame ERROR traceback
    that looks like a real bug, AND Python still exits 0 because the
    main coroutine returned normally. Combined those mislead
    operators (and the systemtest orchestrator) into reporting
    "clean exit" when the bot's shutdown actually went sideways.

    We can't fix the root anyio/MCP cross-task teardown without
    forking either lib, but we CAN:

      1. Demote the cancel-scope traceback to a single WARNING line.
      2. Flip a module-global so main() can promote the exit code.

    Anything else (real bugs unrelated to MCP teardown) flows through
    asyncio's default handler unchanged.
    """
    log = logging.getLogger("silicon.host.runner")
    loop = asyncio.get_running_loop()
    default = loop.get_exception_handler()

    def handler(loop_, context):
        exc = context.get("exception")
        msg = str(exc) if exc is not None else context.get("message", "")
        if (
            isinstance(exc, RuntimeError)
            and "cancel scope" in msg.lower()
        ):
            global _unclean_shutdown_detected
            _unclean_shutdown_detected = True
            log.warning(
                "shutdown: anyio cancel-scope mismatch in MCP "
                "client teardown (known cross-task GC race; bot "
                "work already completed but exit code will be "
                "promoted to 1 so supervisors see the unclean "
                "shutdown). exc=%s",
                exc,
            )
            return
        if default is not None:
            default(loop_, context)
        else:
            loop_.default_exception_handler(context)

    loop.set_exception_handler(handler)


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
    _install_shutdown_handler()
    workers = [
        BotWorker(i, wc, config.server_url)
        for i, wc in enumerate(config.workers)
    ]
    tasks = [
        asyncio.create_task(w.run_forever())
        for w in workers
    ]

    # Status line refresh loop. Exits when all worker tasks have
    # completed — normally a no-op for long-running auto-host (where
    # workers run forever), but required for one_shot workers so the
    # process actually exits after the bounded workload finishes.
    try:
        while True:
            line = _status_line(workers)
            # Clear line and print status.
            sys.stdout.write(f"\r\033[K{line}")
            sys.stdout.flush()
            if all(t.done() for t in tasks):
                break
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

    # Preflight: every worker's provider + model must be able to
    # construct an adapter. Failing fast here beats discovering mid-
    # match that an API key is missing, which would let the worker
    # publish an unplayable room and forfeit real opponents. See
    # host/preflight.py for the full rationale.
    from silicon_pantheon.host.preflight import (
        format_failure_report,
        validate_credentials,
    )
    failures = validate_credentials(config)
    if failures:
        sys.stderr.write(format_failure_report(failures, len(config.workers)))
        raise SystemExit(2)

    log_file = args.log or config.log_file
    _setup_logging(log_file)

    # ── SIGUSR1 → thread-stack dump ──
    # Same rationale as silicon-serve: when a worker silently stops
    # making tool calls (the Vegetable-stuck-forever pattern from
    # 2026-04-20), the operator can:
    #
    #     kill -USR1 $(pidof silicon-host)
    #
    # to dump every Python thread's stack trace, revealing where each
    # worker task is parked. No restart needed to collect evidence.
    import faulthandler
    import os as _os
    import signal
    faulthandler.register(signal.SIGUSR1)

    print(
        f"SiliconPantheon auto-host: {len(config.workers)} workers, "
        f"server={config.server_url}, log={log_file}"
    )
    print(f"pid={_os.getpid()} — `kill -USR1 {_os.getpid()}` for a thread dump")
    print("Press Ctrl+C to stop.\n")

    try:
        asyncio.run(_run(config))
    except KeyboardInterrupt:
        print("\nShutting down.")

    # If the loop's exception handler caught an anyio cancel-scope
    # crash during teardown, surface it as a non-zero exit so the
    # systemtest orchestrator (and the operator) can tell the bot's
    # shutdown went sideways even though the main coroutine returned
    # normally. See _install_shutdown_handler for the rationale.
    if _unclean_shutdown_detected:
        sys.exit(1)


if __name__ == "__main__":
    main()
