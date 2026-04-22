"""silicon-system-test CLI entry."""

from __future__ import annotations

import argparse
import datetime as _dt
import sys
from pathlib import Path

from silicon_pantheon.systemtest.config import load_config


def main() -> int:
    p = argparse.ArgumentParser(
        description=(
            "silicon-system-test — unattended end-to-end fuzz testing. "
            "Spins up a throwaway silicon-serve, runs N concurrent "
            "matches with random-action agents, bundles logs + replays."
        ),
    )
    p.add_argument(
        "--config",
        type=Path,
        required=True,
        help=(
            "Path to the TOML config file. See "
            "src/silicon_pantheon/systemtest/config.py for schema + "
            "example."
        ),
    )
    p.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        help=(
            "Where to write the bundle directory. Default: "
            "~/silicon-system-test-results/<timestamp>/"
        ),
    )
    p.add_argument(
        "-N", "--num-matches",
        type=int,
        default=None,
        help=(
            "Override run.num_matches from the config. "
            "Handy for quick smoke runs without editing the TOML."
        ),
    )
    p.add_argument(
        "--seed",
        type=int,
        default=None,
        help="Override run.seed from the config.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Parse config + compute bundle path + print plan, do not "
            "spawn anything. Useful for validating a TOML file."
        ),
    )
    args = p.parse_args()

    if not args.config.is_file():
        sys.stderr.write(f"config file not found: {args.config}\n")
        return 2
    try:
        cfg = load_config(args.config)
    except Exception as e:
        sys.stderr.write(f"failed to parse config: {e}\n")
        return 2

    if args.num_matches is not None:
        cfg.run.num_matches = args.num_matches
    if args.seed is not None:
        cfg.run.seed = args.seed

    ts = _dt.datetime.now().strftime("%Y%m%dT%H%M%S")
    if args.out_dir is None:
        out_base = Path.home() / "silicon-system-test-results"
    else:
        out_base = args.out_dir
    bundle_dir = out_base / ts

    if args.dry_run:
        print("dry run — would execute:")
        print(f"  bundle_dir: {bundle_dir}")
        print(f"  server:     {cfg.server.ip}:{cfg.server.port} "
              f"(local={cfg.server.is_local})")
        print(f"  client:     {cfg.client.ip} (local={cfg.client.is_local})")
        print(f"  num_matches: {cfg.run.num_matches} ({2 * cfg.run.num_matches} agents)")
        print(f"  timeout_s:  {cfg.run.timeout_s}")
        print(f"  defaults:   mode={cfg.defaults.mode} "
              f"provider={cfg.defaults.provider} model={cfg.defaults.model}")
        print(f"  seed:       {cfg.run.seed}")
        return 0

    # Deferred import: orchestrator pulls in uvicorn/httpx/etc. which
    # are heavy; don't load them during a dry-run or --help.
    from silicon_pantheon.systemtest.orchestrator import orchestrate

    try:
        result = orchestrate(cfg, bundle_dir)
    except KeyboardInterrupt:
        sys.stderr.write("\ninterrupted; bundle may be incomplete\n")
        return 130
    except Exception as e:
        sys.stderr.write(f"\nsystemtest crashed: {e}\n")
        raise

    # Summary on stdout so the caller can pipe it.
    print(f"\nbundle: {result.bundle_dir}")
    print(f"manifest: {result.manifest_path}")
    print(f"incidents: {result.incidents_path}")
    print(
        f"summary: {result.n_agents} agents, "
        f"{result.n_crashed} crashed, "
        f"{result.wall_clock_s:.1f}s wall clock"
        + (" (TIMED OUT)" if result.timed_out else "")
    )
    return 1 if (result.n_crashed > 0 or result.timed_out) else 0


if __name__ == "__main__":
    raise SystemExit(main())
