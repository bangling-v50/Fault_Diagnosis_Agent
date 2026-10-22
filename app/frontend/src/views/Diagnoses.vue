<script setup>
import { ElMessage } from 'element-plus'
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'

import { getDiagnoses } from '../api/diagnosis'
import { getDevices } from '../api/devices'
import { extractError } from '../api/index'

const router = useRouter()

// 与后端 diagnosis_tasks.status 的 CHECK 一致
const STATUS_OPTIONS = [
  { label: '排队中', value: 'QUEUED' },
  { label: '执行中', value: 'RUNNING' },
  { label: '已完成', value: 'SUCCEEDED' },
  { label: '失败', value: 'FAILED' },
  { label: '已取消', value: 'CANCELLED' },
]
const PHASE_LABELS = {
  collect_knowledge: '检索知识库',
  collect_alert_history: '查历史告警',
  collect_work_orders: '查历史工单',
  write_report: '生成报告',
  evaluate_report: '评审报告',
}
const TRIGGER_LABELS = { auto: '自动', manual: '手动', retry: '重试' }

function statusLabel(status) {
  return STATUS_OPTIONS.find((item) => item.value === status)?.label || status
}
function statusType(status) {
  return {
    QUEUED: 'info',
    RUNNING: 'warning',
    SUCCEEDED: 'success',
    FAILED: 'danger',
    CANCELLED: 'info',
  }[status] || 'info'
}
function phaseLabel(phase) {
  return PHASE_LABELS[phase] || phase || '—'
}
function formatTime(t) {
  return t ? new Date(t).toLocaleString() : ''
}

const loading = ref(false)
const rows = ref([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const statusFilter = ref('')
const deviceFilter = ref(null)
const devices = ref([])

onMounted(async () => {
  fetchData()
  try {
    devices.value = (await getDevices({ page: 1, page_size: 100 })).data.data.items
  } catch {
    /* 设备列表拉不到不影响主列表 */
  }
})

async function fetchData() {
  loading.value = true
  try {
    const res = await getDiagnoses({
      page: page.value,
      page_size: pageSize.value,
      status: statusFilter.value || undefined,
      device_id: deviceFilter.value || undefined,
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

function openDetail(row) {
  router.push(`/diagnoses/${row.id}`)
}
</script>

<template>
  <div class="page">
    <div class="page-header">
      <div>
        <h3>故障诊断</h3>
        <p class="subtitle">设备告警触发的自动诊断：谁在诊断、走到哪一步、结论是什么</p>
      </div>
      <el-button @click="fetchData">刷新</el-button>
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
          v-model="deviceFilter"
          placeholder="设备"
          clearable
          filterable
          style="width: 200px"
          @change="resetAndFetch"
        >
          <el-option
            v-for="item in devices"
            :key="item.id"
            :label="item.device_name ? `${item.device_code}（${item.device_name}）` : item.device_code"
            :value="item.id"
          />
        </el-select>
        <el-button @click="resetAndFetch">查询</el-button>
      </div>

      <el-table :data="rows" stripe v-loading="loading" style="width: 100%">
        <el-table-column prop="id" label="任务" width="80" />
        <el-table-column label="设备" min-width="150">
          <template #default="{ row }">{{ row.device_code || row.device_id }}</template>
        </el-table-column>
        <el-table-column label="告警码" width="120">
          <template #default="{ row }">{{ row.alert_code || row.alert_id }}</template>
        </el-table-column>
        <el-table-column label="状态" width="100">
          <template #default="{ row }">
            <el-tag :type="statusType(row.status)" size="small">{{ statusLabel(row.status) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="当前阶段" width="130">
          <template #default="{ row }">
            <span v-if="row.status === 'RUNNING' || row.status === 'QUEUED'">
              {{ phaseLabel(row.current_phase) }}
            </span>
            <span v-else class="muted">{{ phaseLabel(row.current_phase) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="报告版本" width="100">
          <template #default="{ row }">
            <span v-if="row.result_summary?.reportVersion">v{{ row.result_summary.reportVersion }}</span>
            <span v-else class="muted">—</span>
          </template>
        </el-table-column>
        <el-table-column label="触发方式" width="100">
          <template #default="{ row }">{{ TRIGGER_LABELS[row.trigger_type] || row.trigger_type }}</template>
        </el-table-column>
        <el-table-column label="创建时间" width="170">
          <template #default="{ row }">{{ formatTime(row.created_at) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="90" fixed="right">
          <template #default="{ row }">
            <el-button text type="primary" size="small" @click="openDetail(row)">查看</el-button>
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
</style>
