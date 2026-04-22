"""Offload sync ``@mcp.tool()`` handlers onto a thread pool.

FastMCP's ``FuncMetadata.call_fn_with_arg_validation`` dispatches sync
tool functions directly on the asyncio event loop thread (see
``mcp/server/fastmcp/utilities/func_metadata.py``). With 39 sync tools
and dozens of concurrent connections, this serializes every
tool-level decision on a single thread and blocks SSE writes, the
heartbeat sweeper, and incoming-request accept during each tool's
execution window.

Converting every ``def`` to ``async def`` per-tool is a 39-site
mechanical change that preserves almost none of the per-tool logic
(the bodies are already thread-safe via ``state_lock`` / ``session.lock``).
Instead we patch the one SDK entrypoint to route sync tools through
``anyio.to_thread.run_sync``, keeping the event loop free for
concurrent work.

Correctness considerations:
  * ``state_lock`` is a ``threading.RLock``; acquisition from worker
    threads is supported and semantically unchanged.
  * ``session.lock`` is a ``threading.Lock``; ditto.
  * Sweeper still runs on the event loop and acquires ``state_lock``
    briefly via ``with app.state_lock():``. Under contention with a
    thread-pool holder it will block, but tool-side hold times are
    microseconds (Phase 1 resolve only), so contention windows are
    effectively nil.
  * FastMCP's tool schema introspection uses
    ``inspect.iscoroutinefunction(fn)`` to decide the sync/async
    branch. We intercept at the branch point, not at tool registration,
    so the introspection is unchanged.
  * Thread-pool default is 40 concurrent threads (anyio). Sufficient
    for our current scale; worth revisiting if we push past ~30
    concurrent in-flight tool calls.

Idempotent: calling ``install()`` twice is a no-op. Safe to call
unconditionally at server startup.
"""

from __future__ import annotations

import logging
from functools import partial
from typing import Any

_log = logging.getLogger("silicon.server.async_tools")

_INSTALLED = False


def install() -> None:
    """Replace FastMCP's sync-tool dispatch with a thread-pool offload.

    Call once at silicon-serve startup. No-op if already installed.
    """
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    import anyio
    from mcp.server.fastmcp.utilities import func_metadata as _fm

    original = _fm.FuncMetadata.call_fn_with_arg_validation

    async def _patched(
        self: Any,
        fn: Any,
        fn_is_async: bool,
        arguments_to_validate: dict,
        arguments_to_pass_directly: dict | None,
    ) -> Any:
        # Replicate the SDK's arg-validation path exactly; the only
        # difference from the original is the sync branch (see
        # func_metadata.py:92-95).
        arguments_pre_parsed = self.pre_parse_json(arguments_to_validate)
        arguments_parsed_model = self.arg_model.model_validate(arguments_pre_parsed)
        arguments_parsed_dict = arguments_parsed_model.model_dump_one_level()
        arguments_parsed_dict |= arguments_to_pass_directly or {}

        if fn_is_async:
            return await fn(**arguments_parsed_dict)
        # Sync tool: offload to worker thread so the event loop
        # stays free for concurrent SSE writes, new request accepts,
        # heartbeat sweeper, and other in-flight tool calls.
        return await anyio.to_thread.run_sync(
            partial(fn, **arguments_parsed_dict)
        )

    _fm.FuncMetadata.call_fn_with_arg_validation = _patched
    _log.info(
        "async-tools-patch installed: sync @mcp.tool() handlers will "
        "run on the anyio thread pool"
    )
