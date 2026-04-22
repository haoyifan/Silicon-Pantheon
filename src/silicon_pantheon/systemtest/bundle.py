"""Write run-manifest.json and INCIDENTS.md into the bundle.

Split out of orchestrator.py so the manifest schema is documented in
one place and easy to change without touching the run loop.
"""

from __future__ import annotations

import datetime as _dt
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any


def write_bundle_outputs(
    bundle_dir: Path,
    cfg: Any,       # SystemTestConfig (avoid circular import)
    agents: list,   # list[AgentRecord]
    incidents: list[str],
    wall_clock_s: float,
    timed_out: bool,
    git_sha: str,
) -> tuple[Path, Path]:
    """Write manifest.json + INCIDENTS.md, return their paths."""
    manifest_path = bundle_dir / "run-manifest.json"
    incidents_path = bundle_dir / "INCIDENTS.md"

    # ---- manifest ----
    manifest = {
        "started_at": _dt.datetime.fromtimestamp(
            bundle_dir.stat().st_ctime
        ).isoformat(timespec="seconds"),
        "wall_clock_s": round(wall_clock_s, 1),
        "timed_out": timed_out,
        "git_sha": git_sha,
        "config": {
            "server": asdict(cfg.server),
            "client": asdict(cfg.client),
            "run": asdict(cfg.run),
            "defaults": asdict(cfg.defaults),
            "randomize": asdict(cfg.randomize),
        },
        "agents": [
            {
                "slot": a.slot,
                "role": a.role,
                "name": a.name,
                "scenario": a.scenario,
                "mode": a.mode,
                "model": a.model,
                "provider": a.provider,
                "pid": a.pid,
                "returncode": a.returncode,
                "toml_path": a.toml_path,
                "log_path": a.log_path,
                "stdout_path": a.stdout_path,
            }
            for a in agents
        ],
        "summary": {
            "n_agents": len(agents),
            "n_clean_exit": sum(
                1 for a in agents if a.returncode == 0
            ),
            "n_crashed": sum(
                1 for a in agents
                if a.returncode is not None and a.returncode != 0
            ),
            "n_killed_by_timeout": sum(
                1 for a in agents if a.returncode is None
            ),
        },
    }
    manifest_path.write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )

    # ---- INCIDENTS.md ----
    lines = [
        "# System-test incidents",
        "",
        f"Run duration: **{wall_clock_s:.1f}s**"
        + (" (**timed out**)" if timed_out else ""),
        f"Agents: {manifest['summary']['n_agents']}"
        f" (clean={manifest['summary']['n_clean_exit']},"
        f" crashed={manifest['summary']['n_crashed']},"
        f" killed_by_timeout={manifest['summary']['n_killed_by_timeout']})",
        "",
    ]
    if not incidents:
        lines.append("_No orchestrator-detected incidents._")
        lines.append("")
        lines.append(
            "Deeper analysis (fog leaks, slow tool calls, server "
            "warnings) is not attempted here — run the "
            "`/review-system-test` skill on this bundle."
        )
    else:
        lines.append(f"## Orchestrator-detected incidents ({len(incidents)})")
        lines.append("")
        for inc in incidents:
            lines.append(f"- {inc}")
        lines.append("")
        lines.append(
            "For deeper analysis (fog leaks, slow tool calls, "
            "server warnings, per-match outcomes), run the "
            "`/review-system-test` skill on this bundle."
        )
    incidents_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    return manifest_path, incidents_path
