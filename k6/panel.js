import http from 'k6/http'
import exec from 'k6/execution'
import { SharedArray } from 'k6/data'
import { Counter, Rate, Trend } from 'k6/metrics'
import { hash, mixes, operation, requestSpec, validResponse } from './workload.mjs'

const manifestPath = __ENV.BENCH_MANIFEST
const fixture = new SharedArray('fixture', () => [JSON.parse(open(manifestPath))])[0]
const workload = __ENV.BENCH_WORKLOAD || 'reads'
if (!mixes[workload]) throw new Error(`Unknown workload: ${workload}`)
const mode = __ENV.BENCH_MODE || 'rate'
if (!['rate', 'concurrency', 'churn', 'smoke'].includes(mode)) throw new Error(`Unknown mode: ${mode}`)
const duration = `${__ENV.BENCH_DURATION || 60}s`
const timeout = `${__ENV.BENCH_TIMEOUT || 10}s`
const successes = new Counter('bench_successes')
const requests = new Counter('bench_requests')
const errors = new Rate('bench_errors')
const latency = new Trend('bench_latency', true)
const serviceLatency = new Trend('bench_service_latency', true)
const connectionLatency = new Trend('bench_connecting', true)
const tlsLatency = new Trend('bench_tls', true)
const characters = new Counter('bench_response_characters')
const scenario = mode === 'rate' ? {
    executor: 'constant-arrival-rate', rate: Number(__ENV.BENCH_LEVEL || 50), timeUnit: '1s',
    duration, preAllocatedVUs: Number(__ENV.BENCH_PREALLOCATED || 256),
    maxVUs: Number(__ENV.BENCH_MAX_VUS || 1024)
} : mode === 'smoke' ? {
    executor: 'shared-iterations', vus: 1, iterations: mixes[workload].length, maxDuration: '5m'
} : {
    executor: 'constant-vus', vus: Number(__ENV.BENCH_LEVEL || 10), duration
}

export const options = {
    scenarios: { load: { ...scenario, gracefulStop: `${Number(__ENV.BENCH_TIMEOUT || 10) + 5}s` } },
    noConnectionReuse: mode === 'churn',
    discardResponseBodies: false,
    maxRedirects: 0,
    summaryTrendStats: ['avg', 'min', 'med', 'p(90)', 'p(95)', 'p(99)', 'p(99.9)', 'max', 'count'],
    systemTags: ['status', 'method', 'name', 'scenario', 'expected_response', 'error_code', 'proto'],
    thresholds: {
        bench_errors: [`rate<=${__ENV.BENCH_MAX_ERROR || 0.01}`],
        bench_latency: [`p(99)<=${__ENV.BENCH_P99 || 500}`],
        bench_requests: ['count>0'],
        ...(mode === 'rate' ? { dropped_iterations: ['count==0'] } : {}),
        // Materialize per-operation percentiles in summary.json without imposing extra SLOs.
        ...Object.fromEntries([...new Set(mixes[workload])].map(action => [
            `bench_latency{action:${action}}`, ['p(99)>=0']
        ]))
    }
}

const headers = {
    'Authorization': `Bearer ${__ENV.BENCH_TOKEN}`,
    'Content-Type': 'application/json',
    'X-Remnawave-Client-Type': 'browser',
    'User-Agent': 'Gowave-comparison-benchmark/1.0',
    // Direct isolated-backend tests satisfy upstream's production proxy middleware.
    'X-Forwarded-For': '192.0.2.10',
    'X-Forwarded-Proto': 'https'
}

export default function () {
    const iteration = exec.scenario.iterationInTest
    const action = operation(workload, iteration)
    const user = fixture.users[hash(iteration + 1) % fixture.users.length]
    const spec = requestSpec(action, user, iteration, fixture)
    const started = Date.now()
    const response = http.request(spec.method, __ENV.BENCH_URL + spec.path,
        spec.body ? JSON.stringify(spec.body) : null,
        { headers, timeout, tags: { name: action, action }, responseType: 'text' })
    const elapsed = Date.now() - started
    const ok = validResponse(spec, response.status, response.body, user, fixture)
    const tags = { action }
    requests.add(1, tags)
    successes.add(ok ? 1 : 0, tags)
    errors.add(!ok, tags)
    latency.add(elapsed, tags)
    serviceLatency.add(response.timings.duration, tags)
    connectionLatency.add(response.timings.connecting, tags)
    tlsLatency.add(response.timings.tls_handshaking, tags)
    characters.add(response.body ? response.body.length : 0, tags)
}

export function handleSummary(data) {
    return {
        [__ENV.BENCH_SUMMARY || 'summary.json']: JSON.stringify(data, null, 2),
        stdout: `requests=${data.metrics.bench_requests?.values.count || 0} ` +
            `errors=${data.metrics.bench_errors?.values.rate ?? 'n/a'} ` +
            `p99=${data.metrics.bench_latency?.values['p(99)'] ?? 'n/a'}ms\n`
    }
}
