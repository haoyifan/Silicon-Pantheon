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


def orchestrate(
    cfg: SystemTestConfig,
    bundle_dir: Path,
    *,
    keep_remote_alive: bool = False,
    no_pull: bool = False,
) -> RunResult:
    """Entry point: synchronous wrapper that drives the asyncio run.

    ``keep_remote_alive`` and ``no_pull`` are remote-mode knobs; in
    local mode they're no-ops.
    """
    return asyncio.run(_run(
        cfg, bundle_dir,
        keep_remote_alive=keep_remote_alive, no_pull=no_pull,
    ))


async def _run(
    cfg: SystemTestConfig,
    bundle_dir: Path,
    *,
    keep_remote_alive: bool = False,
    no_pull: bool = False,
) -> RunResult:
    start = time.monotonic()
    bundle_dir.mkdir(parents=True, exist_ok=True)
    (bundle_dir / "server").mkdir(exist_ok=True)
    (bundle_dir / "clients").mkdir(exist_ok=True)

    _configure_orchestrator_logging(bundle_dir / "orchestrator.log")
    log.info("systemtest starting: bundle=%s", bundle_dir)

    if not cfg.client.is_local:
        raise NotImplementedError(
            "remote client mode is not supported; agents always spawn "
            "locally. Set client.ip = 127.0.0.1."
        )

    rng = random.Random(cfg.run.seed)
    incidents: list[str] = []

    # ---- bring up the server (local or remote) ----
    # ``server_handle`` is the opaque state needed by the matching
    # teardown + log-collection helpers; its shape differs between
    # modes but callers don't care.
    if cfg.server.is_local:
        server_handle = _bringup_local(cfg, bundle_dir)
    else:
        server_handle = _bringup_remote(cfg, bundle_dir, no_pull=no_pull)

    try:

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
        # Kill server + pull logs into the bundle. Local and remote
        # use different plumbing under the hood but the same shape:
        # first teardown (so no more writes land on disk), then
        # collect. Skipped entirely on --keep-remote-alive so you
        # can SSH in and poke at the server post-run.
        if cfg.server.is_local:
            _teardown_local(server_handle)
            _collect_local(server_handle, bundle_dir / "server")
        else:
            if keep_remote_alive:
                log.warning(
                    "--keep-remote-alive set; server still running at "
                    "%s (pid %s on %s). Remember to kill it yourself.",
                    cfg.server.url,
                    server_handle.pid, server_handle.ssh_dest,
                )
                _collect_remote(server_handle, bundle_dir / "server")
            else:
                _teardown_remote(server_handle)
                _collect_remote(server_handle, bundle_dir / "server")

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


# ────────────────────────── Server lifecycle ──────────────────────────
#
# Two implementations — local subprocess and remote-via-SSH — share a
# common ServerHandle shape. Each mode has three methods: bringup
# (start + health-check), teardown (stop), collect (pull logs into
# the bundle). The top-level _run orchestrator just calls the right
# trio based on cfg.server.is_local.


@dataclass
class LocalServer:
    """Handle for a silicon-serve subprocess we spawned on this host."""
    proc: "subprocess.Popen"
    home: Path           # the throwaway HOME (lives under bundle_dir/server/)
    stdout_log: Path     # the subprocess's stdout+stderr capture


@dataclass
class RemoteServer:
    """Handle for a silicon-serve process running on the VPS over SSH."""
    ssh_dest: str        # "<user>@<host>" — from cfg.server.ssh
    remote_home: str     # throwaway dir on the VPS (mktemp under /tmp)
    pid: str             # server PID as a string (read back via ssh cat)
    url: str             # public URL — from cfg.server.url


