"""Local-mode orchestrator for silicon-system-test.

Spins up a throwaway silicon-serve (fresh HOME = bundle dir, fresh
port), generates per-agent TOMLs for 2N silicon-host subprocesses
(N hosts + N joiners), waits for them to finish or the global
timeout to fire, then collects logs + replays into the bundle.

Scope of THIS file: localhost-only. Both server and clients are
subprocesses of the orchestrator. Remote SSH support is deliberately
deferred — the localhost case covers 99% of "can I run this to
surface bugs before a release?" and keeps the implementation
tractable. See ~/dev/system-test-plan.md for the remote design.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
import logging
import os
import random
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from silicon_pantheon.systemtest.config import (
    Defaults,
    RandomizeSpec,
    SystemTestConfig,
    apply_overrides,
)

log = logging.getLogger("silicon.systemtest")

POLL_INTERVAL_S = 2.0
HEALTH_TIMEOUT_S = 30.0


@dataclass
class AgentRecord:
    """One silicon-host subprocess we spawned."""
    slot: int
    role: str  # "host" | "joiner"
    name: str
    scenario: str | None
    mode: str
    model: str
    provider: str
    pid: int | None = None
    returncode: int | None = None
    toml_path: str = ""
    log_path: str = ""
    stdout_path: str = ""


@dataclass
class RunResult:
    bundle_dir: Path
    manifest_path: Path
    incidents_path: Path
    n_agents: int
    n_crashed: int
    wall_clock_s: float
    timed_out: bool


def orchestrate(cfg: SystemTestConfig, bundle_dir: Path) -> RunResult:
    """Entry point: synchronous wrapper that drives the asyncio run."""
    return asyncio.run(_run(cfg, bundle_dir))


async def _run(cfg: SystemTestConfig, bundle_dir: Path) -> RunResult:
    start = time.monotonic()
    bundle_dir.mkdir(parents=True, exist_ok=True)
    (bundle_dir / "server").mkdir(exist_ok=True)
    (bundle_dir / "clients").mkdir(exist_ok=True)

    _configure_orchestrator_logging(bundle_dir / "orchestrator.log")
    log.info("systemtest starting: bundle=%s", bundle_dir)

    if not cfg.server.is_local or not cfg.client.is_local:
        raise NotImplementedError(
            "remote SSH mode is not yet implemented; use server.ip = "
            "127.0.0.1 and client.ip = 127.0.0.1 for now"
        )

    rng = random.Random(cfg.run.seed)
    incidents: list[str] = []

    # ---- throwaway HOME for silicon-serve ----
    # Overriding HOME puts ~/.silicon-pantheon/{logs,replays,leaderboard.db}
    # inside the bundle dir, isolated from the real user's data.
    server_home = bundle_dir / "server" / "home"
    (server_home / ".silicon-pantheon" / "logs").mkdir(
        parents=True, exist_ok=True
    )
    (server_home / ".silicon-pantheon" / "replays").mkdir(exist_ok=True)

    # Preflight: nothing on server port.
    if _port_in_use(cfg.server.port):
        raise RuntimeError(
            f"port {cfg.server.port} is already in use on this host; "
            f"kill whatever's listening or pick a different server.port"
        )

    # ---- spawn silicon-serve ----
    server_log = bundle_dir / "server" / "silicon-serve.stdout.log"
    server_proc = _spawn_server(cfg, server_home, server_log)
    log.info("silicon-serve spawned pid=%d port=%d", server_proc.pid, cfg.server.port)

    try:
        _wait_healthy(cfg.server.port, HEALTH_TIMEOUT_S)
        log.info("silicon-serve healthy")

        # ---- generate agent TOMLs ----
        scenarios = _resolve_scenarios(cfg.randomize)
        agents = _plan_agents(cfg, rng, scenarios)

        # Drop each agent's TOML into the bundle for reproducibility
        # and as the config path we hand to silicon-host.
        for a in agents:
            toml_path = bundle_dir / "clients" / f"{a.name}.toml"
            toml_path.write_text(
                _render_agent_toml(a, cfg, rng, scenarios),
                encoding="utf-8",
            )
            a.toml_path = str(toml_path)
            a.log_path = str(bundle_dir / "clients" / f"{a.name}.log")
            a.stdout_path = str(bundle_dir / "clients" / f"{a.name}.stdout.log")

        # ---- stagger: spawn hosts, then joiners ----
        host_procs = _spawn_agents(
            [a for a in agents if a.role == "host"], cfg,
        )
        log.info("hosts spawned (N=%d); giving them 5s to create rooms",
                 len(host_procs))
        await asyncio.sleep(5.0)

        joiner_procs = _spawn_agents(
            [a for a in agents if a.role == "joiner"], cfg,
        )
        log.info("joiners spawned (N=%d)", len(joiner_procs))
        all_procs = {**host_procs, **joiner_procs}

        # ---- poll for completion ----
        timed_out = False
        deadline = start + cfg.run.timeout_s
        while all_procs:
            now = time.monotonic()
            if now >= deadline:
                log.warning(
                    "global timeout %.0fs reached with %d agents still "
                    "running — killing survivors",
                    cfg.run.timeout_s, len(all_procs),
                )
                incidents.append(
                    f"TIMEOUT: {len(all_procs)} agents still running after "
                    f"{cfg.run.timeout_s}s; forced termination"
                )
                for proc in all_procs.values():
                    _safe_terminate(proc)
                timed_out = True
                break

            await asyncio.sleep(POLL_INTERVAL_S)
            finished: list[str] = []
            for name, proc in all_procs.items():
                rc = proc.poll()
                if rc is None:
                    continue
                finished.append(name)
                agent = next(a for a in agents if a.name == name)
                agent.returncode = rc
                if rc != 0:
                    tail = _tail_file(Path(agent.stdout_path), 50)
                    msg = (
                        f"CRASH: agent {name} (slot {agent.slot} / "
                        f"{agent.role}) exited rc={rc}"
                    )
                    log.error("%s\n--- last 50 lines of stdout ---\n%s",
                              msg, tail)
                    incidents.append(msg)
                else:
                    log.info(
                        "agent %s (slot %d / %s) finished cleanly",
                        name, agent.slot, agent.role,
                    )
            for name in finished:
                all_procs.pop(name, None)

    finally:
        # Kill anything still running (server + stragglers).
        _safe_terminate(server_proc)
        log.info("silicon-serve terminated rc=%s", server_proc.returncode)
        # Copy server logs into the bundle (already inside fake HOME).

    # ---- final collection ----
    _collect_server_logs(server_home, bundle_dir / "server")

    wall_clock = time.monotonic() - start
    n_crashed = sum(1 for a in agents if a.returncode not in (0, None))

    # manifest + incidents
    from silicon_pantheon.systemtest.bundle import write_bundle_outputs
    manifest_path, incidents_path = write_bundle_outputs(
        bundle_dir=bundle_dir,
        cfg=cfg,
        agents=agents,
        incidents=incidents,
        wall_clock_s=wall_clock,
        timed_out=timed_out,
        git_sha=_git_sha(),
    )

    log.info(
        "systemtest done: wall_clock=%.1fs agents=%d crashed=%d timed_out=%s",
        wall_clock, len(agents), n_crashed, timed_out,
    )

    return RunResult(
        bundle_dir=bundle_dir,
        manifest_path=manifest_path,
        incidents_path=incidents_path,
        n_agents=len(agents),
        n_crashed=n_crashed,
        wall_clock_s=wall_clock,
        timed_out=timed_out,
    )


# ────────────────────────────── helpers ──────────────────────────────


def _configure_orchestrator_logging(path: Path) -> None:
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    fh = logging.FileHandler(path, mode="w", encoding="utf-8")
    fh.setFormatter(fmt)
    stream = logging.StreamHandler()
    stream.setFormatter(fmt)
    root = logging.getLogger("silicon.systemtest")
    root.setLevel(logging.INFO)
    root.addHandler(fh)
    root.addHandler(stream)
    root.propagate = False


def _port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
            return False
        except OSError:
            return True


def _spawn_server(
    cfg: SystemTestConfig, server_home: Path, stdout_log: Path
) -> subprocess.Popen:
    """Spawn silicon-serve with HOME overridden to the bundle dir."""
    env = os.environ.copy()
    env["HOME"] = str(server_home)
    # Don't inherit our own SILICON_DEBUG — server in production mode.
    env.pop("SILICON_DEBUG", None)
    cmd = [
        sys.executable, "-m", "silicon_pantheon.server.main_http",
        "--host", "127.0.0.1",
        "--port", str(cfg.server.port),
        "--log-level", "INFO",
    ]
    fh = open(stdout_log, "ab", buffering=0)
    return subprocess.Popen(
        cmd,
        env=env,
        stdout=fh, stderr=fh,
        start_new_session=True,
    )


def _wait_healthy(port: int, timeout_s: float) -> None:
    deadline = time.monotonic() + timeout_s
    url = f"http://127.0.0.1:{port}/health"
    last_exc: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.0) as resp:
                if resp.status == 200:
                    return
        except Exception as e:
            last_exc = e
        time.sleep(0.25)
    raise RuntimeError(
        f"silicon-serve did not become healthy in {timeout_s}s: "
        f"{last_exc}"
    )


def _resolve_scenarios(rz: RandomizeSpec) -> list[str]:
    if isinstance(rz.scenarios, list) and rz.scenarios:
        return list(rz.scenarios)
    project_root = Path(__file__).resolve().parents[3]
    games_dir = project_root / "games"
    if not games_dir.is_dir():
        return ["01_tiny_skirmish"]
    return sorted(
        d.name for d in games_dir.iterdir()
        if d.is_dir()
        and not d.name.startswith("_")
        and (d / "config.yaml").exists()
    )


def _plan_agents(
    cfg: SystemTestConfig, rng: random.Random, scenarios: list[str]
) -> list[AgentRecord]:
    """Generate 2N AgentRecords: N hosts + N joiners."""
    N = cfg.run.num_matches
    agents: list[AgentRecord] = []
    for i in range(N):
        scenario = rng.choice(scenarios)
        # Host slot = i, joiner slot = N + i. Keeps overrides stable.
        host_slot = i
        joiner_slot = N + i
        host_cfg = apply_overrides(host_slot, cfg.defaults, cfg.agent_overrides)
        joiner_cfg = apply_overrides(joiner_slot, cfg.defaults, cfg.agent_overrides)
        agents.append(AgentRecord(
            slot=host_slot, role="host",
            name=f"match{i:02d}-host",
            scenario=scenario,
            mode=host_cfg["mode"],
            model=host_cfg["model"], provider=host_cfg["provider"],
        ))
        agents.append(AgentRecord(
            slot=joiner_slot, role="joiner",
            name=f"match{i:02d}-joiner",
            scenario=None,  # joiner picks whatever room it joined
            mode=joiner_cfg["mode"],
            model=joiner_cfg["model"], provider=joiner_cfg["provider"],
        ))
    return agents


def _render_agent_toml(
    a: AgentRecord, cfg: SystemTestConfig, rng: random.Random,
    scenarios: list[str],
) -> str:
    """Generate the per-agent silicon-host TOML. One [[worker]] block."""
    fog = rng.choice(cfg.randomize.fog_modes)
    team_assignment = rng.choice(cfg.randomize.team_assignments)
    locale = rng.choice(cfg.randomize.locales)

    parts = [
        f'[server]',
        f'url = "http://127.0.0.1:{cfg.server.port}/mcp/"',
        '',
        '[log]',
        f'file = "{a.log_path}"',
        '',
        '[[worker]]',
        f'name = "{a.name}"',
        f'mode = "{a.mode}"',
        f'provider = "{a.provider}"',
        f'model = "{a.model}"',
        f'kind = "ai"',
        f'one_shot = true',
        f'turn_time_limit_s = {cfg.defaults.turn_time_limit_s}',
        f'locale = "{locale}"',
        f'save_lessons = false',
    ]
    if a.mode == "random" and cfg.run.seed is not None:
        # Per-agent seed derived from run seed + slot, so reruns with
        # the same run seed produce the same play trajectory.
        parts.append(f"seed = {cfg.run.seed * 1000 + a.slot}")
    if a.role == "host":
        parts.append(f'scenarios = ["{a.scenario}"]')
        parts.append(f'fog_of_war = "{fog}"')
        parts.append(f'team_assignment = "{team_assignment}"')
    else:
        parts.append("join_only = true")
    parts.append('')
    return "\n".join(parts)


def _spawn_agents(
    agents: list[AgentRecord], cfg: SystemTestConfig,
) -> dict[str, subprocess.Popen]:
    """Spawn one silicon-host subprocess per agent."""
    client_home_base = Path(cfg.client.ssh_user)  # unused in local mode
    procs: dict[str, subprocess.Popen] = {}
    for a in agents:
        env = os.environ.copy()
        # Each agent gets its OWN fake HOME so its ~/.silicon-pantheon
        # logs land in a predictable, per-agent path. We still share
        # credentials.json from the user's real HOME so LLM-mode
        # workers work. The orchestrator doesn't copy creds; the real
        # HOME's credentials file is referenced via absolute path.
        env["SILICON_DEBUG"] = "1"  # client-side invariant crash-loud
        cmd = [
            sys.executable, "-m", "silicon_pantheon.host.runner",
            a.toml_path,
            "--log", a.log_path,
            "--debug",
        ]
        fh = open(a.stdout_path, "ab", buffering=0)
        proc = subprocess.Popen(
            cmd,
            env=env,
            stdout=fh, stderr=fh,
            start_new_session=True,
        )
        a.pid = proc.pid
        procs[a.name] = proc
        log.info("spawned agent %s pid=%d role=%s", a.name, proc.pid, a.role)
    return procs


def _safe_terminate(proc: subprocess.Popen) -> None:
    """SIGTERM → wait → SIGKILL pattern."""
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
    except Exception:
        pass
    try:
        proc.wait(timeout=5.0)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        proc.kill()
        proc.wait(timeout=2.0)
    except Exception:
        pass


def _collect_server_logs(server_home: Path, server_bundle: Path) -> None:
    """Copy silicon-serve log + replays + leaderboard.db into bundle."""
    src_logs = server_home / ".silicon-pantheon" / "logs"
    if src_logs.is_dir():
        for f in src_logs.iterdir():
            if f.is_file():
                shutil.copy2(f, server_bundle / f.name)
    src_replays = server_home / ".silicon-pantheon" / "replays"
    if src_replays.is_dir():
        dest_replays = server_bundle / "replays"
        dest_replays.mkdir(exist_ok=True)
        for f in src_replays.iterdir():
            if f.is_file():
                shutil.copy2(f, dest_replays / f.name)
    db = server_home / ".silicon-pantheon" / "leaderboard.db"
    if db.is_file():
        shutil.copy2(db, server_bundle / "leaderboard.db")


def _tail_file(path: Path, lines: int) -> str:
    if not path.is_file():
        return "(no stdout captured)"
    try:
        data = path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return f"(could not read {path}: {e})"
    tail = data.splitlines()[-lines:]
    return "\n".join(tail)


def _git_sha() -> str:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=Path(__file__).resolve().parents[3],
            text=True,
        ).strip()
        return out
    except Exception:
        return "unknown"
