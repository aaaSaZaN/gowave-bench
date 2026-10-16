#!/usr/bin/env python3
"""Exercise the real fixture API, all k6 workloads/modes, RSS monitor and startup probe.

Uses a disposable SQLite Gowave process. This validates the suite, not comparative performance.
"""
import argparse
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib import engine as bench


def free_port():
    with socket.socket() as connection:
        connection.bind(("127.0.0.1", 0))
        return connection.getsockname()[1]


def main():
    # This helper is also the explicit start/stop command used by the startup test.
    if sys.argv[1] == "_start":
        Path(sys.argv[2]).write_text(str(os.getpid()))
        os.execv(sys.argv[3], [sys.argv[3]])
    if sys.argv[1] == "_stop":
        try:
            os.kill(int(Path(sys.argv[2]).read_text()), signal.SIGTERM)
        except (ProcessLookupError, FileNotFoundError):
            pass
        return
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", required=True, type=Path)
    parser.add_argument("--k6", default="k6")
    parser.add_argument("--full", action="store_true", help="Also run two-second rate, concurrency and churn tests")
    args = parser.parse_args()
    server = args.server.resolve()
    with tempfile.TemporaryDirectory(prefix="gowave-benchmark-smoke-") as temporary:
        directory = Path(temporary)
        env = dict(os.environ, APP_SECRET=secrets.token_hex(32), APP_PORT=str(free_port()),
                   METRICS_PORT=str(free_port()), DB_DRIVER="sqlite", DATABASE_URL=str(directory / "test.db"))
        pid_file = directory / "server.pid"
        script = str(Path(__file__).resolve())
        start = [sys.executable, script, "_start", str(pid_file), str(server)]
        stop = [sys.executable, script, "_stop", str(pid_file)]
        target = {"url": "http://127.0.0.1:" + env["APP_PORT"], "token_env": "LIVE_SMOKE_TOKEN"}
        with (directory / "server.log").open("w") as log:
            process = subprocess.Popen(start, cwd=directory, env=env, stdout=log, stderr=subprocess.STDOUT)
            try:
                deadline = time.monotonic() + 30
                while True:
                    try:
                        token = bench.api(target, "POST", "/api/auth/register", body={
                            "username": "benchmark", "password": secrets.token_urlsafe(32) + "Aa1"})["accessToken"]
                        break
                    except OSError:
                        if time.monotonic() >= deadline or process.poll() is not None:
                            raise RuntimeError((directory / "server.log").read_text())
                        time.sleep(0.05)
                env["LIVE_SMOKE_TOKEN"] = token
                target["memory"] = {"kind": "proc", "pids": [process.pid]}
                target["startup"] = {"start": start, "stop": stop, "timeout_seconds": 30}
                config = {"targets": {"smoke": target},
                          "dataset": {"prefix": "bench", "users": 12, "hosts": 3, "expire_at": "2035-01-01T00:00:00.000Z"},
                          "load": {**bench.DEFAULT_LOAD, "rates": [5], "concurrency": [2], "repetitions": 1,
                                   "duration_seconds": 2, "warmup_seconds": 1, "cooldown_seconds": 0,
                                   "preallocated_vus": 4, "max_vus": 16, "p99_ms": 5000}}
                config_path = directory / "config.json"
                bench.save(config_path, config)
                common = [sys.executable, str(bench.ROOT / "bench.py")]
                subprocess.run(common + ["prepare", "--config", str(config_path), "--fixtures", str(directory / "fixtures")], env=env, check=True)
                # Preparing twice checks resumability without duplicating users or hosts.
                subprocess.run(common + ["prepare", "--config", str(config_path), "--fixtures", str(directory / "fixtures")], env=env, check=True)
                run = common + ["run", "--config", str(config_path), "--fixtures", str(directory / "fixtures"),
                                "--k6", args.k6, "--workloads", *bench.WORKLOADS]
                subprocess.run(run + ["--smoke", "--output", str(directory / "smoke")], env=env, check=True)
                if args.full:
                    subprocess.run(run + ["--modes", *bench.MODES, "--raw", "--output", str(directory / "load")], env=env, check=True)
                subprocess.run(common + ["startup", "--config", str(config_path), "--repetitions", "2",
                                         "--output", str(directory / "startup")], cwd=directory, env=env, check=True)
                for result in (directory / "smoke").glob("*/result.json"):
                    assert json.loads(result.read_text())["memory"]["valid"]
                assert (directory / "smoke/comparison.svg").is_file()
                assert (directory / "startup/comparison.svg").is_file()
                if args.full:
                    assert (directory / "load/reads-rate.svg").is_file()
                print("Live smoke passed: API fixtures, all workloads, memory sampling and startup.")
            except BaseException:
                for log_path in directory.rglob("*.log"):
                    print(f"--- {log_path.name} ---\n{log_path.read_text()[-6000:]}", file=sys.stderr)
                raise
            finally:
                subprocess.run(stop, check=False, env=env)
                process.wait(timeout=10)


if __name__ == "__main__":
    main()