def _bringup_local(cfg: SystemTestConfig, bundle_dir: Path) -> LocalServer:
    """Spawn silicon-serve as a local subprocess + wait for health."""
    server_home = bundle_dir / "server" / "home"
    (server_home / ".silicon-pantheon" / "logs").mkdir(
        parents=True, exist_ok=True
    )
    (server_home / ".silicon-pantheon" / "replays").mkdir(exist_ok=True)

    if _port_in_use(cfg.server.port):
        raise RuntimeError(
            f"port {cfg.server.port} is already in use on this host; "
            f"kill whatever's listening or pick a different server.port"
        )

    stdout_log = bundle_dir / "server" / "silicon-serve.stdout.log"
    proc = _spawn_server(cfg, server_home, stdout_log)
    log.info("silicon-serve spawned pid=%d port=%d", proc.pid, cfg.server.port)
    _wait_healthy(cfg.server.port, HEALTH_TIMEOUT_S)
    log.info("silicon-serve healthy")
    return LocalServer(proc=proc, home=server_home, stdout_log=stdout_log)


def _teardown_local(h: LocalServer) -> None:
    _safe_terminate(h.proc)
    log.info("silicon-serve terminated rc=%s", h.proc.returncode)


def _collect_local(h: LocalServer, server_bundle: Path) -> None:
    """Copy silicon-serve log + replays + leaderboard into bundle."""
    _collect_server_logs(h.home, server_bundle)


