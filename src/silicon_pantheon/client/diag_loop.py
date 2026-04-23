"""Event-loop stall watchdog (debug-only).

Started implicitly by ``transport.ServerClient.connect()`` when
``SILICON_DEBUG=1`` (set by ``silicon-host --debug`` /
``silicon-join --debug``). Sleeps 100 ms and measures real
wake-up latency; if the event loop was blocked for more than the
thresholds below, logs a warning — and for severe stalls, dumps
the main-thread Python stack via ``faulthandler`` so the next
rerun of a "client went silent → server heartbeat-evicted"
incident has a concrete pointer to the sync callsite that hogged
the loop.

Why a separate task: the TUI ticker already has a stall detector
(``tui/app.py::_ticker``) but headless ``silicon-host`` bots
don't run the TUI, and even the TUI detector only prints the
stall size — it can't tell you WHERE the loop was stuck. This
watchdog adds the stack sample and works under both entrypoints.

Overhead: one ``asyncio.sleep(0.1)`` wake + one ``time.monotonic``
read every 100 ms. Still cheap enough to leave on in prod, but
gated behind ``--debug`` for now because each stall fires a log
line.

See ``heartbeat-starvation-plan.md`` or the 2026-04-22 "09_troy /
23_astronomy_tower" investigation for the use case this was built
for.
"""

from __future__ import annotations

import asyncio
import faulthandler
import gc
import logging
import os
import resource
import sys
import time

log = logging.getLogger("silicon.diag.loop")

# Tunable thresholds. The heartbeat interval is 10 s and the server's
# HEARTBEAT_DEAD_S is 45 s; we care about stalls that can plausibly
# eat one 10-s tick, so warn at 0.5 s and fire a stack dump at 2 s.
_WATCHDOG_INTERVAL_S = 0.1
_STALL_WARN_MS = 500
_STALL_DUMP_MS = 2000

# asyncio debug mode: log any callback that ran for longer than this
# without yielding. Catches slow log formatting, slow deepcopy, slow
# deserialize, slow anything — whichever coroutine held the loop.
_SLOW_CALLBACK_S = 0.5

# Log only GC pauses bigger than this. Generation-0 collections are
# every few seconds and usually sub-ms; gen-2 can pause hundreds of
# ms or more on heap-heavy workloads (big JSON state + long message
# histories). If a 10-s stall correlates with a gen-2 collection,
# this log makes that obvious.
_GC_WARN_MS = 100

# Periodic process-level snapshot cadence.
_PROC_SAMPLE_S = 30.0


def _enabled() -> bool:
    return os.environ.get("SILICON_DEBUG") == "1"


_watchdog_task: asyncio.Task | None = None
_sampler_task: asyncio.Task | None = None
_gc_installed = False
_gc_start_at: dict[int, float] = {}


async def _watchdog_loop() -> None:
    """Wake every 100 ms; log stalls; arm faulthandler for severe ones.

    Key trick: ``faulthandler.dump_traceback_later(N)`` schedules a
    stack dump N seconds from now using a background C-level thread
    that DOES NOT depend on the Python event loop. If the loop
    stays responsive we cancel the pending dump on each tick, so
    nothing gets printed. If the loop blocks for longer than
    ``_STALL_DUMP_MS``, the dump fires IN PLACE — while the main
    thread is still wedged — and we get the actual stack of the
    sync work that was blocking. ``all_threads=True`` because the
    culprit can be the httpx / asyncio default-executor thread pool
    doing sync work on behalf of our coroutines (DNS, SSL, etc.).
    This is the only way to sample a stalled event loop's stack
    from Python; waiting for the watchdog coroutine to wake up and
    then dumping shows only the watchdog itself, which is useless
    for root-causing.
    """
    # Arm faulthandler so dump_traceback_later works. Idempotent.
    try:
        faulthandler.enable()
    except Exception:  # noqa: BLE001
        pass
    # Dumps go to stderr. silicon-host / silicon-join redirect stderr
    # into the per-process log file, so these end up in the same log
    # stream as our other DIAG lines.
    dump_file = sys.stderr
    dump_secs = _STALL_DUMP_MS / 1000.0
    last = time.monotonic()
    try:
        faulthandler.dump_traceback_later(
            dump_secs, file=dump_file, repeat=False,
        )
    except Exception:  # noqa: BLE001
        pass
    while True:
        await asyncio.sleep(_WATCHDOG_INTERVAL_S)
        now = time.monotonic()
        stall_ms = (now - last - _WATCHDOG_INTERVAL_S) * 1000
        last = now
        try:
            faulthandler.cancel_dump_traceback_later()
            faulthandler.dump_traceback_later(
                dump_secs, file=dump_file, repeat=False,
            )
        except Exception:  # noqa: BLE001
            pass
        if stall_ms <= _STALL_WARN_MS:
            continue
        total_ms = stall_ms + _WATCHDOG_INTERVAL_S * 1000
        if stall_ms >= _STALL_DUMP_MS:
            log.warning(
                "DIAG loop-stall: event-loop blocked for %.0fms "
                "(watchdog). Full-process stack should be dumped to "
                "stderr just above this line "
                "(faulthandler.dump_traceback_later, all_threads=True).",
                total_ms,
            )
        else:
            log.warning(
                "DIAG loop-stall: event-loop blocked for %.0fms (watchdog).",
                total_ms,
            )


