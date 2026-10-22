<script setup>
import { ElMessage } from 'element-plus'
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'

import { getAlerts } from '../api/alerts'
import { getDevices } from '../api/devices'
import { extractError } from '../api/index'

const router = useRouter()

// 与后端一致的取值（alerts.status 没有 DB 级 CHECK，这里是当前实际会用到的几个）
const STATUS_OPTIONS = [
  { label: '新告警', value: 'NEW' },
  { label: '已诊断', value: 'DIAGNOSED' },
]
const LEVEL_OPTIONS = [
  { label: '提示', value: 'info' },
  { label: '警告', value: 'warning' },
  { label: '严重', value: 'critical' },
]
const LEVEL_LABELS = { info: '提示', warning: '警告', critical: '严重' }
const STATUS_LABELS = { NEW: '新告警', DIAGNOSED: '已诊断' }

function levelType(level) {
  return { info: 'info', warning: 'warning', critical: 'danger' }[level] || 'info'
}
function statusType(status) {
  return { NEW: 'danger', DIAGNOSED: 'success' }[status] || 'info'
}
function formatTime(t) {
  return t ? new Date(t).toLocaleString() : '—'
}

const loading = ref(false)
const rows = ref([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const statusFilter = ref('')
const levelFilter = ref('')
const keyword = ref('')
// device_id → 设备编码/名称：AlertPublic 里只有 device_id，列表上要显示设备，前端做一次映射
const devices = ref([])

function deviceLabel(row) {
  const device = devices.value.find((item) => item.id === row.device_id)
  return device ? device.device_code : `设备 #${row.device_id}`
}

onMounted(async () => {
  fetchData()
  try {
    devices.value = (await getDevices({ page: 1, page_size: 100 })).data.data.items
  } catch {
    /* 拿不到设备列表就退化成显示 id */
  }
})

async function fetchData() {
  loading.value = true
  try {
    const res = await getAlerts({
      page: page.value,
      page_size: pageSize.value,
      status: statusFilter.value || undefined,
      alert_level: levelFilter.value || undefined,
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
</script>

<template>
  <div class="page">
    <div class="page-header">
      <div>
        <h3>告警中心</h3>
        <p class="subtitle">
          工控机上报的原始告警；同设备同告警码的重复上报会合并计数（出现次数），只有新故障才触发诊断
        </p>
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
          v-model="levelFilter"
          placeholder="级别"
          clearable
          style="width: 130px"
          @change="resetAndFetch"
        >
          <el-option v-for="item in LEVEL_OPTIONS" :key="item.value" :label="item.label" :value="item.value" />
        </el-select>
        <el-input
          v-model="keyword"
          placeholder="搜索告警码 / 名称"
          clearable
          style="width: 220px"
          @keyup.enter="resetAndFetch"
        />
        <el-button @click="resetAndFetch">查询</el-button>
      </div>

      <el-table :data="rows" stripe v-loading="loading" style="width: 100%">
        <el-table-column label="设备" min-width="150">
          <template #default="{ row }">{{ deviceLabel(row) }}</template>
        </el-table-column>
        <el-table-column prop="alert_code" label="告警码" width="120" />
        <el-table-column prop="alert_name" label="告警名称" min-width="140" />
        <el-table-column label="级别" width="90">
          <template #default="{ row }">
            <el-tag :type="levelType(row.alert_level)" size="small">
              {{ LEVEL_LABELS[row.alert_level] || row.alert_level }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="100">
          <template #default="{ row }">
            <el-tag :type="statusType(row.status)" size="small">
              {{ STATUS_LABELS[row.status] || row.status }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="出现次数" width="90">
          <template #default="{ row }">{{ row.occurrence_count }}</template>
        </el-table-column>
        <el-table-column label="最近发生" width="170">
          <template #default="{ row }">{{ formatTime(row.last_seen_at) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="90" fixed="right">
          <template #default="{ row }">
            <el-button text type="primary" size="small" @click="router.push(`/alerts/${row.id}`)">
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
  </div>
</template>

<style scoped>
.page { max-width: 1250px; }
.page-header { margin-bottom: 20px; display: flex; justify-content: space-between; align-items: flex-start; }
.page-header h3 { font-size: 20px; font-weight: 600; }
.subtitle { color: #909399; font-size: 13px; margin-top: 6px; }
.filters { display: flex; gap: 12px; margin-bottom: 16px; }
.pagination-wrap { display: flex; justify-content: flex-end; margin-top: 16px; }
</style>
