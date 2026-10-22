import api from './index'

export function getUsers(params) {
  return api.get('/users', { params })
}

export function createUser(payload) {
  return api.post('/users', payload)
}

export function getUser(id) {
  return api.get(`/users/${id}`)
}

export function updateUser(id, payload) {
  return api.put(`/users/${id}`, payload)
}

export function resetPassword(id, newPassword) {
  return api.post(`/users/${id}/password`, { new_password: newPassword })
}

export function disableUser(id) {
  return api.post(`/users/${id}/disable`)
}

export function enableUser(id) {
  return api.post(`/users/${id}/enable`)
}

/**
 * 可接工单的人（engineer / admin）。
 *
 * 与 `getUsers` 分开是因为权限不同：`GET /users` 对 engineer 强制只返回 operator
 * （权限矩阵里"engineer 只能建管 operator"），所以工程师拿不到同事名单来派单。
 */
export function getAssignableUsers() {
  return api.get('/users/assignable')
}
