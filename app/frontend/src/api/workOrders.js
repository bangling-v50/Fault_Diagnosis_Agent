import api from './index'

/** 工单列表（分页 + 筛选：status / priority / device_id / assigned_to / keyword）。 */
export function getWorkOrders(params) {
  return api.get('/work_orders', { params })
}

/** 工单详情。 */
export function getWorkOrder(id) {
  return api.get(`/work_orders/${id}`)
}

/**
 * 建单。**建议带上 alert_id**——它是工单与故障码之间唯一的纽带，不带的话这张工单
 * 不会被后续诊断当成历史维修经验检索到。
 */
export function createWorkOrder(payload) {
  return api.post('/work_orders', payload)
}

/** 局部更新：派单 / 改优先级 / 状态流转 / 结案（结案必须同时带根因与方案）。 */
export function updateWorkOrder(id, payload) {
  return api.put(`/work_orders/${id}`, payload)
}
