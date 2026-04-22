"""TOML configuration schema for the auto-host.

Example config::

    [server]
    url = "https://game.siliconpantheon.com/mcp/"

    [defaults]
    provider = "xai"
    model = "grok-3-mini"
    scenarios = ["random"]
    fog_of_war = "none"
    team_assignment = "fixed"
    host_team = "blue"
    turn_time_limit_s = 1800
    save_lessons = true
    locale = "en"

    [[worker]]
    name = "Arena-1"

    [[worker]]
    name = "Arena-2"
    model = "claude-haiku-4-5"
    provider = "anthropic"
    strategy = "strategies/aggressive.md"
    lessons = ["lessons/06_agincourt/*.md"]
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:
    import tomli as tomllib  # type: ignore[no-redef]


@dataclass
class WorkerConfig:
    """Resolved configuration for a single bot worker."""
    name: str
    provider: str
    model: str
    kind: str = "ai"
    scenarios: list[str] = field(default_factory=lambda: ["random"])
    fog_of_war: str = "none"
    team_assignment: str = "fixed"
    host_team: str = "blue"
    turn_time_limit_s: int = 1800
    save_lessons: bool = True
    strategy: str | None = None  # path to a .md file
    lessons: list[str] = field(default_factory=list)  # glob patterns
    locale: str = "en"
    # One-shot mode: return from run_forever after one completed match
    # instead of looping. Used by the system-test framework to run a
    # bounded workload. Default False preserves the long-running
    # daemon semantics for auto-host.
    one_shot: bool = False
    # Agent mode. "llm" uses the NetworkedAgent + provider adapter
    # (default, unchanged behaviour). "random" uses RandomNetworkAgent
    # — picks uniformly random legal actions via the MCP transport,
    # no LLM provider, zero per-move cost. Used by the system-test
    # framework to exercise server+transport end-to-end without
    # burning LLM credits.
    mode: str = "llm"
    # Optional seed for the random mode's RNG. Makes a run reproducible.
    # Ignored when mode="llm".
    seed: int | None = None
    # Join-only workers DON'T create a room; they list existing rooms
    # and join one in "waiting_for_players" status. Used by the
    # system-test framework's joiner population so the workload is
    # evenly split (N hosts + N joiners = N matches). Default False
    # means behave as a host (create a room). Hosts and joiners never
    # swap mid-run; set at config parse time.
    join_only: bool = False


@dataclass
class HostConfig:
    """Top-level configuration."""
    server_url: str
    workers: list[WorkerConfig]
    log_file: str = "auto_host.log"


def load_config(path: Path) -> HostConfig:
    """Parse a TOML config file into a HostConfig."""
    with open(path, "rb") as f:
        raw = tomllib.load(f)

    server = raw.get("server", {})
    server_url = server.get("url", "https://game.siliconpantheon.com/mcp/")
    log_file = raw.get("log", {}).get("file", "auto_host.log")

    defaults = raw.get("defaults", {})
    workers: list[WorkerConfig] = []

    for i, w in enumerate(raw.get("worker", []), start=1):
        # Merge: worker overrides defaults.
        merged = {**defaults, **w}
        workers.append(WorkerConfig(
            name=merged.get("name", f"Bot-{i}"),
            provider=merged.get("provider", "xai"),
            model=merged.get("model", "grok-3-mini"),
            kind=merged.get("kind", "ai"),
            scenarios=merged.get("scenarios", ["random"]),
            fog_of_war=merged.get("fog_of_war", "none"),
            team_assignment=merged.get("team_assignment", "fixed"),
            host_team=merged.get("host_team", "blue"),
            turn_time_limit_s=int(merged.get("turn_time_limit_s", 1800)),
            save_lessons=bool(merged.get("save_lessons", True)),
            strategy=merged.get("strategy"),
            lessons=merged.get("lessons", []),
            locale=merged.get("locale", "en"),
            one_shot=bool(merged.get("one_shot", False)),
            mode=merged.get("mode", "llm"),
            seed=merged.get("seed"),
            join_only=bool(merged.get("join_only", False)),
        ))

    if not workers:
        raise ValueError("config must define at least one [[worker]]")

    return HostConfig(
        server_url=server_url,
        workers=workers,
        log_file=log_file,
    )
