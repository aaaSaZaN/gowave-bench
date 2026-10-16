#!/usr/bin/env python3
"""k6 comparison harness. Python 3.10+; Matplotlib exports vector reports."""

import csv
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import platform
import random
import re
import shutil
import signal
import socket
import statistics
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

from . import plots

ROOT = Path(__file__).resolve().parent.parent
WORKLOADS = ("reads", "subscriptions", "writes", "mixed", "stats", "user-list", "user-detail")
MODES = ("rate", "concurrency", "churn")
HEADERS = {"Content-Type": "application/json", "X-Forwarded-For": "192.0.2.10",
           "X-Remnawave-Client-Type": "browser",
           "X-Forwarded-Proto": "https", "User-Agent": "Gowave-comparison-benchmark/1.0"}
DEFAULT_LOAD = dict(rates=[50, 100, 250, 500, 1000, 2000], concurrency=[1, 10, 50, 100, 250, 500],
                    duration_seconds=60, warmup_seconds=10, cooldown_seconds=5, repetitions=3,
                    preallocated_vus=256, max_vus=1024, timeout_seconds=10, max_error_rate=0.01,
                    p99_ms=500, min_completion_ratio=0.99, sample_interval_seconds=0.25)


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")
    temporary.replace(path)


def load_config(path):
    config = json.loads(Path(path).read_text())
    if not config.get("targets"):
        raise ValueError("At least one target is required")
    for name, target in config["targets"].items():
        if not re.fullmatch(r"[a-zA-Z0-9_-]+", name):
            raise ValueError("Target names must contain only letters, digits, _ or -")
        url = urllib.parse.urlsplit(target["url"])
        if url.scheme not in ("http", "https") or not url.hostname or url.username or url.password or url.query or url.fragment or url.path not in ("", "/"):
            raise ValueError(f"{name}: url must be an HTTP(S) origin without credentials")
        target["url"] = target["url"].rstrip("/")
    config["load"] = {**DEFAULT_LOAD, **config.get("load", {})}
    load = config["load"]
    for key in ("duration_seconds", "repetitions", "preallocated_vus", "max_vus", "timeout_seconds"):
        if type(load[key]) is not int or load[key] < 1:
            raise ValueError(f"load.{key} must be a positive integer")
    for key in ("warmup_seconds", "cooldown_seconds"):
        if type(load[key]) is not int or load[key] < 0:
            raise ValueError(f"load.{key} must be a nonnegative integer")
    for key in ("rates", "concurrency"):
        if not load[key] or any(type(v) is not int or v <= 0 for v in load[key]):
            raise ValueError(f"load.{key} must contain positive integers")
    if load["max_vus"] < load["preallocated_vus"]:
        raise ValueError("max_vus must be >= preallocated_vus")
    for key in ("max_error_rate", "min_completion_ratio"):
        if not 0 <= load[key] <= 1:
            raise ValueError(f"load.{key} must be in [0, 1]")
    if load["sample_interval_seconds"] <= 0 or load["p99_ms"] <= 0:
        raise ValueError("sample_interval_seconds and p99_ms must be positive")
    dataset = config["dataset"]
    if not re.fullmatch(r"[a-zA-Z0-9_-]{3,16}", dataset["prefix"]):
        raise ValueError("dataset.prefix must be 3-16 letters, digits, _ or -")
    for key in ("users", "hosts"):
        if type(dataset[key]) is not int or dataset[key] < 1:
            raise ValueError(f"dataset.{key} must be a positive integer")
    return config


def selected(config, names):
    names = names or list(config["targets"])
    for name in names:
        if name not in config["targets"]:
            raise ValueError(f"Unknown target: {name}")
    return names


def api(target, method, path, token=None, body=None, missing=False, timeout=10):
    headers = dict(HEADERS)
    if token:
        headers["Authorization"] = "Bearer " + token
    request = urllib.request.Request(target["url"] + path, method=method, headers=headers,
                                     data=None if body is None else json.dumps(body).encode())
    # Never forward credentials across a redirect, or inherit host HTTP proxy settings.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open(request, timeout=timeout) as response:
            value = json.load(response)
    except urllib.error.HTTPError as error:
        if missing and error.code == 404:
            return None
        # Error bodies may contain tokens; don't copy them to artifacts or logs.
        raise RuntimeError(f"{method} {path.split('?')[0]} returned HTTP {error.code}") from None
    if not isinstance(value, dict) or "response" not in value:
        raise RuntimeError(f"{method} {path.split('?')[0]}: missing response envelope")
    return value["response"]


