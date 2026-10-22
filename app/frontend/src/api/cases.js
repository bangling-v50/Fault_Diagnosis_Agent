import api from './index'

/** 案例列表（分页 + 筛选：device_model / alert_code / work_order_id）。 */
export function getCases(params) {
  return api.get('/cases', { params })
}

/**
 * 把已结案工单沉淀为维修案例。
 *
 * 后端会顺带生成一份知识文档并触发索引（回流）。**重复调用是幂等的**：
 * 响应里的 ``created=false`` 表示命中了已有案例（同一张工单，或同机型同告警码
 * 同根因同方案的案例已存在），此时返回的是那条已有记录。
 */
export function createCase(payload) {
  return api.post('/cases', payload)
}
