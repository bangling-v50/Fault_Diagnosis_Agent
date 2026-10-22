<script setup>
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'

import { extractError } from '../api/index'
import {
  deleteDocument,
  getDocuments,
  getIndexTask,
  reindexDocument,
  uploadDocument,
} from '../api/knowledge'
import { getTenants } from '../api/tenants'
import { getUser } from '../utils/token'

const DOC_TYPES = [
  { label: '设备手册', value: 'manual' },
  { label: '标准作业流程 SOP', value: 'sop' },
  { label: '维修案例', value: 'diagnostic_case' },
  { label: '常见问题 FAQ', value: 'faq' },
]
const INDEX_STATUS = {
  pending: { label: '等待索引', type: 'info' },
  indexing: { label: '索引中', type: 'warning' },
  indexed: { label: '已索引', type: 'success' },
  failed: { label: '索引失败', type: 'danger' },
}
// 上传后轮询索引任务的间隔：抽取 + 切分 + 多次嵌入通常是秒级
const POLL_INTERVAL_MS = 2000

const me = ref(getUser())
const canManage = computed(() => ['engineer', 'admin'].includes(me.value?.role))

function docTypeLabel(value) {
  return DOC_TYPES.find((item) => item.value === value)?.label || value
}
function indexStatusLabel(status) {
  return INDEX_STATUS[status]?.label || status
}
function indexStatusType(status) {
  return INDEX_STATUS[status]?.type || 'info'
}
function formatSize(bytes) {
  if (!bytes) return '—'
  return bytes < 1024 * 1024 ? `${(bytes / 1024).toFixed(0)} KB` : `${(bytes / 1024 / 1024).toFixed(1)} MB`
}
function formatTime(t) {
  return t ? new Date(t).toLocaleString() : '—'
}

