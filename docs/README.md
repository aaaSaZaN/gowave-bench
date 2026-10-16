# Panel benchmarks

`benchmarks/bench.py` is the only user-facing entry point. It manages the container lab, data preparation, k6 runs, startup trials, validation and SVG reports. All commands below run from the repository root.

```text
benchmarks/
  bench.py                 orchestrator
  config/                  example/local configuration and Python requirements
  docker/                  image build and Compose definitions; private .env
  lib/                     internal orchestration, measurement and chart modules
  k6/                      HTTP workload implementation
  tests/                   regression and disposable-instance checks
  docs/                    this guide
  fixtures/                generated target-local identifiers (ignored)
  results/                 measurements and figures (ignored)
```

## Setup

Install Python 3.10+, k6 and Docker Engine with Compose. k6 1.6.1 was used for validation; use the same version for both panels. Node is needed only by the suite's own regression checks. Matplotlib produces standalone vector images; Grafana and a cloud account are not required.

```sh
python3 -m venv benchmarks/.venv
benchmarks/.venv/bin/pip install -r benchmarks/config/requirements.txt
source benchmarks/.venv/bin/activate

python3 benchmarks/bench.py init \
  --remnawave-image docker.io/remnawave/backend:3.4.5
python3 benchmarks/bench.py up
python3 benchmarks/bench.py configure --register
python3 benchmarks/bench.py prepare
```

Prefer image digests when publishing results. `init` generates private credentials in `docker/.env` and refuses to overwrite them. Review that file before `up`. The default resource budget is 2 CPUs/2 GiB per panel, 1 CPU/1 GiB per PostgreSQL container, and 0.5 CPU/256 MiB for Remnawave's required Valkey. Allow additional host/build/load-generator headroom. Both databases use the same PostgreSQL image, settings and volume type.

Both panels receive requests through equivalent published loopback ports and bridge networks. This includes the deployed container network path; it does not isolate a universal fixed container overhead. Defaults are Gowave at `127.0.0.1:33000` and Remnawave at `127.0.0.1:33010`. Database/cache ports are private. Go runs as a non-root user; Remnawave retains its normal migration/seed entrypoint and process manager.

`configure --register` registers an administrator only on these isolated services if registration is allowed, then verifies the stored credentials. It writes `config/local.json` with container/image IDs, runtime version, resource limits, memory scope, URLs and restart commands. Re-run it with `--replace-config` after recreating containers. Commands using a managed-lab config load its administrator credentials automatically.

The default memory scope includes the whole application container and all of its workers. Use `configure --replace-config --memory-scope stack` for app + PostgreSQL, plus Valkey for Remnawave. Keep app-only and stack-total experiments separate. Automatic memory accounting needs a local Linux cgroup v2 host. Use `--memory-scope none` on Docker Desktop/remote daemons or when cgroups are unavailable; missing memory is never reported as zero.

For Podman use `python3 benchmarks/bench.py --engine podman ...` on lab commands and label those results as Podman. The validation artifacts included in this workspace were collected with Podman through a Docker-compatible CLI.

## Workloads and measurements

Preparation creates the same logical dataset through each API: 1,000 active users, 20 TLS/VLESS hosts, one benchmark profile and one squad. Reserved `.invalid` host names require no actual proxy nodes. Preparation resumes missing records without duplicating existing users. Changed datasets require fresh dedicated databases; unrelated users or hosts cause a consistency failure. Fixture files contain subscription identifiers and should remain private.

| Workload | Request mix |
|---|---|
| `reads` | 30% paginated user list, 40% user detail, 20% dashboard stats, 10% nodes |
| `subscriptions` | 40% Xray JSON, 30% Mihomo, 30% sing-box |
| `writes` | 100% user-description updates |
| `mixed` | 10% list, 20% detail, 10% stats, 40% subscriptions, 20% updates |
| `stats` | Dashboard statistics only |
| `user-list` | Paginated user list only |
| `user-detail` | User detail only |

