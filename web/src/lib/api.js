const BASE = '/api'

class ApiError extends Error {
  constructor(message, status) {
    super(message)
    this.status = status
  }
}

async function request(path, { method = 'GET', body, signal } = {}) {
  const response = await fetch(BASE + path, {
    method,
    signal,
    credentials: 'same-origin',
    headers: body === undefined ? {} : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  })

  const text = await response.text()
  let payload = null
  if (text) {
    try {
      payload = JSON.parse(text)
    } catch {
      payload = { detail: text }
    }
  }

  if (!response.ok) {
    const detail = payload?.detail ?? payload?.errors?.join('; ') ?? `HTTP ${response.status}`
    throw new ApiError(typeof detail === 'string' ? detail : JSON.stringify(detail), response.status)
  }
  return payload
}

export const api = {
  me: () => request('/auth/me'),
  login: (username, password) => request('/auth/login', { method: 'POST', body: { username, password } }),
  logout: () => request('/auth/logout', { method: 'POST' }),

  status: () => request('/status'),
  interfaces: () => request('/interfaces'),
  system: () => request('/system'),

  getConfig: () => request('/config'),
  patchConfig: (patch) => request('/config', { method: 'POST', body: patch }),
  apply: () => request('/apply', { method: 'POST' }),

  servers: () => request('/servers'),
  addServer: (server) => request('/servers', { method: 'POST', body: server }),
  editServer: (id, server) => request(`/servers/${id}`, { method: 'PUT', body: server }),
  deleteServer: (id) => request(`/servers/${id}`, { method: 'DELETE' }),
  activateServer: (id) => request(`/servers/${id}/activate`, { method: 'POST' }),
  testServer: (id) => request(`/servers/${id}/test`, { method: 'POST' }),
  parseServer: (payload) => request('/servers/parse', { method: 'POST', body: payload }),
  importServers: (payload) => request('/servers/import', { method: 'POST', body: payload }),

  clients: () => request('/clients'),
  live: (points = 120) => request(`/metrics/live?points=${points}`),
  history: (range) => request(`/metrics/history?range=${range}`),

  geoCategories: () => request('/geo/categories'),
  geoUpdate: () => request('/geo/update', { method: 'POST' }),

  changePassword: (current_password, new_password) =>
    request('/auth/password', { method: 'POST', body: { current_password, new_password } }),

  adblock: () => request('/adblock'),
  adblockUpdate: () => request('/adblock/update', { method: 'POST' }),
  adblockResetStats: () => request('/adblock/reset-stats', { method: 'POST' }),

  speedtestStatus: () => request('/speedtest'),
  speedtestStart: (server_ids) => request('/speedtest', { method: 'POST', body: { server_ids } }),

  wanspeed: () => request('/wanspeed'),
  wanspeedStart: (paths, duration = 10) =>
    request('/wanspeed', { method: 'POST', body: { paths, duration } }),

  nodes: () => request('/nodes'),
  createNode: (payload) => request('/nodes', { method: 'POST', body: payload }),
  editNode: (id, payload) => request(`/nodes/${id}`, { method: 'PUT', body: payload }),
  deleteNode: (id, wipe = true) => request(`/nodes/${id}?wipe=${wipe}`, { method: 'DELETE' }),
  redeployNode: (id, credentials = {}) =>
    request(`/nodes/${id}/deploy`, { method: 'POST', body: credentials }),
  tuneNode: (id) => request(`/nodes/${id}/tune`, { method: 'POST' }),
  applyNodeProfile: (id, key) => request(`/nodes/${id}/apply`, { method: 'POST', body: { key } }),
  restartNode: (id) => request(`/nodes/${id}/restart`, { method: 'POST' }),
  forgetNodeHostKey: (id) => request(`/nodes/${id}/forget-host-key`, { method: 'POST' }),
  nodeMetrics: (id) => request(`/nodes/${id}/metrics`),

  logs: (service, tail = 300) => request(`/logs/${service}?tail=${tail}`),
  restartService: (service) => request(`/services/${service}/restart`, { method: 'POST' }),
  restartPppoe: () => request('/pppoe/restart', { method: 'POST' }),
}

export { ApiError }
