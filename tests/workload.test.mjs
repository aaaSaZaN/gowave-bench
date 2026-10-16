import assert from 'node:assert/strict'
import { test } from 'node:test'
import { mixes, operation, requestSpec, validResponse } from '../k6/workload.mjs'

const user = { username: 'bench_00000001', shortUuid: 'short' }
const manifest = { users: [user], addresses: ['bench-0000.example.invalid', 'bench-0001.example.invalid'] }

test('mixes have exactly one request per iteration and deterministic operation weights', () => {
    for (const [workload, mix] of Object.entries(mixes)) {
        assert.deepEqual(Array.from({ length: mix.length }, (_, i) => operation(workload, i)), mix)
    }
    const update = requestSpec('update', user, 100, manifest)
    assert.equal(update.path, '/api/users')
    assert.equal(update.body.username, user.username)
})

test('HTML, redirects, empty configs and partial host lists cannot pass', () => {
    for (const action of ['json', 'mihomo', 'singbox']) {
        const spec = requestSpec(action, user, 1, manifest)
        for (const body of ['<html>login</html>', '{}', '[]', 'proxies: []', manifest.addresses[0]]) {
            assert.equal(validResponse(spec, 200, body, user, manifest), false)
        }
        assert.equal(validResponse(spec, 302, '{}', user, manifest), false)
    }
})

test('all subscription hosts must be present in a response of the requested format', () => {
    const json = JSON.stringify(manifest.addresses.map(address => ({ address })))
    assert.equal(validResponse({ action: 'json' }, 200, json, user, manifest), true)
    const singbox = JSON.stringify({ outbounds: manifest.addresses.map(server => ({ server })) })
    assert.equal(validResponse({ action: 'singbox' }, 200, singbox, user, manifest), true)
    const yaml = `proxies:\n${manifest.addresses.map(server => `  - server: ${server}`).join('\n')}`
    assert.equal(validResponse({ action: 'mihomo' }, 200, yaml, user, manifest), true)
})

test('successful writes must echo the requested description and user', () => {
    const spec = requestSpec('update', user, 1, manifest)
    assert.equal(validResponse(spec, 200, JSON.stringify({ response: { ...user, ...spec.body } }), user, manifest), true)
    assert.equal(validResponse(spec, 200, JSON.stringify({ response: user }), user, manifest), false)
})

test('list and detail checks detect stale fixtures and generic success envelopes', () => {
    assert.equal(validResponse({ action: 'detail' }, 200, '{"response":{}}', user, manifest), false)
    assert.equal(validResponse({ action: 'list' }, 200, '{"response":{"users":[],"total":1}}', user, manifest), false)
    assert.equal(validResponse({ action: 'detail' }, 200, JSON.stringify({ response: user }), user, manifest), true)
})
