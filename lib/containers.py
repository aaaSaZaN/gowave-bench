#!/usr/bin/env python3
"""Manage the isolated Docker/PostgreSQL benchmark lab; never remove its volumes."""
import json
import os
from pathlib import Path
import secrets
import subprocess
import time

ROOT = Path(__file__).resolve().parent.parent / "docker"
from . import engine as bench

ENV_FILE = ROOT / ".env"
CONFIG_FILE = ROOT.parent / "config/local.json"


def environment():
    if not ENV_FILE.exists():
        raise ValueError("Run benchmarks/bench.py init --remnawave-image IMAGE first")
    values = {}
    for line in ENV_FILE.read_text().splitlines():
        if line.strip() and not line.startswith("#"):
            key, value = line.split("=", 1)
            values[key] = value
    # The same values must reach Compose and the harness, regardless of the caller's shell.
    return {**os.environ, **values}


def compose(engine):
    return [engine, "compose", "--project-name", "gowave-benchmark", "--env-file", str(ENV_FILE),
            "--file", str(ROOT / "compose.yaml")]


def initialize(image):
    if ENV_FILE.exists():
        raise ValueError(f"{ENV_FILE} already exists; edit it to preserve database credentials")
    if any(c.isspace() for c in image) or "=" in image or not image:
        raise ValueError("Supply an exact image tag or digest")
    if ":" not in image.rsplit("/", 1)[-1] or image.endswith(":latest"):
        raise ValueError("Use a versioned Remnawave tag or @sha256 digest, not latest")
    revision = subprocess.check_output(["git", "-C", str(ROOT.parent.parent), "rev-parse", "HEAD"], text=True).strip()
    values = dict(REMNAWAVE_IMAGE=image, POSTGRES_IMAGE="docker.io/library/postgres:17",
                  VALKEY_IMAGE="docker.io/valkey/valkey:9-alpine", APP_SECRET=secrets.token_hex(32),
                  POSTGRES_PASSWORD=secrets.token_hex(24), BENCH_ADMIN_USERNAME="benchmark",
                  BENCH_ADMIN_PASSWORD=secrets.token_urlsafe(32) + "Aa1", GOWAVE_REVISION=revision,
                  APP_CPUS="2", APP_MEMORY="2g", DB_CPUS="1", DB_MEMORY="1g",
                  CACHE_CPUS="0.5", CACHE_MEMORY="256m", GO_PROCS="2", API_INSTANCES="1",
                  GOWAVE_PORT="33000", REMNAWAVE_PORT="33010")
    descriptor = os.open(ENV_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "w") as file:
        file.write("# Private benchmark credentials and deployment settings. No shell syntax.\n")
        file.write("\n".join(f"{key}={value}" for key, value in values.items()) + "\n")
    print(f"Created {ENV_FILE}. Edit image pins/resources before running up.")


def inspect_container(engine, container, env):
    return json.loads(subprocess.check_output([engine, "inspect", container], env=env, text=True))[0]


def public_metadata(info):
    # A full inspect contains credentials in Config.Env; never save it to results.
    host = info.get("HostConfig", {})
    labels = info.get("Config", {}).get("Labels") or {}
    return {"container_id": info["Id"], "image_id": info["Image"],
            "image_reference": info.get("Config", {}).get("Image"),
            "revision": labels.get("org.opencontainers.image.revision"),
            "limits": {key: host.get(key) for key in ("NanoCpus", "CpuQuota", "CpuPeriod", "CpusetCpus",
                                                        "Memory", "MemorySwap", "PidsLimit", "Ulimits")},
            "network_mode": host.get("NetworkMode")}


