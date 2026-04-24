"""Regression test for silicon-host's MCP/anyio shutdown handler.

The 2026-04-23 system test review surfaced bug #4: when MCP's
streamable_http_client teardown races with asyncio loop shutdown,
anyio raises ``RuntimeError: Attempted to exit cancel scope in a
different task than it was entered in`` from a GC-driven athrow.
The default asyncio handler logs a multi-frame ERROR traceback AND
Python still exits 0 because the main coroutine returned cleanly.
That combination misled the systemtest orchestrator into reporting
"clean exit" for a worker whose shutdown actually went sideways.

The fix in ``silicon_pantheon.host.runner._install_shutdown_handler``:
intercept that specific exception at the loop level, log it once at
WARNING, and flip a module-global so ``main()`` can promote the exit
code to 1.

These tests exercise the handler directly (no actual MCP transport
needed) so the behavior is locked in regardless of which lib version
trips the bug next.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from silicon_pantheon.host import runner as host_runner


@pytest.fixture(autouse=True)
def _reset_flag():
    """Each test starts with the unclean-shutdown flag cleared."""
    host_runner._unclean_shutdown_detected = False
    yield
    host_runner._unclean_shutdown_detected = False


def _install_in_loop():
    """Helper: install the handler inside an asyncio.run context."""

    async def go():
        host_runner._install_shutdown_handler()
        return asyncio.get_running_loop()

    return asyncio.run(go())


def test_handler_traps_anyio_cancel_scope_runtime_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The exact error MCP/anyio raises during cross-task teardown
    must flip the unclean-shutdown flag and log a single WARNING —
    NOT propagate to asyncio's default handler (which logs ERROR
    plus a long traceback)."""

    async def go():
        host_runner._install_shutdown_handler()
        loop = asyncio.get_running_loop()
        exc = RuntimeError(
            "Attempted to exit cancel scope in a different task "
            "than it was entered in"
        )
        with caplog.at_level(logging.WARNING, logger="silicon.host.runner"):
            loop.call_exception_handler({
                "message": "shutdown",
                "exception": exc,
            })

    asyncio.run(go())

    assert host_runner._unclean_shutdown_detected is True, (
        "handler must flip _unclean_shutdown_detected so main() "
        "promotes the exit code"
    )
    warnings = [
        r for r in caplog.records
        if r.levelno == logging.WARNING
        and "cancel-scope mismatch" in r.message
    ]
    assert len(warnings) == 1, (
        f"expected exactly one WARNING about the cancel-scope "
        f"mismatch; got: {[r.message for r in caplog.records]}"
    )


def test_handler_passes_unrelated_errors_to_default(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Anything that ISN'T the cancel-scope mismatch must reach
    asyncio's default handler (which logs ERROR). The shutdown
    suppressor must not become a generic exception swallower."""

    async def go():
        host_runner._install_shutdown_handler()
        loop = asyncio.get_running_loop()
        exc = ValueError("something else entirely")
        with caplog.at_level(logging.ERROR):
            loop.call_exception_handler({
                "message": "unrelated",
                "exception": exc,
            })

    asyncio.run(go())

    assert host_runner._unclean_shutdown_detected is False, (
        "unrelated exceptions must not flip the flag"
    )
    # asyncio.BaseEventLoop.default_exception_handler logs at ERROR
    # via the asyncio logger.
    asyncio_errors = [
        r for r in caplog.records
        if r.levelno == logging.ERROR and r.name == "asyncio"
    ]
    assert asyncio_errors, (
        f"expected default handler to fire; got: "
        f"{[(r.name, r.levelname, r.message) for r in caplog.records]}"
    )


def test_handler_message_only_with_no_exception(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Asyncio sometimes calls the exception handler with only a
    ``message`` string (no exception object) — e.g. a task that was
    never awaited. The handler must not crash on those, and must
    NOT mistake an unrelated message containing 'cancel scope' for
    the MCP shutdown bug (we gate on isinstance RuntimeError)."""

    async def go():
        host_runner._install_shutdown_handler()
        loop = asyncio.get_running_loop()
        loop.call_exception_handler({
            "message": "task contains the words cancel scope but no exception",
        })

    asyncio.run(go())

    assert host_runner._unclean_shutdown_detected is False, (
        "without a RuntimeError exception object the handler must "
        "treat this as 'not the bug we trap' and pass through"
    )
