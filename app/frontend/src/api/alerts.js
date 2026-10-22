import api from './index'

/**
 * 告警列表（分页 + 筛选：status / alert_level / device_id / tenant_id / keyword）。
 *
 * ⚠️ 后端这两个读端点是动词式路径（历史遗留），与 devices/diagnoses 的 REST 风格不同。
 */
export function getAlerts(params) {
  return api.get('/alerts/get_alerts_list', { params })
}

/** 告警详情。 */
export function getAlert(id) {
  return api.get(`/alerts/get_alert/${id}`)
}