def configure(args, env):
    if CONFIG_FILE.exists() and not args.replace_config:
        raise ValueError(f"{CONFIG_FILE} exists; pass --replace-config to replace only the benchmark config")
    config = json.loads((ROOT.parent / "config/example.json").read_text())
    config["managed_lab"] = True
    runtime = subprocess.check_output([args.engine, "--version"], env=env, text=True).strip()
    infos = {}
    for service in ("gowave", "remnawave", "gowave-db", "remnawave-db", "valkey"):
        container = subprocess.check_output(compose(args.engine) + ["ps", "-q", service], env=env, text=True).strip()
        if not container:
            raise ValueError(f"{service} is not running; run benchmarks/bench.py up first")
        info = inspect_container(args.engine, container, env)
        if not info["State"]["Running"]:
            raise ValueError(f"{service} is not running")
        infos[service] = info
    for name in ("gowave", "remnawave"):
        info = infos[name]
        target = config["targets"][name]
        port = env["GOWAVE_PORT" if name == "gowave" else "REMNAWAVE_PORT"]
        target.update(url=f"http://127.0.0.1:{port}", username_env="BENCH_ADMIN_USERNAME",
                      password_env="BENCH_ADMIN_PASSWORD")
        target.pop("token_env", None)
        members = [name] if args.memory_scope == "app" else [name, name + "-db"] + (["valkey"] if name == "remnawave" else [])
        if args.memory_scope != "none":
            target["memory"] = {"kind": "docker", "engine": args.engine,
                                "containers": [infos[member]["Id"] for member in members], "scope": args.memory_scope}
            reader, _ = bench.memory_reader(target)
            reader()
        target["startup"] = {"start": [args.engine, "start", info["Id"]],
                             "stop": [args.engine, "stop", "--time", "10", info["Id"]],
                             "timeout_seconds": 180, "poll_interval_seconds": 0.01}
        target["metadata"] = {"deployment": "container / published loopback port / bridge network",
                              "container_runtime": runtime,
                              "memory_scope": args.memory_scope, "application": public_metadata(info),
                              "database": public_metadata(infos[name + "-db"]),
                              "api_instances": env["API_INSTANCES"] if name == "remnawave" else None,
                              "go_maxprocs": env["GO_PROCS"] if name == "gowave" else None,
                              "database_pool": "50 per process; PostgreSQL max_connections=200"}
        if name == "remnawave":
            target["metadata"]["valkey"] = public_metadata(infos["valkey"])
        deadline = time.monotonic() + 180
        next_notice = time.monotonic() + 30
        while True:
            try:
                bench.api(target, "GET", "/api/auth/status")
                break
            except (OSError, ValueError, RuntimeError):
                if time.monotonic() >= deadline:
                    raise RuntimeError(f"{name}: API did not become available; inspect container logs")
                if time.monotonic() >= next_notice:
                    print(f"Waiting for {name} API...", flush=True)
                    next_notice += 30
                time.sleep(0.25)
        if args.register:
            status = bench.api(target, "GET", "/api/auth/status")
            if status.get("isRegisterAllowed"):
                bench.api(target, "POST", "/api/auth/register", body={
                    "username": env["BENCH_ADMIN_USERNAME"], "password": env["BENCH_ADMIN_PASSWORD"]})
        # Verify that existing databases still use the credentials stored by init.
        token = bench.api(target, "POST", "/api/auth/login", body={
            "username": env["BENCH_ADMIN_USERNAME"], "password": env["BENCH_ADMIN_PASSWORD"]})["accessToken"]
        bench.api(target, "GET", "/api/users?size=1", token)
    bench.save(CONFIG_FILE, config)
    print(f"Configured {CONFIG_FILE}; memory scope: {args.memory_scope}. Credentials stay in docker/.env.")


def add_commands(sub):
    p = sub.add_parser("init")
    p.add_argument("--remnawave-image", required=True, help="Exact tag or digest; e.g. remnawave/backend:3.4.5")
    sub.add_parser("up")
    sub.add_parser("down")
    p = sub.add_parser("configure")
    p.add_argument("--register", action="store_true", help="Register the first administrator on these isolated stacks")
    p.add_argument("--replace-config", action="store_true")
    p.add_argument("--memory-scope", choices=("app", "stack", "none"), default="app")


def execute(args):
    if args.command == "init":
        initialize(args.remnawave_image)
        return 0
    env = environment()
    if args.command in ("up", "down"):
        command = compose(args.engine) + (["up", "--detach", "--build"] if args.command == "up" else ["down"])
        return subprocess.call(command, env=env)
    if args.command == "configure":
        configure(args, env)
        return 0
