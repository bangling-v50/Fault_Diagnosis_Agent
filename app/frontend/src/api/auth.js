import api from './index'

export function login(username, password) {
  return api.post('/auth/login', { username, password })
}

export function logout() {
  return api.post('/auth/logout')
}

export function getCurrentUser() {
  return api.get('/auth/me')
}

/** 自助修改本人资料（real_name / email） */
export function updateProfile(payload) {
  return api.put('/auth/me', payload)
}
