<script setup>
import { ElMessage } from 'element-plus'
import { computed, onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'

import { getAlerts } from '../api/alerts'
import { getDevices } from '../api/devices'
import { extractError } from '../api/index'
import { createWorkOrder, getWorkOrders } from '../api/workOrders'
import { getUser } from '../utils/token'

const router = useRouter()

const STATUS_OPTIONS = [
  { label: '待受理', value: 'OPEN' },
  { label: '已派单', value: 'ASSIGNED' },
  { label: '处理中', value: 'IN_PROGRESS' },
  { label: '已解决', value: 'RESOLVED' },
  { label: '已关闭', value: 'CLOSED' },
]
const PRIORITY_OPTIONS = [
  { label: '低', value: 'low' },
  { label: '中', value: 'medium' },
  { label: '高', value: 'high' },
  { label: '紧急', value: 'urgent' },
]
const STATUS_TYPES = { OPEN: 'info', ASSIGNED: 'warning', IN_PROGRESS: 'warning', RESOLVED: 'success', CLOSED: 'success' }
const PRIORITY_TYPES = { low: 'info', medium: 'info', high: 'warning', urgent: 'danger' }

const me = ref(getUser())
const canManage = computed(() => ['engineer', 'admin'].includes(me.value?.role))

function statusLabel(status) {
  return STATUS_OPTIONS.find((item) => item.value === status)?.label || status
}
function priorityLabel(priority) {
  return PRIORITY_OPTIONS.find((item) => item.value === priority)?.label || priority
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
const statusFilter = ref('')
const priorityFilter = ref('')
const keyword = ref('')

const devices = ref([])
const createVisible = ref(false)
// 建单时按所选设备拉它的告警，供"关联告警"下拉使用
const alertOptions = ref([])
const alertLoading = ref(false)
const createForm = reactive({
  device_id: null, title: '', description: '', priority: 'medium', alert_id: null,
})

onMounted(async () => {
  fetchData()
  try {
    devices.value = (await getDevices({ page: 1, page_size: 100 })).data.data.items
  } catch {
    /* 忽略 */
  }
})

async function fetchData() {
  loading.value = true
  try {
    const res = await getWorkOrders({
      page: page.value,
      page_size: pageSize.value,
      status: statusFilter.value || undefined,
      priority: priorityFilter.value || undefined,
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

function openCreate() {
  Object.assign(createForm, {
    device_id: null, title: '', description: '', priority: 'medium', alert_id: null,
  })
  alertOptions.value = []
  createVisible.value = true
}

async function onDeviceChange(deviceId) {
  createForm.alert_id = null
  alertOptions.value = []
  if (!deviceId) return
  alertLoading.value = true
  try {
    alertOptions.value = (await getAlerts({ page: 1, page_size: 50, device_id: deviceId }))
      .data.data.items
  } catch {
    /* 拉不到就只让用户不关联告警 */
  } finally {
    alertLoading.value = false
  }
}

async function handleCreate() {
  if (!createForm.device_id || !createForm.title) {
    ElMessage.warning('请选择设备并填写标题')
    return
  }
  saving.value = true
  try {
    const res = await createWorkOrder({
      device_id: createForm.device_id,
      title: createForm.title,
      description: createForm.description || undefined,
      priority: createForm.priority,
      alert_id: createForm.alert_id || undefined,
    })
    ElMessage.success('工单已创建')
    createVisible.value = false
    fetchData()
    // 建完直接进详情，方便接着派单/结案
    router.push(`/work-orders/${res.data.data.id}`)
  } catch (e) {
    ElMessage.error(extractError(e))
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <div class="page">
    <div class="page-header">
      <div>
        <h3>维修工单</h3>
        <p class="subtitle">诊断给出"该怎么修"，工单记录"修了没有、修得对不对"</p>
      </div>
      <el-button v-if="canManage" type="primary" @click="openCreate">新建工单</el-button>
    </div>

    <el-card shadow="never">
      <div class="filters">
        <el-select
          v-model="statusFilter"
          placeholder="状态"
          clearable
          style="width: 130px"
          @change="resetAndFetch"
        >
          <el-option v-for="item in STATUS_OPTIONS" :key="item.value" :label="item.label" :value="item.value" />
        </el-select>
        <el-select
          v-model="priorityFilter"
          placeholder="优先级"
          clearable
          style="width: 130px"
          @change="resetAndFetch"
        >
          <el-option v-for="item in PRIORITY_OPTIONS" :key="item.value" :label="item.label" :value="item.value" />
        </el-select>
        <el-input
          v-model="keyword"
          placeholder="搜索标题 / 描述"
          clearable
          style="width: 220px"
          @keyup.enter="resetAndFetch"
        />
        <el-button @click="resetAndFetch">查询</el-button>
      </div>

      <el-table :data="rows" stripe v-loading="loading" style="width: 100%">
        <el-table-column prop="id" label="工单" width="80" />
        <el-table-column prop="title" label="标题" min-width="200" />
        <el-table-column label="设备" min-width="150">
          <template #default="{ row }">
            {{ row.device_code || `设备 #${row.device_id}` }}
          </template>
        </el-table-column>
        <el-table-column label="优先级" width="90">
          <template #default="{ row }">
            <el-tag :type="PRIORITY_TYPES[row.priority] || 'info'" size="small">
              {{ priorityLabel(row.priority) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="100">
          <template #default="{ row }">
            <el-tag :type="STATUS_TYPES[row.status] || 'info'" size="small">
              {{ statusLabel(row.status) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="处理人" width="120">
          <template #default="{ row }">
            <span v-if="row.assigned_to_name">{{ row.assigned_to_name }}</span>
            <span v-else class="muted">未派单</span>
          </template>
        </el-table-column>
        <el-table-column label="创建时间" width="170">
          <template #default="{ row }">{{ formatTime(row.created_at) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="90" fixed="right">
          <template #default="{ row }">
            <el-button text type="primary" size="small" @click="router.push(`/work-orders/${row.id}`)">
              查看
            </el-button>
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

    <el-dialog v-model="createVisible" title="新建工单" width="560px">
      <el-form :model="createForm" label-width="90px">
        <el-form-item label="设备" required>
          <el-select
            v-model="createForm.device_id"
            placeholder="选择设备"
            filterable
            style="width: 100%"
            @change="onDeviceChange"
          >
            <el-option
              v-for="item in devices"
              :key="item.id"
              :label="item.device_name ? `${item.device_code}（${item.device_name}）` : item.device_code"
              :value="item.id"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="标题" required>
          <el-input v-model="createForm.title" maxlength="255" placeholder="例如：主轴过载报警处理" />
        </el-form-item>
        <el-form-item label="关联告警">
          <el-select
            v-model="createForm.alert_id"
            placeholder="可选；选了才能被后续诊断当作历史经验"
            clearable
            filterable
            :loading="alertLoading"
            :disabled="!createForm.device_id"
            style="width: 100%"
          >
            <el-option
              v-for="item in alertOptions"
              :key="item.id"
              :label="`${item.alert_code} ${item.alert_name || ''}（${item.status}）`"
              :value="item.id"
            />
          </el-select>
          <p class="dialog-hint">
            ⚠️ 不关联告警的工单<strong>不会出现在后续诊断的历史维修记录里</strong>——
            工单与故障码之间只有「告警」这一条纽带。
          </p>
        </el-form-item>
        <el-form-item label="优先级">
          <el-select v-model="createForm.priority" style="width: 100%">
            <el-option v-for="item in PRIORITY_OPTIONS" :key="item.value" :label="item.label" :value="item.value" />
          </el-select>
        </el-form-item>
        <el-form-item label="故障描述">
          <el-input v-model="createForm.description" type="textarea" :rows="3" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="createVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="handleCreate">创建</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.page { max-width: 1250px; }
.page-header { margin-bottom: 20px; display: flex; justify-content: space-between; align-items: flex-start; }
.page-header h3 { font-size: 20px; font-weight: 600; }
.subtitle { color: #909399; font-size: 13px; margin-top: 6px; }
.filters { display: flex; gap: 12px; margin-bottom: 16px; }
.pagination-wrap { display: flex; justify-content: flex-end; margin-top: 16px; }
.muted { color: #c0c4cc; }
.dialog-hint { color: #e6a23c; font-size: 12px; line-height: 1.6; margin: 6px 0 0; }
</style>
