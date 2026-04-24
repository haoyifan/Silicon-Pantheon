"""End-to-end smoke for silicon-system-test in local mode.

Spins up the orchestrator with N=1 on 01_tiny_skirmish (the scenario
that reliably terminates quickly under random play) and verifies:
  - server subprocess starts and becomes healthy
  - host + joiner silicon-host subprocesses spawn and complete
  - bundle directory contains the expected files
  - manifest records the 2 agents with returncode=0
  - no orchestrator-detected incidents

The test is synchronous (uses orchestrate() which wraps asyncio.run).
Budget: ~30 s wall clock locally.
"""

from __future__ import annotations

import socket
from pathlib import Path

import pytest


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_systemtest_local_n1(tmp_path: Path) -> None:
    from silicon_pantheon.systemtest.config import (
        ClientSpec, Defaults, RandomizeSpec, RunSpec, ServerSpec,
        SystemTestConfig,
    )
    from silicon_pantheon.systemtest.orchestrator import orchestrate

    port = _free_port()
    cfg = SystemTestConfig(
        server=ServerSpec(ip="127.0.0.1", port=port, ssh_user="x"),
        client=ClientSpec(ip="127.0.0.1", ssh_user="x"),
        run=RunSpec(num_matches=1, timeout_s=120, seed=42),
        defaults=Defaults(
            mode="random",
            provider="xai",
            model="grok-3-mini",
            locale="en",
            turn_time_limit_s=60,
        ),
        randomize=RandomizeSpec(
            scenarios=["01_tiny_skirmish"],
            fog_modes=["none"],
            team_assignments=["fixed"],
            locales=["en"],
        ),
        agent_overrides=[],
    )

    result = orchestrate(cfg, tmp_path / "bundle")

    # ---- layout checks ----
    bd = result.bundle_dir
    assert (bd / "orchestrator.log").is_file(), "orchestrator.log missing"
    assert (bd / "run-manifest.json").is_file(), "manifest missing"
    assert (bd / "INCIDENTS.md").is_file(), "INCIDENTS.md missing"
    assert (bd / "server").is_dir(), "server dir missing"
    assert (bd / "clients").is_dir(), "clients dir missing"

    # 2 agents × (toml + log + stdout.log) = 6 files minimum
    client_files = list((bd / "clients").iterdir())
    assert len(client_files) >= 6, (
        f"expected >= 6 files in clients/, got {len(client_files)}: "
        f"{[f.name for f in client_files]}"
    )

    # ---- manifest structure ----
    import json
    manifest = json.loads((bd / "run-manifest.json").read_text())
    assert manifest["config"]["run"]["num_matches"] == 1
    assert len(manifest["agents"]) == 2, manifest["agents"]
    roles = sorted(a["role"] for a in manifest["agents"])
    assert roles == ["host", "joiner"], roles

    # ---- outcomes ----
    assert result.n_agents == 2
    assert not result.timed_out, "run timed out; random play didn't converge"
    # Both agents should have exited 0. If not, surface which ones.
    for a in manifest["agents"]:
        assert a["returncode"] == 0, (
            f"agent {a['name']} exited rc={a['returncode']}; "
            f"stdout tail:\n{Path(a['stdout_path']).read_text()[-500:] if Path(a['stdout_path']).is_file() else '(no stdout file)'}"
        )
    assert result.n_crashed == 0

    # ---- replays land in the bundle ----
    # Regression for the 2026-04-23 bundle bug: silicon-serve writes
    # per-match replays to a relative ``runs-server/`` path. Without
    # the cwd pin in _spawn_server, those files landed in the
    # orchestrator's own cwd (the repo root) and the bundle's
    # server/replays/ stayed empty, even after a match finished and
    # was recorded to the leaderboard. With one match completed
    # cleanly above we should see exactly one replay file here.
    replays_dir = bd / "server" / "replays"
    assert replays_dir.is_dir(), "server/replays/ missing from bundle"
    replay_files = sorted(replays_dir.glob("*.jsonl"))
    assert len(replay_files) >= 1, (
        f"expected >= 1 replay.jsonl in server/replays/, got "
        f"{[f.name for f in replay_files]}"
    )
    # Sanity: each replay has at least one event line.
    for r in replay_files:
        body = r.read_text(encoding="utf-8").strip()
        assert body, f"replay {r.name} is empty"
        # Each line is one JSON event; first line should parse.
        first = body.splitlines()[0]
        json.loads(first)