def authenticate(target):
    if target.get("token_env"):
        value = os.environ.get(target["token_env"], "")
        if not value:
            raise ValueError(f"Set environment variable {target['token_env']} to an admin/API token")
        return value
    username = os.environ.get(target.get("username_env", ""), "")
    password = os.environ.get(target.get("password_env", ""), "")
    if not username or not password:
        raise ValueError("Set token_env, or username_env and password_env, for each target")
    return api(target, "POST", "/api/auth/login", body=dict(username=username, password=password))["accessToken"]


def dataset_hash(dataset):
    return hashlib.sha256(json.dumps(dataset, sort_keys=True).encode()).hexdigest()


def instant(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def array(value, key):
    return value if isinstance(value, list) else value[key]


def prepare(config, name, directory):
    target, dataset = config["targets"][name], config["dataset"]
    token = authenticate(target)
    prefix = dataset["prefix"]
    # Keep construction independent of each implementation's internal schema.
    profiles = array(api(target, "GET", "/api/config-profiles", token), "configProfiles")
    profile = next((p for p in profiles if p["name"] == prefix), None)
    if profile is None:
        profile = api(target, "POST", "/api/config-profiles", token, {
            "name": prefix,
            "config": {
                "log": {"loglevel": "warning"},
                "inbounds": [{"tag": "BENCH_VLESS", "port": 443, "protocol": "vless",
                              "settings": {"clients": [], "decryption": "none"},
                              "streamSettings": {"network": "tcp", "security": "tls",
                                                 "tlsSettings": {"serverName": "bench.example.invalid"}}}],
                "outbounds": [{"tag": "DIRECT", "protocol": "freedom"}]
            }
        })
    inbounds = array(api(target, "GET", f"/api/config-profiles/{profile['uuid']}/inbounds", token), "inbounds")
    inbound = next((i for i in inbounds if i["tag"] == "BENCH_VLESS"), None)
    if inbound is None:
        raise ValueError("Existing benchmark profile differs; use a clean database or a new prefix")
    squads = array(api(target, "GET", "/api/internal-squads", token), "internalSquads")
    squad = next((s for s in squads if s["name"] == prefix), None)
    if squad is None:
        squad = api(target, "POST", "/api/internal-squads", token, {"name": prefix, "inbounds": [inbound["uuid"]]})
    elif {i["uuid"] if isinstance(i, dict) else i for i in squad["inbounds"]} != {inbound["uuid"]}:
        raise ValueError("Existing benchmark squad differs; use a clean database or a new prefix")
    hosts = array(api(target, "GET", "/api/hosts", token), "hosts")
    addresses = [f"bench-{i:04d}.example.invalid" for i in range(dataset["hosts"])]
    for i, address in enumerate(addresses):
        remark = f"{prefix}-{i:04d}"
        host = next((h for h in hosts if h["remark"] == remark), None)
        if host is None:
            api(target, "POST", "/api/hosts", token, {
                "inbound": {"configProfileUuid": profile["uuid"], "configProfileInboundUuid": inbound["uuid"]},
                "remark": remark, "address": address, "port": 443, "sni": "bench.example.invalid"
            })
        elif (host["address"] != address or host["port"] != 443 or host.get("isDisabled", False)
              or host.get("inbound", {}).get("configProfileInboundUuid") != inbound["uuid"]):
            raise ValueError("Existing benchmark hosts differ; use a clean database or a new prefix")
    users = []
    for i in range(dataset["users"]):
        username = f"{prefix}_{i:08d}"
        user = api(target, "GET", f"/api/users/by-username/{username}", token, missing=True)
        if user is None:
            user = api(target, "POST", "/api/users", token, {
                "username": username, "expireAt": dataset["expire_at"], "status": "ACTIVE",
                "trafficLimitBytes": 107374182400, "trafficLimitStrategy": "NO_RESET",
                "activeInternalSquads": [squad["uuid"]], "tag": "BENCHMARK",
                "description": "benchmark"
            })
        memberships = {s["uuid"] if isinstance(s, dict) else s for s in user.get("activeInternalSquads", [])}
        if (user["status"] != "ACTIVE" or memberships != {squad["uuid"]}
                or user["trafficLimitBytes"] != 107374182400 or user["trafficLimitStrategy"] != "NO_RESET"
                or instant(user["expireAt"]) != instant(dataset["expire_at"])):
            raise ValueError(f"{username}: existing user differs; use an unchanged dataset or a clean database")
        users.append({"username": username, "shortUuid": user["shortUuid"]})
        if (i + 1) % 100 == 0:
            print(f"{name}: prepared {i + 1}/{dataset['users']} users", flush=True)
    total = api(target, "GET", "/api/users?size=1", token)["total"]
    if total != dataset["users"]:
        raise ValueError(f"Expected a dedicated database with {dataset['users']} users, found {total}")
    total_hosts = len(array(api(target, "GET", "/api/hosts", token), "hosts"))
    if total_hosts != dataset["hosts"]:
        raise ValueError(f"Expected {dataset['hosts']} hosts in a dedicated database, found {total_hosts}")
    manifest = {"schema": 1, "target": name, "url": target["url"], "dataset": dataset,
                "dataset_hash": dataset_hash(dataset), "users": users, "addresses": addresses}
    path = directory / f"{name}.json"
    save(path, manifest)
    print(f"{name}: fixture saved to {path}", flush=True)


def process_rss(roots, proc=Path("/proc")):
    """Sum resident pages for a PID forest, counting each PID once."""
    processes = {}
    for entry in proc.iterdir():
        if not entry.name.isdecimal():
            continue
        try:
            status = (entry / "stat").read_text()
            fields = status[status.rfind(")") + 2:].split()
            processes[int(entry.name)] = (int(fields[1]), int(fields[21]))
        except (OSError, ValueError, IndexError):
            continue  # A process may disappear between enumeration and read.
    if any(pid not in processes for pid in roots):
        raise RuntimeError("A monitored root PID has exited")
    included = set(roots)
    while True:
        more = {pid for pid, (parent, _) in processes.items() if parent in included}
        if more <= included:
            break
        included |= more
    return sum(processes[pid][1] for pid in included) * os.sysconf("SC_PAGE_SIZE")


def memory_reader(target):
    memory = target.get("memory")
    if not memory:
        return None, "disabled"
    kind = memory["kind"]
    if kind == "docker":
        containers = memory.get("containers", [])
        if not containers:
            raise ValueError("memory.containers must identify running benchmark containers")
        info = json.loads(subprocess.check_output(
            [memory.get("engine", "docker"), "inspect", *containers], text=True, timeout=10))
        paths = []
        for container in info:
            pid = container["State"]["Pid"]
            if not container["State"]["Running"] or pid <= 0:
                raise ValueError("A monitored container is stopped")
            membership = Path(f"/proc/{pid}/cgroup").read_text().splitlines()
            relative = next((line[3:] for line in membership if line.startswith("0::")), None)
            if relative is None or ".." in Path(relative).parts:
                raise ValueError("Container memory requires a local Linux cgroup v2 host")
            path = Path("/sys/fs/cgroup") / relative.lstrip("/")
            if not (path / "memory.current").is_file():
                raise ValueError("Container cgroup is not visible on this host; use local Linux or --memory-scope none")
            paths.append(str(path))
        reader, description = memory_reader({"memory": {"kind": "cgroup", "paths": paths}})
        return reader, f"{description}; container scope={memory.get('scope', 'app')}"
    if kind == "proc":
        roots = memory.get("pids", [])
        if not roots or any(type(p) is not int or p <= 0 for p in roots):
            raise ValueError("memory.pids must contain live positive root PIDs")
        return lambda: process_rss(roots), "sum of process-tree RSS (shared pages counted per process)"
    if kind == "cgroup":
        paths = [Path(p) / "memory.current" for p in memory["paths"]]
        if not paths:
            raise ValueError("memory.paths must contain cgroup v2 directories")
        resolved = [p.parent.resolve() for p in paths]
        if len(set(resolved)) != len(resolved) or any(a in b.parents for a in resolved for b in resolved if a != b):
            raise ValueError("Use distinct, non-overlapping cgroups to avoid double-counting")
        return lambda: sum(int(p.read_text()) for p in paths), "cgroup v2 memory.current (includes page cache)"
    raise ValueError("memory.kind must be proc, cgroup or docker")


def memory_summary(samples, errors, description):
    # Trapezoidal time weighting handles scheduler jitter and irregular sample spacing.
    duration = samples[-1][0] - samples[0][0] if len(samples) > 1 else 0
    area = sum((b[0] - a[0]) * (a[1] + b[1]) / 2 for a, b in zip(samples, samples[1:]))
    return {"description": description, "samples": len(samples), "errors": errors,
            "valid": len(samples) >= 2 and errors == 0,
            "sampled_peak_bytes": max((s[1] for s in samples), default=None),
            "time_weighted_average_bytes": area / duration if duration > 0 else None,
            "sampled_duration_seconds": duration}


def stop_process(process):
    if process.poll() is None:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)