async def _sampler_loop() -> None:
    """Every _PROC_SAMPLE_S: dump RSS / loadavg / FD count.

    Correlates a stall with the machine-level situation: is the
    process RSS ballooning (→ swap / paging), is the system
    loadavg spiking (→ sibling process contention), is the FD
    count climbing (→ leak tying up kernel state)? If a 10-s
    stall lines up with swap, RSS will jump between samples; if
    it lines up with contention, loadavg will exceed CPU count.
    """
    pid = os.getpid()
    # Linux ``getrusage`` returns ru_maxrss in KB. On macOS it's
    # bytes; we're on Linux in the deploy target but guard anyway.
    rss_divisor = 1024 if sys.platform == "linux" else 1024 * 1024
    while True:
        await asyncio.sleep(_PROC_SAMPLE_S)
        try:
            rss_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / rss_divisor
            try:
                loads = os.getloadavg()
            except OSError:
                loads = (-1.0, -1.0, -1.0)
            try:
                fds = len(os.listdir(f"/proc/{pid}/fd"))
            except OSError:
                fds = -1
            try:
                # Count live tasks and pending callbacks on the loop.
                loop = asyncio.get_running_loop()
                tasks = len(asyncio.all_tasks(loop))
            except Exception:  # noqa: BLE001
                tasks = -1
            log.info(
                "DIAG process: pid=%d rss_mb=%.0f loadavg=%.2f,%.2f,%.2f "
                "fds=%d asyncio_tasks=%d",
                pid, rss_mb, loads[0], loads[1], loads[2], fds, tasks,
            )
        except Exception as e:  # noqa: BLE001
            log.debug("DIAG process sampler failed: %s", e)


def _gc_callback(phase: str, info: dict) -> None:
    """Time every GC pass; warn on slow ones.

    Python's generational GC can pause for hundreds of ms on a
    heap full of nested dicts (which is exactly what a long
    message history + big JSON game states look like). If a
    10-s stall correlates with a ``DIAG gc stop`` line of
    similar duration, we have a gc-pause root cause.
    """
    now = time.monotonic()
    gen = info.get("generation", -1)
    if phase == "start":
        _gc_start_at[gen] = now
        return
    # phase == "stop"
    t0 = _gc_start_at.pop(gen, None)
    if t0 is None:
        return
    dur_ms = (now - t0) * 1000
    if dur_ms < _GC_WARN_MS:
        return
    log.warning(
        "DIAG gc stop: gen=%d duration_ms=%.0f collected=%d uncollectable=%d",
        gen, dur_ms, info.get("collected", 0), info.get("uncollectable", 0),
    )


def start() -> None:
    """Launch watchdog + sampler + GC callback + asyncio debug mode.

    Idempotent. No-op when ``SILICON_DEBUG`` is off, or called
    outside a running loop.
    """
    global _watchdog_task, _sampler_task, _gc_installed
    if not _enabled():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return

    # 1. asyncio debug mode + slow-callback warning. The loop logs
    # "Executing <Handle coro=...> took X.Xs" for any callback
    # that holds the loop for > _SLOW_CALLBACK_S without yielding.
    # Highest-signal, zero-maintenance: whichever file:line is in
    # the warning is the exact coroutine that hogged the loop.
    try:
        loop.set_debug(True)
        loop.slow_callback_duration = _SLOW_CALLBACK_S  # type: ignore[attr-defined]
        # asyncio's own logger lives at logging.getLogger("asyncio").
        # Make sure it's at WARNING so the slow-callback messages land.
        logging.getLogger("asyncio").setLevel(logging.WARNING)
    except Exception:  # noqa: BLE001
        pass

    # 2. GC pause logging.
    if not _gc_installed:
        try:
            gc.callbacks.append(_gc_callback)
            _gc_installed = True
        except Exception:  # noqa: BLE001
            pass

    # 3. Watchdog task (stall detector + faulthandler stack dumps).
    if _watchdog_task is None or _watchdog_task.done():
        _watchdog_task = loop.create_task(_watchdog_loop())

    # 4. Sampler task (RSS / loadavg / FDs / task count).
    if _sampler_task is None or _sampler_task.done():
        _sampler_task = loop.create_task(_sampler_loop())

    log.info(
        "loop-watchdog started (warn>%dms dump-stack>%dms interval=%dms); "
        "asyncio slow_callback_duration=%.1fs; gc warn>%dms; "
        "proc sampler every %.0fs",
        _STALL_WARN_MS, _STALL_DUMP_MS, int(_WATCHDOG_INTERVAL_S * 1000),
        _SLOW_CALLBACK_S, _GC_WARN_MS, _PROC_SAMPLE_S,
    )
