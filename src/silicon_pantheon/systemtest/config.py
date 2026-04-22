"""TOML schema for system-test runs.

Example::

    [server]
    ip = "127.0.0.1"    # or a remote IP; 127.0.0.1 = local subprocess
    port = 8090
    ssh_user = "silicon"  # ignored when ip is 127.0.0.1/localhost

    [client]
    ip = "127.0.0.1"
    ssh_user = "silicon"

    [run]
    num_matches = 10
    timeout_s = 14400    # 4 hours
    seed = 42            # optional

    [defaults]
    mode = "random"      # "random" | "llm"
    provider = "xai"     # only used when mode = "llm"
    model = "grok-3-mini"
    locale = "en"

    [randomize]
    scenarios = "all"     # or explicit list
    fog_modes = ["none", "classic"]
    team_assignments = ["fixed"]
    locales = ["en"]
    max_turns_range = [8, 20]

    [[agent]]           # optional per-slot override; slot = 0..2N-1
    slot = 0
    mode = "llm"
    model = "claude-haiku-4-5"
    provider = "anthropic"
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
class ServerSpec:
    ip: str = "127.0.0.1"
    port: int = 8090
    ssh_user: str = "silicon"

    @property
    def is_local(self) -> bool:
        return self.ip in ("127.0.0.1", "localhost", "::1")


@dataclass
class ClientSpec:
    ip: str = "127.0.0.1"
    ssh_user: str = "silicon"

    @property
    def is_local(self) -> bool:
        return self.ip in ("127.0.0.1", "localhost", "::1")


@dataclass
class RunSpec:
    num_matches: int = 10
    timeout_s: int = 14400  # 4h
    seed: int | None = None


@dataclass
class Defaults:
    mode: str = "random"
    provider: str = "xai"
    model: str = "grok-3-mini"
    locale: str = "en"
    turn_time_limit_s: int = 1800


@dataclass
class RandomizeSpec:
    scenarios: str | list[str] = "all"
    fog_modes: list[str] = field(
        default_factory=lambda: ["none", "classic", "line_of_sight"]
    )
    team_assignments: list[str] = field(
        default_factory=lambda: ["fixed"]
    )
    locales: list[str] = field(default_factory=lambda: ["en"])
    max_turns_range: list[int] = field(default_factory=lambda: [8, 20])


@dataclass
class AgentOverride:
    """Per-slot override; merges over [defaults]."""
    slot: int
    mode: str | None = None
    provider: str | None = None
    model: str | None = None


@dataclass
class SystemTestConfig:
    server: ServerSpec
    client: ClientSpec
    run: RunSpec
    defaults: Defaults
    randomize: RandomizeSpec
    agent_overrides: list[AgentOverride]


def load_config(path: Path) -> SystemTestConfig:
    """Parse a TOML file into a SystemTestConfig. Every section is
    optional and falls back to dataclass defaults."""
    with open(path, "rb") as f:
        raw = tomllib.load(f)

    srv = raw.get("server", {}) or {}
    server = ServerSpec(
        ip=str(srv.get("ip", "127.0.0.1")),
        port=int(srv.get("port", 8090)),
        ssh_user=str(srv.get("ssh_user", "silicon")),
    )

    cli = raw.get("client", {}) or {}
    client = ClientSpec(
        ip=str(cli.get("ip", "127.0.0.1")),
        ssh_user=str(cli.get("ssh_user", "silicon")),
    )

    rn = raw.get("run", {}) or {}
    run = RunSpec(
        num_matches=int(rn.get("num_matches", 10)),
        timeout_s=int(rn.get("timeout_s", 14400)),
        seed=rn.get("seed"),
    )

    d = raw.get("defaults", {}) or {}
    defaults = Defaults(
        mode=str(d.get("mode", "random")),
        provider=str(d.get("provider", "xai")),
        model=str(d.get("model", "grok-3-mini")),
        locale=str(d.get("locale", "en")),
        turn_time_limit_s=int(d.get("turn_time_limit_s", 1800)),
    )

    rz = raw.get("randomize", {}) or {}
    randomize = RandomizeSpec(
        scenarios=rz.get("scenarios", "all"),
        fog_modes=list(
            rz.get("fog_modes", ["none", "classic", "line_of_sight"])
        ),
        team_assignments=list(rz.get("team_assignments", ["fixed"])),
        locales=list(rz.get("locales", ["en"])),
        max_turns_range=list(rz.get("max_turns_range", [8, 20])),
    )

    overrides: list[AgentOverride] = []
    for a in raw.get("agent", []) or []:
        overrides.append(
            AgentOverride(
                slot=int(a.get("slot", -1)),
                mode=a.get("mode"),
                provider=a.get("provider"),
                model=a.get("model"),
            )
        )

    if run.num_matches < 1:
        raise ValueError(f"run.num_matches must be >= 1, got {run.num_matches}")

    return SystemTestConfig(
        server=server,
        client=client,
        run=run,
        defaults=defaults,
        randomize=randomize,
        agent_overrides=overrides,
    )


def apply_overrides(
    slot: int, defaults: Defaults, overrides: list[AgentOverride]
) -> dict[str, Any]:
    """Merge defaults with any per-slot override into a flat dict of
    fields that vary per agent."""
    cfg: dict[str, Any] = {
        "mode": defaults.mode,
        "provider": defaults.provider,
        "model": defaults.model,
    }
    for o in overrides:
        if o.slot == slot:
            if o.mode is not None:
                cfg["mode"] = o.mode
            if o.provider is not None:
                cfg["provider"] = o.provider
            if o.model is not None:
                cfg["model"] = o.model
    return cfg
