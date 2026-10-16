#!/usr/bin/env python3
"""Run the container lab, workloads and visual reports through one entry point."""
import argparse
import os
from pathlib import Path
import subprocess
import sys

sys.dont_write_bytecode = True
from lib import containers, engine, plots

ROOT = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", default="docker", help="Docker-compatible CLI used for the managed lab")
    commands = parser.add_subparsers(dest="command", required=True)
    containers.add_commands(commands)
    engine.add_commands(commands)
    plots.add_commands(commands)
    check = commands.add_parser("check", help="Run the suite's regression checks")
    check.add_argument("--server", type=Path, help="Also validate against this prebuilt disposable Gowave server")
    check.add_argument("--k6", default="k6")
    check.add_argument("--full", action="store_true")
    args = parser.parse_args()
    if args.command in ("init", "up", "down", "configure"):
        return containers.execute(args)
    if args.command == "figures":
        return plots.execute(args)
    if args.command == "check":
        env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
        subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", str(ROOT / "tests"), "-v"], env=env, check=True)
        subprocess.run(["node", str(ROOT / "tests/workload.test.mjs")], env=env, check=True)
        if args.server:
            command = [sys.executable, str(ROOT / "tests/live_smoke.py"), "--server", str(args.server.resolve()), "--k6", args.k6]
            if args.full:
                command.append("--full")
            subprocess.run(command, env=env, check=True)
        return 0
    return engine.execute(args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("Interrupted; completed artifacts are preserved.", file=sys.stderr)
        sys.exit(130)
    except (OSError, ValueError, RuntimeError, KeyError, subprocess.SubprocessError) as error:
        print(f"Benchmark error: {error}", file=sys.stderr)
        sys.exit(1)
