import api from './index'

/** 诊断任务列表（分页 + 可选筛选：tenant_id / device_id / alert_id / status）。 */
export function getDiagnoses(params) {
  return api.get('/diagnoses', { params })
}

/** 一次诊断的完整过程：{task, steps, evidence, reports, links}。 */
export function getDiagnosis(id) {
  return api.get(`/diagnoses/${id}`)
}
