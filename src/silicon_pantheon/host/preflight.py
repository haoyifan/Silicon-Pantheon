"""Startup credentials preflight for silicon-host.

Before spawning any worker and opening any connection to the server,
validate that every worker's configured (provider, model) can actually
resolve to a usable adapter with the credentials present in this
process's environment. Rejects the run with a clear, actionable report
if any worker is misconfigured — prevents the "worker creates a room,
opponent joins, adapter init crashes, opponent gets forfeit win" loop
that silently traps players into unplayable matches.

Design note (2026-04-22): a runtime preflight inside the worker loop
was also considered for catching credential changes mid-run. We
deliberately don't implement that — the operational assumption is
that credentials.json does not change during a silicon-host lifetime.
If you need to update creds, stop silicon-host, update, restart.
"""

from __future__ import annotations

from dataclasses import dataclass

from silicon_pantheon.host.config import HostConfig, WorkerConfig


@dataclass
class PreflightFailure:
    """One worker that can't be started due to credential problems."""
    worker: WorkerConfig
    error: str


def validate_credentials(config: HostConfig) -> list[PreflightFailure]:
    """Try to construct each worker's provider adapter.

    Returns an empty list if every worker's credentials resolve.
    Otherwise returns one ``PreflightFailure`` per broken worker with
    the exact RuntimeError message from the adapter factory.

    The adapter factory (``_build_default_adapter``) is deterministic
    and side-effect-free at construction time for the providers we
    care about — it walks env vars + credentials.json and raises
    ``RuntimeError`` if nothing matches. No network call, no SDK
    handshake, safe to call for every worker at startup.

    Anthropic adapters don't validate the Claude SDK CLI's login
    state at construction time (the CLI's own session is checked on
    first use). That means a missing ``claude login`` won't be
    caught here; it surfaces as a ProviderError on first tool call,
    which is handled by worker.py's existing terminal-provider-error
    path. Not perfect but matches the information available at
    construction.
    """
    from silicon_pantheon.client.agent_bridge import _build_default_adapter

    failures: list[PreflightFailure] = []
    for w in config.workers:
        # Random-mode workers have no provider/LLM adapter — nothing to
        # validate. Skip them. This is the system-test path where the
        # whole point is to run without LLM credentials.
        if getattr(w, "mode", "llm") == "random":
            continue
        try:
            _build_default_adapter(w.model)
        except RuntimeError as e:
            failures.append(PreflightFailure(worker=w, error=str(e)))
        except Exception as e:
            # Unexpected (import error, malformed credentials file,
            # etc.). Record it but don't crash preflight itself —
            # report every broken worker in one pass so the operator
            # gets a complete picture on the first run.
            failures.append(
                PreflightFailure(
                    worker=w,
                    error=f"unexpected error during adapter construction: "
                          f"{type(e).__name__}: {e}",
                )
            )
    return failures


def format_failure_report(
    failures: list[PreflightFailure], total_workers: int
) -> str:
    """Human-readable summary suitable for stderr.

    Lists each failing worker with its model/provider and the error
    message. Ends with hints about how to resolve.
    """
    lines = [
        f"silicon-host: refusing to start — "
        f"{len(failures)} of {total_workers} workers have unresolved "
        "credentials.",
        "",
    ]
    for f in failures:
        lines.append(
            f"  ✗ {f.worker.name}  ({f.worker.provider} / {f.worker.model})"
        )
        # Indent the error message for readability.
        for err_line in f.error.splitlines() or [f.error]:
            lines.append(f"      {err_line}")
        lines.append("")
    lines.extend([
        "Fix by any of:",
        "  - export the required API key env var for the provider",
        "  - run 'silicon-join login' to save it to credentials.json",
        "  - remove these workers from your auto_host.toml",
        "",
    ])
    return "\n".join(lines)