const loading = ref(false)
const saving = ref(false)
const rows = ref([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const scopeFilter = ref('')
const typeFilter = ref('')
const statusFilter = ref('')
const keyword = ref('')
const tenants = ref([])

const uploadVisible = ref(false)
const selectedFile = ref(null)
const fileInput = ref(null)
const uploadForm = reactive({ scope: 'tenant', tenant_id: null, doc_type: 'manual', doc_name: '' })
const pollingTask = ref(null)

onMounted(async () => {
  fetchData()
  if (canManage.value) {
    try {
      tenants.value = (await getTenants({ page: 1, page_size: 100 })).data.data.items
    } catch {
      /* 忽略 */
    }
  }
})
// 组件卸载时必须停表，否则离开页面后仍在打接口
onBeforeUnmount(stopPolling)

async function fetchData() {
  loading.value = true
  try {
    const res = await getDocuments({
      page: page.value,
      page_size: pageSize.value,
      scope: scopeFilter.value || undefined,
      doc_type: typeFilter.value || undefined,
      index_status: statusFilter.value || undefined,
      keyword: keyword.value || undefined,
    })
    rows.value = res.data.data.items
    total.value = res.data.data.total
  } catch (e) {
    ElMessage.error(extractError(e))
  } finally {
    loading.value = false
  }
}

function resetAndFetch() {
  page.value = 1
  fetchData()
}

// ---------- 上传 ----------

function openUpload() {
  selectedFile.value = null
  Object.assign(uploadForm, { scope: 'tenant', tenant_id: null, doc_type: 'manual', doc_name: '' })
  if (fileInput.value) fileInput.value.value = '' // 允许重选同一个文件
  uploadVisible.value = true
}

function onFileChange(event) {
  const file = event.target.files?.[0]
  selectedFile.value = file || null
  if (file && !uploadForm.doc_name) {
    uploadForm.doc_name = file.name
  }
}

async function handleUpload() {
  if (!selectedFile.value) {
    ElMessage.warning('请先选择文件')
    return
  }
  if (uploadForm.scope === 'tenant' && !uploadForm.tenant_id) {
    ElMessage.warning('租户范围内的文档必须选择租户')
    return
  }
  saving.value = true
  try {
    // ⚠️ 后端这五个字段是用 Form() 声明的，必须走 FormData——
    // 放 query 参数后端一个都收不到（参考前端里有这个错误示范）
    const formData = new FormData()
    formData.append('file', selectedFile.value)
    formData.append('scope', uploadForm.scope)
    if (uploadForm.scope === 'tenant') {
      formData.append('tenant_id', String(uploadForm.tenant_id))
    }
    formData.append('doc_type', uploadForm.doc_type)
    if (uploadForm.doc_name) formData.append('doc_name', uploadForm.doc_name)

    const res = await uploadDocument(formData)
    const { document: doc, index_task: task } = res.data.data
    ElMessage.success('已上传，正在索引…')
    uploadVisible.value = false
    fetchData()
    // 上传即自动索引，这里跟着任务状态轮询，让用户看见"索引中 → 已索引"
    startPolling(task.id, doc.id)
  } catch (e) {
    ElMessage.error(extractError(e))
  } finally {
    saving.value = false
  }
}

function startPolling(taskId, documentId) {
  stopPolling()
  pollingTask.value = { taskId, documentId, timer: null }
  const tick = async () => {
    try {
      const res = await getIndexTask(taskId)
      const task = res.data.data
      if (!['QUEUED', 'RUNNING'].includes(task.status)) {
        stopPolling()
        if (task.status === 'SUCCEEDED') {
          ElMessage.success('索引完成')
        } else {
          ElMessage.error(`索引失败：${task.failure_reason || '未记录原因'}`)
        }
        fetchData()
      }
    } catch {
      stopPolling() // 查不到就停表，别无限打
    }
  }
  tick()
  pollingTask.value.timer = setInterval(tick, POLL_INTERVAL_MS)
}

function stopPolling() {
  if (pollingTask.value?.timer) clearInterval(pollingTask.value.timer)
  pollingTask.value = null
}

// ---------- 行操作 ----------

async function handleReindex(row) {
  saving.value = true
  try {
    const res = await reindexDocument(row.id)
    ElMessage.success('已提交重新索引')
    fetchData()
    startPolling(res.data.data.index_task.id, row.id)
  } catch (e) {
    ElMessage.error(extractError(e))
  } finally {
    saving.value = false
  }
}

async function handleDelete(row) {
  try {
    await ElMessageBox.confirm(
      `删除「${row.doc_name || row.filename}」？会同时清掉它的向量片段，之后检索不再命中。`,
      '删除文档',
      { type: 'warning' },
    )
  } catch {
    return // 用户取消
  }
  try {
    await deleteDocument(row.id)
    ElMessage.success('文档已删除')
    fetchData()
  } catch (e) {
    ElMessage.error(extractError(e))
  }
}
</script>

<template>
  <div class="page">
    <div class="page-header">
      <div>
        <h3>知识库</h3>
        <p class="subtitle">
          诊断时检索的语料：手册 / SOP / 维修案例。上传后自动抽取、切分、嵌入并写入向量库
        </p>
      </div>
      <div class="header-actions">
        <el-button @click="fetchData">刷新</el-button>
        <el-button v-if="canManage" type="primary" @click="openUpload">上传文档</el-button>
      </div>
    </div>

    <el-card shadow="never">
      <div class="filters">
        <el-select v-model="scopeFilter" placeholder="范围" clearable style="width: 130px" @change="resetAndFetch">
          <el-option label="全局" value="global" />
          <el-option label="租户" value="tenant" />
        </el-select>
        <el-select v-model="typeFilter" placeholder="类型" clearable style="width: 140px" @change="resetAndFetch">
          <el-option v-for="item in DOC_TYPES" :key="item.value" :label="item.label" :value="item.value" />
        </el-select>
        <el-select v-model="statusFilter" placeholder="索引状态" clearable style="width: 140px" @change="resetAndFetch">
          <el-option v-for="(item, key) in INDEX_STATUS" :key="key" :label="item.label" :value="key" />
        </el-select>
        <el-input v-model="keyword" placeholder="搜索文档名 / 文件名" clearable style="width: 200px"
                  @keyup.enter="resetAndFetch" />
        <el-button @click="resetAndFetch">查询</el-button>
      </div>

      <el-table :data="rows" stripe v-loading="loading" style="width: 100%">
        <el-table-column label="文档" min-width="220">
          <template #default="{ row }">
            <div>{{ row.doc_name || row.filename }}</div>
            <div class="sub-line">{{ row.filename }} ｜ {{ formatSize(row.size_bytes) }}</div>
          </template>
        </el-table-column>
        <el-table-column label="类型" width="130">
          <template #default="{ row }">{{ docTypeLabel(row.doc_type) }}</template>
        </el-table-column>
        <el-table-column label="范围" width="100">
          <template #default="{ row }">
            <el-tag size="small" type="info">
              {{ row.scope === 'global' ? '全局' : `租户 #${row.tenant_id}` }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="索引状态" width="110">
          <template #default="{ row }">
            <el-tag :type="indexStatusType(row.index_status)" size="small">
              {{ indexStatusLabel(row.index_status) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="来源" width="110">
          <template #default="{ row }">
            {{ { upload: '人工上传', diagnosis_case: '案例沉淀', system: '系统生成' }[row.source_type] || row.source_type }}
          </template>
        </el-table-column>
        <el-table-column label="上传时间" width="170">
          <template #default="{ row }">{{ formatTime(row.created_at) }}</template>
        </el-table-column>
        <el-table-column v-if="canManage" label="操作" width="150" fixed="right">
          <template #default="{ row }">
            <el-button text type="primary" size="small" :loading="saving" @click="handleReindex(row)">
              重新索引
            </el-button>
            <el-button text type="danger" size="small" @click="handleDelete(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>

      <div class="pagination-wrap">
        <el-pagination
          v-model:current-page="page"
          :page-size="pageSize"
          :total="total"
          layout="total, prev, pager, next"
          @current-change="fetchData"
        />
      </div>
    </el-card>

    <el-dialog v-model="uploadVisible" title="上传文档" width="560px">
      <el-form :model="uploadForm" label-width="90px">
        <el-form-item label="文件" required>
          <input
            ref="fileInput"
            class="file-input"
            type="file"
            accept=".md,.txt,.pdf"
            @change="onFileChange"
          />
          <p class="dialog-hint">支持 .md / .txt / .pdf，单文件上限 20MB</p>
        </el-form-item>
        <el-form-item label="可见范围">
          <el-radio-group v-model="uploadForm.scope">
            <el-radio value="tenant">仅本租户</el-radio>
            <el-radio value="global">所有租户共享</el-radio>
          </el-radio-group>
        </el-form-item>
        <el-form-item v-if="uploadForm.scope === 'tenant'" label="租户" required>
          <el-select v-model="uploadForm.tenant_id" placeholder="选择租户" filterable style="width: 100%">
            <el-option
              v-for="item in tenants"
              :key="item.id"
              :label="`${item.tenant_name}（${item.tenant_code}）`"
              :value="item.id"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="文档类型">
          <el-select v-model="uploadForm.doc_type" style="width: 100%">
            <el-option v-for="item in DOC_TYPES" :key="item.value" :label="item.label" :value="item.value" />
          </el-select>
        </el-form-item>
        <el-form-item label="文档名称">
          <el-input v-model="uploadForm.doc_name" placeholder="留空则用文件名" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="uploadVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="handleUpload">上传并索引</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.page { max-width: 1250px; }
.page-header { margin-bottom: 20px; display: flex; justify-content: space-between; align-items: flex-start; }
.page-header h3 { font-size: 20px; font-weight: 600; }
.subtitle { color: #909399; font-size: 13px; margin-top: 6px; }
.header-actions { display: flex; gap: 10px; }
.filters { display: flex; gap: 12px; margin-bottom: 16px; flex-wrap: wrap; }
.pagination-wrap { display: flex; justify-content: flex-end; margin-top: 16px; }
.sub-line { color: #909399; font-size: 12px; margin-top: 2px; }
.file-input { color: #606266; font-size: 13px; }
.dialog-hint { color: #909399; font-size: 12px; margin: 6px 0 0; }
</style>
