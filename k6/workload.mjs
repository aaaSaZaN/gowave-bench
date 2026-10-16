// Pure functions shared with the offline tests. One iteration is one HTTP request.
export const mixes = {
    reads: ['list', 'list', 'list', 'detail', 'detail', 'detail', 'detail', 'stats', 'stats', 'nodes'],
    subscriptions: ['json', 'json', 'json', 'json', 'mihomo', 'mihomo', 'mihomo', 'singbox', 'singbox', 'singbox'],
    writes: ['update'],
    stats: ['stats'],
    'user-list': ['list'],
    'user-detail': ['detail'],
    mixed: ['list', 'detail', 'detail', 'stats', 'json', 'json', 'mihomo', 'singbox', 'update', 'update']
}

export function hash(n) {
    n = Math.imul(n ^ (n >>> 16), 0x45d9f3b)
    n = Math.imul(n ^ (n >>> 16), 0x45d9f3b)
    return (n ^ (n >>> 16)) >>> 0
}

export function operation(workload, iteration) {
    const mix = mixes[workload]
    if (!mix) throw new Error(`Unknown workload: ${workload}`)
    return mix[iteration % mix.length]
}

export function requestSpec(action, user, iteration, manifest) {
    const name = encodeURIComponent(user.username)
    if (action === 'list') {
        const start = hash(iteration) % Math.max(1, manifest.users.length - 24)
        return { method: 'GET', path: `/api/users?start=${start}&size=25`, action }
    }
    if (action === 'detail') return { method: 'GET', path: `/api/users/by-username/${name}`, action }
    if (action === 'stats') return { method: 'GET', path: '/api/system/stats', action }
    if (action === 'nodes') return { method: 'GET', path: '/api/nodes', action }
    if (action === 'update') {
        return {
            method: 'PATCH', path: '/api/users', action,
            body: { username: user.username, description: `benchmark-${iteration % 1000000}` }
        }
    }
    return { method: 'GET', path: `/api/sub/${encodeURIComponent(user.shortUuid)}/${action}`, action }
}

export function validResponse(spec, status, body, user, manifest) {
    if (status !== 200 || typeof body !== 'string') return false
    if (['json', 'mihomo', 'singbox'].includes(spec.action)) {
        // Every host must be rendered; a login page, empty config or block response fails.
        if (!manifest.addresses.every(address => body.includes(address))) return false
        if (spec.action === 'mihomo') return /(?:^|\n)proxies:/.test(body)
        try {
            const value = JSON.parse(body)
            return spec.action === 'json' ? Array.isArray(value) && value.length > 0
                : value !== null && Array.isArray(value.outbounds)
        } catch (_) { return false }
    }
    try {
        const value = JSON.parse(body).response
        if (spec.action === 'list') return Array.isArray(value.users) && value.users.length > 0 && value.total >= manifest.users.length
        if (spec.action === 'detail') return value.username === user.username && value.shortUuid === user.shortUuid
        if (spec.action === 'update') return value.username === user.username && value.description === spec.body.description
        if (spec.action === 'stats') return value.users.totalUsers >= manifest.users.length
        if (spec.action === 'nodes') return Array.isArray(value) || Array.isArray(value.nodes)
    } catch (_) { return false }
    return false
}