Every k6 iteration makes one HTTP request. Login, fixture creation, retries and redirects do not inflate the request rate. Checks reject errors, empty subscriptions, HTML and unexpected envelopes. Every configured host must appear in rendered subscriptions. Writes keep the user table size bounded, although history/outbox records can grow.

```sh
python3 benchmarks/bench.py run --smoke \
  --workloads reads subscriptions writes mixed \
  --output benchmarks/results/smoke-01

python3 benchmarks/bench.py run \
  --workloads reads subscriptions --modes rate concurrency \
  --output benchmarks/results/load-01

python3 benchmarks/bench.py run \
  --workloads reads --modes churn \
  --output benchmarks/results/churn-01
```

`rate` uses independent constant-arrival-rate runs to expose latency and errors as offered load rises. `concurrency` repeatedly sends requests from fixed numbers of clients using persistent connections, exposing actual achieved throughput. `churn` repeats that experiment with new connections per request. VUs are active clients, not a promise of one open TCP socket, particularly with HTTP/2 or proxies.

The default `run` modes are `rate concurrency`. A rate-only experiment capped at 100 requests/s will naturally show approximately 100 requests/s for both healthy panels. **This does not measure their maximum throughput.** Concurrent-client results provide observed throughput without that request-rate cap. Increase VUs until saturation/latency degradation becomes visible; compare complete repetitions under the same resource budget. The fastest passing tested VU level can differ between implementations. Neither an all-passing low rate sweep nor two clients establishes peak capacity.

Each level has a separate k6 process, warmup and summary. Target order alternates and blocks are shuffled with a recorded seed. Defaults use 60-second trials and three repetitions. The two-workload rate sweep alone takes about 90 minutes; adding concurrency takes longer. For an initial validation use a separate config with short durations and fewer levels, labeled with `experiment_label`. Short validation runs do not establish capacity or reliable extreme percentiles.

The example SLO is p99 ≤ 500 ms, invalid/error responses ≤ 1%, zero dropped iterations and ≥99% completion of offered rate. The same limits apply to both targets. Drops can indicate backend overload or generator limits. Exit `0` means all runs passed; `2` means an SLO failed and reports were still written; `1` means configuration/execution failed. Overload experiments commonly return `2`.

## Startup and memory

```sh
python3 benchmarks/bench.py startup --repetitions 10 \
  --output benchmarks/results/startup-01
```

Startup times launch existing containers after confirming the published port is closed. Trials record first TCP acceptance and the first complete valid authenticated database-backed API response. Runtime launch and application migrations/initialization are included; compilation, image pulls and initial database setup happen beforehand. Dependencies remain running. A port-forwarding process may accept TCP before the backend is ready, so API readiness is the stronger metric. Polling, RTT and scheduler jitter add uncertainty. These are application restarts with warm host caches, not first-install/cold-host startup. Trials leave the panels stopped.

Memory uses sampled cgroup `memory.current`, including charged page cache. Average is weighted by elapsed time; peak is the largest observed sample and can miss shorter spikes. The default interval is 250 ms. Failed samples invalidate a run's memory result. The window includes k6 initialization and request drain; warmup has separate files. Longer steady runs make those effects small. For native processes, `memory.kind: proc` sums the RSS of configured PID trees; shared pages are counted per process. Compare the same accounting method and scope.

For a soak experiment, use one sustainable rate and a long `duration_seconds`, then inspect `memory.csv`. Compare both panels at the same successful workload. A high-water mark alone does not prove a leak.

## Visual reports

Reports automatically include `comparison.svg`, per-workload/mode SVG curves and `charts.json` with the exact grouped chart values. `report.md` embeds the overview and lists every trial. Raw summaries, sampled memory and logs remain in individual run directories. `--raw` also records the compressed k6 metric stream and adds generator I/O.

The overview uses **observed passing throughput from concurrent-client runs**. Fixed-rate rows show **per-endpoint p99** instead of misleading rate-capped throughput bars. Overall p99 and memory are compared at the highest common passing offered load. Charts retain missing/failed/incomplete measurements, distinguish memory scopes and never substitute zero. Median per-run percentiles are labeled as such; they are not a pooled percentile.

