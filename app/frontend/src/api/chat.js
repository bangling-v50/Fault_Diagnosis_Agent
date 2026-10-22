import api from './index'

/** 我的会话列表（聊天按用户私有，只返回自己的）。 */
export function getChatSessions() {
  return api.get('/chat/sessions')
}

/**
 * 建会话。
 *
 * `device_id` 等三个 id 可选——从诊断详情页点「追问」时会把它们带上，
 * 会话就有设备上下文（工具集也会随之变化）。
 */
export function createChatSession(payload = {}) {
  return api.post('/chat/sessions', payload)
}

/** 会话详情：会话 + 消息 + 上下文占用状态。`includeTool` 为真时连 tool 行一起返回（回放用）。 */
export function getChatSession(id, params = {}) {
  return api.get(`/chat/sessions/${id}`, { params })
}

export function deleteChatSession(id) {
  return api.delete(`/chat/sessions/${id}`)
}

/** 手动提炼这个会话里的长期记忆（「结束会话并记住重点」）。 */
export function extractChatMemories(sessionId) {
  return api.post(`/chat/sessions/${sessionId}/memory:extract`)
}

/** 我的长期记忆（含已作废的）。 */
export function getMemories() {
  return api.get('/chat/memories')
}

/** 删掉一条长期记忆——用户必须能真的删掉。 */
export function deleteMemory(id) {
  return api.delete(`/chat/memories/${id}`)
}
