import api from './index'

export function getDevices(params) {
  return api.get('/devices', { params })
}

export function createDevice(payload) {
  return api.post('/devices', payload)
}

export function getDevice(id) {
  return api.get(`/devices/${id}`)
}

export function updateDevice(id, payload) {
  return api.put(`/devices/${id}`, payload)
}
