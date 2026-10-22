import api from './index'

export function getTenants(params) {
  return api.get('/tenants', { params })
}

export function createTenant(payload) {
  return api.post('/tenants', payload)
}

export function getTenant(id) {
  return api.get(`/tenants/${id}`)
}

export function updateTenant(id, payload) {
  return api.put(`/tenants/${id}`, payload)
}

export function deactivateTenant(id) {
  return api.post(`/tenants/${id}/deactivate`)
}

export function activateTenant(id) {
  return api.post(`/tenants/${id}/activate`)
}
