import api from './index'

/** 文档列表（分页 + 筛选：scope / doc_type / index_status / status / keyword）。 */
export function getDocuments(params) {
  return api.get('/documents/get_documents', { params })
}

/**
 * 上传文档（multipart）。
 *
 * ``formData`` 里必须同时带上**文件**与后端的五个表单字段
 * （``scope`` / ``tenant_id`` / ``doc_type`` / ``doc_name`` / ``version``）——
 * 后端是用 ``Form()`` 声明的，走 query 参数一律收不到（参考前端里有两种错误示范）。
 *
 * 这里**不要手动设 ``Content-Type``**：交给 axios 自己加，它才会带上 boundary；
 * 手写 ``'multipart/form-data'`` 少了 boundary，后端会解析失败。
 */
export function uploadDocument(formData) {
  return api.post('/documents/upload', formData)
}

/** 重新索引指定文档（会新建一条索引任务并入队）。 */
export function reindexDocument(documentId) {
  return api.post('/documents/index_document', { document_id: documentId })
}

/** 软删文档并清掉它的向量。 */
export function deleteDocument(id) {
  return api.delete(`/documents/delete_document/${id}`)
}

/** 查索引任务状态（上传后轮询它，直到不再是 QUEUED/RUNNING）。 */
export function getIndexTask(taskId) {
  return api.get(`/documents/get_index_task/${taskId}`)
}
