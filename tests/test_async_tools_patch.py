"""Smoke test for the async-tools-patch.

Confirms that after ``install()``:
  - ``FuncMetadata.call_fn_with_arg_validation`` is replaced
  - sync tool functions passed through the patched dispatcher run on
    a worker thread, not the asyncio event-loop thread
  - async tool functions still run on the event-loop thread
  - ``install()`` is idempotent (safe to call twice)
"""

from __future__ import annotations

import asyncio
import threading

import pytest


def _event_loop_thread_name() -> str:
    # asyncio runs the loop on the calling thread; for tests this is
    # the main thread.
    return threading.current_thread().name


@pytest.mark.asyncio
async def test_sync_tool_runs_in_thread_pool():
    from silicon_pantheon.server.async_tools_patch import install
    install()

    from mcp.server.fastmcp.utilities.func_metadata import func_metadata

    event_loop_thread = _event_loop_thread_name()

    def sync_tool() -> dict:
        return {"thread_name": threading.current_thread().name}

    meta = func_metadata(sync_tool)
    result = await meta.call_fn_with_arg_validation(
        sync_tool, fn_is_async=False,
        arguments_to_validate={}, arguments_to_pass_directly={},
    )
    assert result["thread_name"] != event_loop_thread, (
        f"sync tool ran on event loop thread ({event_loop_thread}); "
        f"patch did not offload"
    )


@pytest.mark.asyncio
async def test_async_tool_still_runs_inline():
    from silicon_pantheon.server.async_tools_patch import install
    install()

    from mcp.server.fastmcp.utilities.func_metadata import func_metadata

    event_loop_thread = _event_loop_thread_name()

    async def async_tool() -> dict:
        return {"thread_name": threading.current_thread().name}

    meta = func_metadata(async_tool)
    result = await meta.call_fn_with_arg_validation(
        async_tool, fn_is_async=True,
        arguments_to_validate={}, arguments_to_pass_directly={},
    )
    assert result["thread_name"] == event_loop_thread, (
        "async tool should run on event-loop thread, not in pool"
    )


def test_install_is_idempotent():
    from silicon_pantheon.server.async_tools_patch import install

    from mcp.server.fastmcp.utilities import func_metadata as _fm

    install()
    first = _fm.FuncMetadata.call_fn_with_arg_validation
    install()
    second = _fm.FuncMetadata.call_fn_with_arg_validation
    assert first is second, "install() should be a no-op the second time"