def k6_run(args, config, target, token, manifest, workload, mode, level, directory, duration):
    load = config["load"]
    directory.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    # Do not inherit proxies or k6 overrides from the operator's shell.
    for key in list(env):
        if key.startswith(("K6_", "BENCH_")) or key.lower() in ("http_proxy", "https_proxy", "all_proxy"):
            del env[key]
    env.update(BENCH_URL=target["url"], BENCH_TOKEN=token, BENCH_MANIFEST=str(manifest.resolve()),
               BENCH_WORKLOAD=workload, BENCH_MODE=mode, BENCH_LEVEL=str(level), BENCH_DURATION=str(duration),
               BENCH_TIMEOUT=str(load["timeout_seconds"]), BENCH_PREALLOCATED=str(load["preallocated_vus"]),
               BENCH_MAX_VUS=str(load["max_vus"]), BENCH_MAX_ERROR=str(load["max_error_rate"]),
               BENCH_P99=str(load["p99_ms"]), BENCH_SUMMARY=str((directory / "summary.json").resolve()),
               K6_NO_USAGE_REPORT="true")
    command = [args.k6, "run", "--quiet", str(ROOT / "k6/panel.js")]
    if args.raw:
        command[2:2] = ["--out", f"json={directory.resolve() / 'metrics.json.gz'}"]
    reader, description = memory_reader(target)
    if reader:
        reader()  # Fail before the test when the monitor is misconfigured.
    samples, sample_errors = [], 0
    started = time.monotonic()
    limit = (300 if mode == "smoke" else duration) + load["timeout_seconds"] + 60
    with (directory / "k6.log").open("w") as log, (directory / "memory.csv").open("w") as memory_log:
        writer = csv.writer(memory_log)
        writer.writerow(["elapsed_seconds", "bytes"])
        process = subprocess.Popen(command, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        next_notice = started + 30
        try:
            while True:
                elapsed = time.monotonic() - started
                if reader:
                    try:
                        value = reader()
                        samples.append((elapsed, value))
                        writer.writerow([round(elapsed, 6), value])
                    except (OSError, ValueError, RuntimeError):
                        sample_errors += 1
                if process.poll() is not None:
                    break
                if elapsed > limit:
                    raise RuntimeError(f"k6 exceeded watchdog timeout; inspect {directory / 'k6.log'}")
                if time.monotonic() >= next_notice:
                    print(f"  {directory.name}: still running ({elapsed:.0f}s)", flush=True)
                    next_notice += 30
                time.sleep(load["sample_interval_seconds"])
        finally:
            stop_process(process)
    memory = memory_summary(samples, sample_errors, description)
    save(directory / "memory.json", memory)
    summary_path = directory / "summary.json"
    summary = json.loads(summary_path.read_text()) if summary_path.exists() else {}
    return process.returncode, summary, memory


def metric(summary, name, key, default=None):
    return summary.get("metrics", {}).get(name, {}).get("values", {}).get(key, default)


def operation_metrics(summary):
    result = {}
    for name, metric_data in summary.get("metrics", {}).items():
        match = re.fullmatch(r"bench_latency\{action:([^}]+)\}", name)
        if match:
            values = metric_data.get("values", {})
            result[match[1]] = {"p50_ms": values.get("med"), "p95_ms": values.get("p(95)"),
                                "p99_ms": values.get("p(99)"), "samples": values.get("count")}
    return result


def read_records(directory):
    records = json.loads((directory / "results.json").read_text())
    for record in records:
        if "operations" not in record and record.get("directory"):
            # Older runs already contain these measured submetrics in the k6 summary.
            path = directory / record["directory"] / "summary.json"
            if path.is_file():
                record["operations"] = operation_metrics(json.loads(path.read_text()))
    return records


def score(summary, mode, level, duration, load, exit_code):
    count = metric(summary, "bench_requests", "count", 0)
    success = metric(summary, "bench_successes", "count", 0)
    dropped = metric(summary, "dropped_iterations", "count", 0)
    # Counters' rates use k6's actual test interval, including final in-flight completions.
    rps = metric(summary, "bench_successes", "rate", 0)
    p99 = metric(summary, "bench_latency", "p(99)")
    error_rate = metric(summary, "bench_errors", "rate")
    completion = count / (level * duration) if mode == "rate" else None
    passed = (exit_code == 0 and count > 0 and error_rate is not None and
              error_rate <= load["max_error_rate"] and p99 is not None and p99 <= load["p99_ms"] and
              (mode != "rate" or (dropped == 0 and completion >= load["min_completion_ratio"])))
    return dict(requests=count, successful_requests=success, successful_rps=rps,
                error_rate=error_rate, dropped_iterations=dropped, completion_ratio=completion,
                p50_ms=metric(summary, "bench_latency", "med"),
                p95_ms=metric(summary, "bench_latency", "p(95)"), p99_ms=p99,
                p999_ms=metric(summary, "bench_latency", "p(99.9)"),
                max_ms=metric(summary, "bench_latency", "max"),
                connecting_p99_ms=metric(summary, "bench_connecting", "p(99)"),
                tls_p99_ms=metric(summary, "bench_tls", "p(99)"),
                operations=operation_metrics(summary), passed=passed)


def run_load(args, config):
    names = selected(config, args.targets)
    k6 = shutil.which(args.k6)
    if not k6:
        raise ValueError("k6 was not found; install it or pass --k6 /absolute/path/to/k6")
    args.k6 = k6
    destination = Path(args.output).resolve()
    destination.mkdir(parents=True, exist_ok=False)
    tokens, fixtures = {}, {}
    for name in names:
        target = config["targets"][name]
        path = Path(args.fixtures) / f"{name}.json"
        manifest = json.loads(path.read_text())
        if manifest["url"] != target["url"] or manifest["dataset_hash"] != dataset_hash(config["dataset"]):
            raise ValueError(f"{name}: stale fixtures; run prepare again")
        if len(manifest["users"]) != config["dataset"]["users"]:
            raise ValueError(f"{name}: incomplete fixture")
        tokens[name] = authenticate(target)
        fixtures[name] = path
    load = config["load"]
    version = subprocess.check_output([args.k6, "version"], text=True).strip()
    metadata = {"schema": 1, "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "k6": version, "python": platform.python_version(), "platform": platform.platform(),
                "cpu_count": os.cpu_count(), "dataset": config["dataset"],
                "experiment_label": config.get("experiment_label"),
                "dataset_hash": dataset_hash(config["dataset"]), "load": load,
                "targets": {n: {k: v for k, v in config["targets"][n].items() if k in ("url", "metadata", "memory")} for n in names},
                "seed": args.seed, "raw": args.raw}
    save(destination / "metadata.json", metadata)
    records = []
    if args.smoke:
        jobs = [(w, "smoke", 1, 1) for w in args.workloads]
    else:
        jobs = [(w, m, level, rep) for w in args.workloads for m in args.modes
                for level in (load["rates"] if m == "rate" else load["concurrency"])
                for rep in range(1, load["repetitions"] + 1)]
    # Shuffle blocks, then alternate implementation order within every paired block.
    rng = random.Random(args.seed)
    rng.shuffle(jobs)
    save(destination / "schedule.json", jobs)
    failures = 0
    for index, (workload, mode, level, rep) in enumerate(jobs):
        order = names[index % len(names):] + names[:index % len(names)]
        for name in order:
            label = f"{index:04d}-{name}-{workload}-{mode}-{level}-r{rep}"
            print(f"[{index + 1}/{len(jobs)}] {label}", flush=True)
            target = config["targets"][name]
            if load["warmup_seconds"] and not args.smoke:
                code, summary, _ = k6_run(args, config, target, tokens[name], fixtures[name], workload,
                                          "concurrency", 1, destination / (label + "-warmup"), load["warmup_seconds"])
                if code not in (0, 99) or metric(summary, "bench_errors", "rate", 1) > load["max_error_rate"]:
                    raise RuntimeError(f"{name}: warmup failed; inspect {label}-warmup/k6.log")
            directory = destination / label
            code, summary, memory = k6_run(args, config, target, tokens[name], fixtures[name], workload,
                                            mode, level, directory, load["duration_seconds"])
            record = {"target": name, "workload": workload, "mode": mode, "level": level, "repetition": rep,
                      "directory": label, "exit_code": code,
                      **score(summary, mode, level, load["duration_seconds"], load, code), "memory": memory}
            save(directory / "result.json", record)
            records.append(record)
            save(destination / "results.json", records)
            if not record["passed"]:
                failures += 1
            if code not in (0, 99):
                raise RuntimeError(f"k6 failed with exit {code}; inspect {directory / 'k6.log'}")
            print(f"  {record['successful_rps']:.1f} successful req/s; p99={record['p99_ms']}; passed={record['passed']}", flush=True)
            if load["cooldown_seconds"] and not args.smoke:
                time.sleep(load["cooldown_seconds"])
    report(destination)
    return 2 if failures else 0


def report(directory):
    directory = Path(directory)
    if not (directory / "results.json").exists() and (directory / "startup.json").exists():
        return startup_report(directory)
    records = read_records(directory)
    metadata_path = directory / "metadata.json"
    metadata = json.loads(metadata_path.read_text()) if metadata_path.exists() else {}
    expected_repetitions = metadata.get("load", {}).get("repetitions", 1)
    images = plots.render(directory, records, metadata, title=metadata.get("experiment_label") or "Gowave / Remnawave — benchmark comparison")
    lines = ["# Panel benchmark results", "", "![Benchmark comparison](comparison.svg)", "",
             "SVG figures are vector images suitable for sharing and printing. "
             "Latency charts show the median of per-run percentiles; they do not estimate a pooled percentile.", "",
             "Rate-controlled runs measure response under offered load; they do not establish maximum throughput when every rate passes. "
             "Use concurrent-client sweeps for observed throughput and increase the range until saturation is visible.", "",
             "Each row is a separate run. Latency includes client connection/queue time. Memory covers the k6 invocation, including VU initialization and drain.", "",
             "| Target | Workload | Mode | Level | Repeat | Success req/s | p95 ms | p99 ms | p99.9 ms | Errors % | Drops | Avg MiB | Peak MiB | SLO |",
             "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|"]
    def fmt(value):
        return "n/a" if value is None else f"{value:.2f}"
    for r in sorted(records, key=lambda r: (r["workload"], r["mode"], r["level"], r["target"], r["repetition"])):
        memory = r["memory"]
        avg = memory["time_weighted_average_bytes"] if memory["valid"] else None
        peak = memory["sampled_peak_bytes"] if memory["valid"] else None
        values = [r["target"], r["workload"], r["mode"], str(r["level"]), str(r["repetition"]),
                  fmt(r["successful_rps"]), fmt(r["p95_ms"]), fmt(r["p99_ms"]), fmt(r["p999_ms"]),
                  fmt(None if r["error_rate"] is None else 100 * r["error_rate"]), str(r["dropped_iterations"]),
                  fmt(None if avg is None else avg / 2**20), fmt(None if peak is None else peak / 2**20),
                  "PASS" if r["passed"] else "FAIL"]
        lines.append("| " + " | ".join(values) + " |")
    if any(r["mode"] == "smoke" for r in records):
        lines += ["", "Smoke validation only: these short runs do not establish throughput or latency performance."]
    lines += ["", "## Tested load coverage", "",
              "A level qualifies only when every repetition passes. Rate-mode levels are offered-load coverage, not maximum capacity. "
              "Highest observed throughput may include failing runs; the overview uses passing concurrent-client trials. No percentile is averaged across runs.", "",
              "| Target | Workload | Mode | Highest observed success req/s | Highest passing level | Median success req/s at passing level |",
              "|---|---|---|---:|---:|---:|"]
    groups = sorted({(r["target"], r["workload"], r["mode"]) for r in records if r["mode"] != "smoke"})
    for target, workload, mode in groups:
        rows = [r for r in records if (r["target"], r["workload"], r["mode"]) == (target, workload, mode)]
        levels = sorted({r["level"] for r in rows})
        passing = []
        for level in levels:
            trials = [r for r in rows if r["level"] == level]
            if (len(trials) == expected_repetitions and
                    {r["repetition"] for r in trials} == set(range(1, expected_repetitions + 1)) and
                    all(r["passed"] for r in trials)):
                passing.append(level)
        best = max(passing) if passing else None
        median = statistics.median(r["successful_rps"] for r in rows if r["level"] == best) if best else None
        lines.append(f"| {target} | {workload} | {mode} | {max(r['successful_rps'] for r in rows):.2f} | {best or 'none'} | {fmt(median)} |")
    lines += ["", "See metadata.json, schedule.json, each run's result.json/summary.json, memory.csv and k6.log. Sampled memory peaks can miss spikes between samples.", ""]
    if len(images) > 1:
        lines += ["## Load response charts", ""]
        lines += [f"- [{image.removesuffix('.svg')}]({image})" for image in images[1:]]
        lines.append("")
    (directory / "report.md").write_text("\n".join(lines))
    print(f"Report: {directory / 'report.md'}", flush=True)


def tcp_accepts(target, timeout):
    url = urllib.parse.urlsplit(target["url"])
    try:
        with socket.create_connection((url.hostname, url.port or (443 if url.scheme == "https" else 80)), timeout=timeout):
            return True
    except OSError:
        return False


def command_args(command):
    if not isinstance(command, list) or not command or any(not isinstance(x, str) or not x for x in command):
        raise ValueError("startup start/stop must be nonempty argument arrays; shell strings are not supported")
    return command


def run_startup(args, config):
    names = selected(config, args.targets)
    destination = Path(args.output).resolve()
    destination.mkdir(parents=True, exist_ok=False)
    records = []
    save(destination / "metadata.json", {"experiment_label": config.get("experiment_label"),
         "platform": platform.platform(), "repetitions": args.repetitions,
         "targets": {name: {key: value for key, value in config["targets"][name].items()
                            if key in ("url", "metadata", "memory")} for name in names}})
    for name in names:
        target = config["targets"][name]
        startup = target.get("startup")
        if not startup:
            raise ValueError(f"{name}: configure startup.start and startup.stop")
        start, stop = command_args(startup["start"]), command_args(startup["stop"])
        token = authenticate(target)  # Acquire before stopping the service.
        timeout = startup.get("timeout_seconds", 120)
        poll = startup.get("poll_interval_seconds", 0.01)
        if timeout <= 0 or poll <= 0:
            raise ValueError("Startup timeouts/poll interval must be positive")
        save(destination / f"{name}-metadata.json", {"target": name, "url": target["url"],
             "metadata": target.get("metadata", {}), "startup": startup,
             "clock": "time.monotonic", "includes": "launch command overhead; existing database migrations; excludes dependency startup unless start command includes it"})
        for rep in range(1, args.repetitions + 1):
            print(f"{name}: startup trial {rep}/{args.repetitions}", flush=True)
            with (destination / f"{name}-{rep}.log").open("w") as log:
                subprocess.run(stop, check=True, stdout=log, stderr=subprocess.STDOUT, timeout=timeout)
                deadline = time.monotonic() + timeout
                while tcp_accepts(target, min(0.1, timeout)):
                    if time.monotonic() >= deadline:
                        raise RuntimeError("Port remains open after stop command; point startup directly at the backend")
                    time.sleep(poll)
                process = None
                started = time.monotonic()
                first_tcp = first_http = None
                try:
                    process = subprocess.Popen(start, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
                    next_notice = started + 30
                    while time.monotonic() - started < timeout:
                        remaining = timeout - (time.monotonic() - started)
                        if first_tcp is None and tcp_accepts(target, min(poll, remaining)):
                            first_tcp = (time.monotonic() - started) * 1000
                        if first_tcp is not None:
                            try:
                                value = api(target, "GET", "/api/users?size=1", token, timeout=min(0.25, max(0.001, remaining)))
                                if (isinstance(value, dict) and isinstance(value.get("users"), list)
                                        and isinstance(value.get("total"), (int, float))):
                                    first_http = (time.monotonic() - started) * 1000
                                    break
                            except (OSError, ValueError, RuntimeError):
                                pass
                        if process.poll() not in (None, 0):
                            raise RuntimeError("Startup command failed; inspect trial log")
                        if time.monotonic() >= next_notice:
                            print(f"  {name}: waiting for API readiness", flush=True)
                            next_notice += 30
                        time.sleep(poll)
                    if first_http is None:
                        raise RuntimeError(f"{name}: API readiness timed out after {timeout}s")
                    records.append(dict(target=name, repetition=rep, tcp_accept_ms=first_tcp, api_ready_ms=first_http))
                    save(destination / "startup.json", records)
                except BaseException:
                    records.append(dict(target=name, repetition=rep, tcp_accept_ms=first_tcp,
                                        api_ready_ms=first_http, failed=True))
                    save(destination / "startup.json", records)
                    raise
                finally:
                    try:
                        subprocess.run(stop, check=True, stdout=log, stderr=subprocess.STDOUT, timeout=timeout)
                    finally:
                        if process is not None:
                            stop_process(process)
            print(f"  TCP={first_tcp:.2f}ms; API={first_http:.2f}ms", flush=True)
    startup_report(destination)
    return 0


def startup_report(destination):
    records = json.loads((destination / "startup.json").read_text())
    metadata_path = destination / "metadata.json"
    metadata = json.loads(metadata_path.read_text()) if metadata_path.exists() else {}
    plots.render(destination, startup=records, title=metadata.get("experiment_label") or "Gowave / Remnawave — startup comparison")
    lines = ["# Startup results", "", "![Startup comparison](comparison.svg)", "",
             "| Target | Successful trials | Failed trials | TCP median ms | TCP max ms | API median ms | API max ms |",
             "|---|---:|---:|---:|---:|---:|---:|"]
    names = sorted({r["target"] for r in records})
    for name in names:
        rows = [r for r in records if r["target"] == name and not r.get("failed")]
        failures = sum(r["target"] == name and bool(r.get("failed")) for r in records)
        values = [f"{fn(r[key] for r in rows):.2f}" if rows else "n/a"
                  for key in ("tcp_accept_ms", "api_ready_ms") for fn in (statistics.median, max)]
        lines.append(f"| {name} | {len(rows)} | {failures} | " + " | ".join(values) + " |")
    lines += ["", "Times include launch-command overhead and polling uncertainty. API time ends after a successful authenticated database-backed response. Dependencies remain running unless configured otherwise. Targets are left stopped.", ""]
    (destination / "report.md").write_text("\n".join(lines))
    return 0


def add_commands(sub):
    for command in ("prepare", "run", "startup"):
        p = sub.add_parser(command)
        p.add_argument("--config", default=str(ROOT / "config/local.json"))
        p.add_argument("--targets", nargs="+")
        if command in ("prepare", "run"):
            p.add_argument("--fixtures", default=str(ROOT / "fixtures"))
        if command != "prepare":
            p.add_argument("--output", required=True, help="New directory; existing results are never overwritten")
        if command == "run":
            p.add_argument("--k6", default="k6")
            p.add_argument("--workloads", nargs="+", choices=WORKLOADS, default=["reads", "subscriptions"])
            p.add_argument("--modes", nargs="+", choices=MODES, default=["rate", "concurrency"])
            p.add_argument("--seed", type=int, default=20261016)
            p.add_argument("--raw", action="store_true", help="Write all k6 metric samples (can affect generator capacity)")
            p.add_argument("--smoke", action="store_true", help="One request per workload mix slot, no warmup")
        if command == "startup":
            p.add_argument("--repetitions", type=int, default=10)
    p = sub.add_parser("report")
    p.add_argument("directory", type=Path)


def execute(args):
    if args.command in ("run", "startup", "report"):
        plots.plotting()  # Check the chart dependency before starting a long experiment.
    if args.command == "report":
        report(args.directory)
        return 0
    config = load_config(args.config)
    if config.get("managed_lab"):
        from . import containers
        env = containers.environment()
        for key in ("BENCH_ADMIN_USERNAME", "BENCH_ADMIN_PASSWORD"):
            os.environ[key] = env[key]
    if args.command == "prepare":
        for name in selected(config, args.targets):
            prepare(config, name, Path(args.fixtures))
        return 0
    if args.command == "startup":
        if args.repetitions < 1:
            raise ValueError("repetitions must be positive")
        return run_startup(args, config)
    return run_load(args, config)