Combine matching experiments with the same dataset, deployment, SLO, duration and repetition count:

```sh
python3 benchmarks/bench.py figures \
  --load benchmarks/results/load-01 \
  --startup benchmarks/results/startup-01 \
  --output benchmarks/results/figures-01
```

If rate and concurrent-client results are in separate directories, pass the second with `--connections`. Incompatible or duplicate load trials are rejected. Check that startup measurements use the same deployment. Regenerate any existing report with `python3 benchmarks/bench.py report PATH`.

### Understanding the earlier latency gap

The short Podman validation with 1,000 users at 100 offered requests/s showed mixed-read p99 around 29–30 ms for Gowave and 6 ms for Remnawave. Endpoint submetrics reveal that this is not a uniform language/runtime advantage:

| Endpoint | Gowave p99 across the two runs | Remnawave p99 across the two runs |
|---|---:|---:|
| Dashboard stats | 32–34.01 ms | 4.01–5 ms |
| User list | 3.5 ms | 7 ms |
| User detail | 1.01–2 ms | 3 ms |

`reads` includes 20% dashboard requests, so that slower endpoint dominates its tail. Gowave's `backend-go/internal/system/handler.go:GetStats` loads every full user row with GORM and counts statuses in Go. [Upstream's repository](https://github.com/remnawave/backend/blob/010b365ab1fabea01192b5e6ade4e98e66ee1dbd/src/modules/users/repositories/users.repository.ts) aggregates status counts in PostgreSQL and returns those counts. Its [stats query handler](https://github.com/remnawave/backend/blob/010b365ab1fabea01192b5e6ade4e98e66ee1dbd/src/modules/users/queries/get-short-user-stats/get-short-user-stats.handler.ts) calls those repository methods directly; caching is not established as the explanation for this path.

Fetching and decoding complete rows adds database transfer, allocations and application work that aggregation avoids. The aggregation path was also verified in the compiled Remnawave 3.4.5 image used for these measurements. This is a concrete implementation difference and a strong explanation for the endpoint-specific gap; a profile or controlled optimization experiment is needed to quantify its contribution. The results do not prove that TS is generally five times faster than Go. The new per-endpoint charts and isolated `stats`/`user-list`/`user-detail` workloads make this distinction visible without changing either backend.

## Existing deployments and fair comparisons

For external targets, copy `config/example.json` to `config/local.json` and supply credentials through its environment-variable names. Targets are HTTP(S) origins, without `/api`. Tokens or an existing administrator's login credentials are supported. Administrator requests include Remnawave's browser-client header; direct requests also include the proxy/HTTPS headers required by upstream production middleware. Use isolated targets or equivalent real proxies.

Record exact image/source versions, runtime versions, CPU/RAM limits, database settings, API instances and pool settings. Default Gowave pool size is 50; TS's Prisma limit is 50 per process, so total pool capacity need not be identical. Match configuration deliberately. Keep the generator off backend cores where possible and watch its CPU, RAM, descriptors, ports and network. Equalize background work, subscription templates/history retention and cache conditions; restore equivalent snapshots if mutation/history growth matters.

These fixtures have no real nodes. Results do not establish node RPC, traffic ingestion, outbox drain, browser rendering or proxy/VPN data-plane throughput. Test those separately with appropriate completion/backlog measurements.

## Checks and shutdown

```sh
python3 benchmarks/bench.py check
python3 benchmarks/bench.py check --server /absolute/path/to/prebuilt-gowave --k6 k6 --full
python3 benchmarks/bench.py down
```

The optional live check uses a temporary SQLite instance and verifies fixture preparation, workloads, memory, startup and chart export; it is not a comparative performance result. `down` removes only lab containers/networks and retains database volumes and private credentials. After `up`, re-run `configure --replace-config` because container IDs can change. The orchestrator never deletes database volumes automatically.