def _bringup_remote(
    cfg: SystemTestConfig, bundle_dir: Path, *, no_pull: bool,
) -> RemoteServer:
    """Start silicon-serve on the configured VPS + wait for public URL.

    Assumes the operator has already:
      - set up passwordless SSH from this host to cfg.server.ssh
      - ensured cfg.server.url's hostname resolves to the VPS
      - configured a reverse proxy (e.g. Caddy) fronting cfg.server.port
      - cloned this repo at cfg.server.remote_repo on the VPS

    See docs/SYSTEM_TEST.md for the one-time VPS setup.
    """
    from silicon_pantheon.systemtest import ssh as _ssh

    ssh_dest = cfg.server.ssh
    repo = cfg.server.remote_repo
    port = cfg.server.port
    url = cfg.server.url

    # Extract the hostname from the URL for silicon-serve's
    # --allowed-host flag. FastMCP rejects requests whose Host
    # header isn't in this list, so the hostname must match what
    # the reverse proxy forwards.
    from urllib.parse import urlparse
    hostname = urlparse(url).hostname
    if not hostname:
        raise RuntimeError(
            f"could not parse hostname from server.url={url!r}"
        )

    # 1. SSH reachable?
    pre = _ssh.preflight(ssh_dest)
    if not pre.ok:
        raise RuntimeError(
            f"SSH preflight failed for {ssh_dest}: {pre.stderr.strip()}"
        )
    log.info("remote SSH reachable: %s", ssh_dest)

    # 2. Public URL resolves + TLS + reverse proxy are wired?
    # A 502 is the "healthy" answer here — nothing's on :port yet.
    # Anything else (DNS failure, TLS error, 4xx) is a config bug.
    import urllib.request
    try:
        with urllib.request.urlopen(url.rstrip("/"), timeout=10.0) as resp:
            _ = resp.status
    except urllib.error.HTTPError as e:
        if e.code not in (502, 404):
            log.warning(
                "public URL returned unexpected %d — continuing but "
                "this suggests a proxy misconfig", e.code,
            )
    except Exception as e:
        raise RuntimeError(
            f"public URL {url} not reachable — check DNS + reverse "
            f"proxy: {e}"
        )

    # 3. Confirm the target port is free on the VPS.
    port_check = _ssh.run(
        ssh_dest, f"ss -tln | grep -c ':{port}\\b' || true", timeout_s=10.0,
    )
    if port_check.ok and port_check.stdout.strip() != "0":
        raise RuntimeError(
            f"port {port} already has a listener on {ssh_dest}; "
            f"pick a different server.port or clean up the stale server"
        )

    # 4. Prepare the clone: fetch latest + sync venv.
    if not no_pull:
        log.info("remote: git pull + uv sync on %s", repo)
        sync = _ssh.run(
            ssh_dest,
            f"cd {_ssh.quote(repo)} && git pull --ff-only && "
            f"~/.local/bin/uv sync",
            timeout_s=300.0,
        )
        if not sync.ok:
            raise RuntimeError(
                f"remote git pull / uv sync failed:\n{sync.stderr[:2000]}"
            )

    # 5. Create a throwaway HOME on the VPS so server logs + replays
    # + leaderboard land somewhere we can scp back and then delete.
    mktemp = _ssh.run(
        ssh_dest, "mktemp -d /tmp/sst-XXXXXXXX", timeout_s=10.0, check=True,
    )
    remote_home = mktemp.stdout.strip()
    _ssh.run(
        ssh_dest,
        f"mkdir -p {_ssh.quote(remote_home)}/.silicon-pantheon/logs "
        f"{_ssh.quote(remote_home)}/.silicon-pantheon/replays",
        timeout_s=10.0, check=True,
    )
    log.info("remote throwaway HOME: %s:%s", ssh_dest, remote_home)

    # 6. Launch silicon-serve via nohup, capture PID.
    # Sensible defaults for a system-test run: full debug logging
    # (--log-level DEBUG, --log-debug-mcp-http), SILICON_DEBUG=1 so
    # invariants crash loudly and get captured. --diagnose-sse is
    # omitted: it spawns tcpdump, which needs CAP_NET_RAW that a
    # plain nohup launch won't inherit. Operators who want pcaps
    # can grant the capability to the remote silicon-serve binary
    # out-of-band (setcap) and we'll pick it up.
    launch_script = f"""\
set -e
cd {_ssh.quote(repo)}
export HOME={_ssh.quote(remote_home)}
export SILICON_DEBUG=1
nohup .venv/bin/silicon-serve \\
    --host 127.0.0.1 \\
    --port {port} \\
    --allowed-host {_ssh.quote(hostname)} \\
    --log-level DEBUG \\
    --log-debug-mcp-http \\
    > {_ssh.quote(remote_home)}/server.stdout.log 2>&1 &
echo $! > {_ssh.quote(remote_home)}/server.pid
disown
"""
    launch = _ssh.run(
        ssh_dest, "bash -s", stdin=launch_script, timeout_s=30.0,
    )
    if not launch.ok:
        raise RuntimeError(
            f"remote silicon-serve launch failed:\n{launch.stderr[:2000]}"
        )

    pid_read = _ssh.run(
        ssh_dest, f"cat {_ssh.quote(remote_home)}/server.pid",
        timeout_s=10.0, check=True,
    )
    pid = pid_read.stdout.strip()
    log.info("remote silicon-serve spawned pid=%s at %s", pid, url)

    handle = RemoteServer(
        ssh_dest=ssh_dest, remote_home=remote_home, pid=pid, url=url,
    )
    # atexit safety net: if the orchestrator dies mid-run, try to
    # kill the remote server so it doesn't linger on the VPS. Best-
    # effort; swallow every error since atexit can't do much.
    import atexit
    def _cleanup_on_exit() -> None:
        try:
            _ssh.run(
                ssh_dest,
                f"kill {pid} 2>/dev/null; sleep 1; "
                f"kill -9 {pid} 2>/dev/null; "
                f"rm -rf {_ssh.quote(remote_home)}",
                timeout_s=15.0,
            )
        except Exception:
            pass
    atexit.register(_cleanup_on_exit)

    # 7. Wait for silicon-serve's /health to return 200 by curling
    # 127.0.0.1:{port} on the VPS itself. We deliberately DON'T go
    # through the public URL here — that would force the operator
    # to expose /health via Caddy just for our probe, when in
    # production Caddy typically only routes /mcp/*. The pre-launch
    # HTTPS probe (step 2) already verified the Caddy + TLS layer;
    # this step verifies the Python process is answering.
    _wait_healthy_remote(ssh_dest, port, HEALTH_TIMEOUT_S)
    log.info("remote silicon-serve healthy at %s", url)
    return handle


