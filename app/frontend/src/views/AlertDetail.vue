<script setup>
import { ElMessage } from 'element-plus'
import { onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { getAlert } from '../api/alerts'
import { getDiagnoses } from '../api/diagnosis'
import { getDevice } from '../api/devices'
import { extractError } from '../api/index'

const route = useRoute()
const router = useRouter()
const alertId = route.params.id

const LEVEL_LABELS = { info: '提示', warning: '警告', critical: '严重' }
const STATUS_LABELS = { NEW: '新告警', DIAGNOSED: '已诊断' }
const LEVEL_TYPES = { info: 'info', warning: 'warning', critical: 'danger' }

const loading = ref(false)
const loadFailed = ref(false)
const alert = ref(null)
const device = ref(null)
// 这条告警触发的诊断任务：告警 → 诊断是主链路，这里给一个直达入口
const tasks = ref([])

function formatTime(t) {
  return t ? new Date(t).toLocaleString() : '—'
}
function statusLabel(value) {
  return STATUS_LABELS[value] || value
}

onMounted(async () => {
  loading.value = true
  try {
    alert.value = (await getAlert(alertId)).data.data
    try {
      device.value = (await getDevice(alert.value.device_id)).data.data
    } catch {
      /* 设备可能已被删，不影响告警本身 */
    }
    try {
      tasks.value = (await getDiagnoses({ page: 1, page_size: 10, alert_id: alertId })).data.data.items
    } catch {
      /* 诊断列表拉不到不影响告警详情 */
    }
  } catch (e) {
    loadFailed.value = true
    ElMessage.error(extractError(e))
  } finally {
    loading.value = false
  }
})
</script>

<template>
  <div class="page" v-loading="loading">
    <div class="page-header">
      <div>
        <h3>
          告警 #{{ alertId }}
          <el-tag v-if="alert" :type="LEVEL_TYPES[alert.alert_level] || 'info'" size="small">
            {{ LEVEL_LABELS[alert.alert_level] || alert.alert_level }}
          </el-tag>
          <el-tag v-if="alert" size="small" type="info">{{ statusLabel(alert.status) }}</el-tag>
        </h3>
        <p class="subtitle" v-if="alert">
          {{ device ? `${device.device_code}（${device.device_name || '未命名'}）` : `设备 #${alert.device_id}` }}
          ｜ 累计出现 {{ alert.occurrence_count }} 次
        </p>
      </div>
      <el-button @click="router.push('/alerts')">返回列表</el-button>
    </div>

    <el-empty v-if="loadFailed" description="告警不存在或无权查看" />

    <template v-else-if="alert">
      <el-card shadow="never" class="block-gap">
        <template #header>告警信息</template>
        <el-descriptions :column="3" border size="small">
          <el-descriptions-item label="告警码">{{ alert.alert_code }}</el-descriptions-item>
          <el-descriptions-item label="告警名称">{{ alert.alert_name || '—' }}</el-descriptions-item>
          <el-descriptions-item label="来源">{{ alert.source || '—' }}</el-descriptions-item>
          <el-descriptions-item label="首次发生">{{ formatTime(alert.first_seen_at) }}</el-descriptions-item>
          <el-descriptions-item label="最近发生">{{ formatTime(alert.last_seen_at) }}</el-descriptions-item>
          <el-descriptions-item label="上报时间">{{ formatTime(alert.created_at) }}</el-descriptions-item>
        </el-descriptions>

        <h4 class="section-title">原始报文</h4>
        <pre class="raw">{{ alert.raw_message || '（设备未上报原始报文）' }}</pre>

        <template v-if="alert.sensor_summary">
          <h4 class="section-title">传感器摘要</h4>
          <pre class="raw">{{ JSON.stringify(alert.sensor_summary, null, 2) }}</pre>
        </template>
      </el-card>

      <el-card shadow="never">
        <template #header>这条告警触发的诊断（{{ tasks.length }}）</template>
        <el-table v-if="tasks.length" :data="tasks" size="small" style="width: 100%">
          <el-table-column prop="id" label="任务" width="80" />
          <el-table-column label="状态" width="100">
            <template #default="{ row }">
              <el-tag
                size="small"
                :type="{ SUCCEEDED: 'success', FAILED: 'danger', RUNNING: 'warning', QUEUED: 'info', CANCELLED: 'info' }[row.status] || 'info'"
              >
                {{ { QUEUED: '排队中', RUNNING: '执行中', SUCCEEDED: '已完成', FAILED: '失败', CANCELLED: '已取消' }[row.status] || row.status }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="报告版本" width="100">
            <template #default="{ row }">
              {{ row.result_summary?.reportVersion ? `v${row.result_summary.reportVersion}` : '—' }}
            </template>
          </el-table-column>
          <el-table-column label="创建时间" width="170">
            <template #default="{ row }">{{ formatTime(row.created_at) }}</template>
          </el-table-column>
          <el-table-column label="操作" width="90">
            <template #default="{ row }">
              <el-button text type="primary" size="small" @click="router.push(`/diagnoses/${row.id}`)">
                查看
              </el-button>
            </template>
          </el-table-column>
        </el-table>
        <el-empty v-else description="这条告警没有触发诊断（可能上报时已有未处理的同类告警被合并了）" />
      </el-card>
    </template>
  </div>
</template>

<style scoped>
.page { max-width: 1100px; }
.page-header { margin-bottom: 20px; display: flex; justify-content: space-between; align-items: flex-start; }
.page-header h3 { font-size: 20px; font-weight: 600; display: flex; align-items: center; gap: 8px; }
.subtitle { color: #909399; font-size: 13px; margin-top: 6px; }
.block-gap { margin-bottom: 16px; }
.section-title { font-size: 14px; font-weight: 600; margin: 18px 0 8px; color: #303133; }
.raw { background: #f7f8fa; border: 1px solid #ebeef5; border-radius: 4px; padding: 10px 12px; font-size: 13px; color: #606266; white-space: pre-wrap; word-break: break-all; margin: 0; }
</style>