def _teardown_remote(h: RemoteServer) -> None:
    from silicon_pantheon.systemtest import ssh as _ssh
    # SIGTERM → wait 2s → SIGKILL. Then rm the scratch dir after we
    # pull logs (caller invokes _collect_remote first).
    log.info("remote: stopping silicon-serve pid=%s on %s", h.pid, h.ssh_dest)
    _ssh.run(
        h.ssh_dest,
        f"kill {h.pid}; sleep 2; kill -9 {h.pid} 2>/dev/null; true",
        timeout_s=15.0,
    )


def _collect_remote(h: RemoteServer, server_bundle: Path) -> None:
    """SCP the remote HOME's ~/.silicon-pantheon data into the bundle."""
    from silicon_pantheon.systemtest import ssh as _ssh
    server_bundle.mkdir(parents=True, exist_ok=True)
    remote_sp = f"{h.remote_home}/.silicon-pantheon"
    targets = [
        (f"{remote_sp}/logs", server_bundle / "logs", True),
        (f"{remote_sp}/replays", server_bundle / "replays", True),
        (f"{remote_sp}/leaderboard.db", server_bundle / "leaderboard.db", False),
        (f"{h.remote_home}/server.stdout.log",
         server_bundle / "silicon-serve.stdout.log", False),
        # pcaps if --diagnose-sse was on; fine if missing
        (f"{remote_sp}/sse-diag", server_bundle / "sse-diag", True),
    ]
    for remote, local, recursive in targets:
        r = _ssh.scp_pull(
            h.ssh_dest, remote, local,
            recursive=recursive, timeout_s=300.0,
        )
        if r.ok:
            log.info("pulled %s -> %s", remote, local)
        else:
            # Missing dir / file is OK (replays/ may be empty, sse-diag
            # only exists with --diagnose-sse). Log but don't raise.
            log.info(
                "skipped %s (rc=%d): %s", remote, r.returncode,
                r.stderr.strip()[:200] if r.stderr else "",
            )
    # Finally remove the scratch dir so the VPS doesn't accumulate
    # pcaps + logs on every run.
    _ssh.run(
        h.ssh_dest, f"rm -rf {_ssh.quote(h.remote_home)}", timeout_s=30.0,
    )


def _wait_healthy_remote(
    ssh_dest: str, port: int, timeout_s: float,
) -> None:
    """Poll silicon-serve's /health via curl on the VPS itself.

    Goes through loopback on the remote, so it's independent of
    Caddy's routing rules — we only need the Python process to be
    answering, not for /health to be publicly exposed.
    """
    from silicon_pantheon.systemtest import ssh as _ssh
    deadline = time.monotonic() + timeout_s
    last_stderr = ""
    while time.monotonic() < deadline:
        r = _ssh.run(
            ssh_dest,
            f"curl -sf -o /dev/null -w '%{{http_code}}' "
            f"http://127.0.0.1:{port}/health",
            timeout_s=5.0,
        )
        if r.ok and r.stdout.strip() == "200":
            return
        last_stderr = r.stderr.strip() or r.stdout.strip()
        time.sleep(0.5)
    raise RuntimeError(
        f"silicon-serve on {ssh_dest}:{port} did not return 200 at "
        f"/health in {timeout_s}s: {last_stderr}"
    )


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

    # Local mode: agents talk to loopback directly.
    # Remote mode: agents go through the public URL (Caddy + TLS +
    # reverse proxy), which is the whole point of remote mode.
    server_url = (
        cfg.server.url
        if not cfg.server.is_local
        else f"http://127.0.0.1:{cfg.server.port}/mcp/"
    )
    parts = [
        f'[server]',
        f'url = "{server_url}"',
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
