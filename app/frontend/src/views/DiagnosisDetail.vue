<script setup>
import { ElMessage } from 'element-plus'
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { createChatSession } from '../api/chat'
import { getDiagnosis } from '../api/diagnosis'
import { extractError } from '../api/index'
import { createWorkOrder } from '../api/workOrders'
import MarkdownContent from '../components/MarkdownContent.vue'
import { getUser } from '../utils/token'

const route = useRoute()
const router = useRouter()
const taskId = route.params.id

// 与 workflow 的节点顺序一致（diagnosis_tasks.current_phase 的取值）
const PHASES = [
  { value: 'collect_knowledge', label: '检索知识库' },
  { value: 'collect_alert_history', label: '查历史告警' },
  { value: 'collect_work_orders', label: '查历史工单' },
  { value: 'write_report', label: '生成报告' },
  { value: 'evaluate_report', label: '评审报告' },
]
const KIND_LABELS = {
  alert: '本次告警',
  knowledge: '知识库片段',
  alert_history: '历史告警',
  work_order: '历史维修工单',
}
const RISK_LABELS = { low: '低', medium: '中', high: '高' }
const LEVEL_LABELS = { low: '低', medium: '中', high: '高' }
const SHUTDOWN_LABELS = { yes: '需要停机', no: '无需停机', conditional: '视情况停机' }
const STEP_STATUS_TYPE = { SUCCEEDED: 'success', FAILED: 'danger', RUNNING: 'warning' }

const loading = ref(false)
const loadFailed = ref(false)
const task = ref(null)
const steps = ref([])
const evidence = ref([])
const reports = ref([])
const links = ref([])
const activeVersion = ref(null)
const highlightNo = ref(null)

// 未到终态就每 3 秒拉一次：诊断要跑几秒到几十秒，让用户盯着一个死页面不合适
let timer = null
const TERMINAL = ['SUCCEEDED', 'FAILED', 'CANCELLED']
const running = computed(() => task.value && !TERMINAL.includes(task.value.status))
const phaseIndex = computed(() => {
  const index = PHASES.findIndex((p) => p.value === task.value?.current_phase)
  return index < 0 ? 0 : index
})
const activeReport = computed(
  () => reports.value.find((r) => r.version === activeVersion.value) || reports.value.at(-1) || null,
)
const review = computed(() => activeReport.value?.payload?.review || null)
/** 报告里被引用到的证据编号（用来在证据链里高亮） */
const citedNos = computed(() => {
  const numbers = new Set()
  for (const report of reports.value) {
    for (const group of ['possible_causes', 'suggested_actions']) {
      for (const item of report[group] || []) {
        for (const n of item.evidenceIndexes || []) numbers.add(n)
      }
    }
  }
  return numbers
})

function phaseLabel(phase) {
  return PHASES.find((p) => p.value === phase)?.label || phase || '—'
}
function kindLabel(kind) {
  return KIND_LABELS[kind] || kind
}
function levelLabel(value) {
  return LEVEL_LABELS[value] || ''
}
function shutdownLabel(value) {
  return SHUTDOWN_LABELS[value] || '—'
}
function formatTime(t) {
  return t ? new Date(t).toLocaleString() : '—'
}
/** 步骤 payload 压成一行摘要：不同节点的 payload 形状不同，挑最有信息量的键 */
function stepSummary(payload = {}) {
  const parts = []
  if (payload.tool) parts.push(`工具 ${payload.tool}`)
  if (payload.hitCount !== undefined) parts.push(`命中 ${payload.hitCount} 条`)
  if (payload.count !== undefined) parts.push(`查到 ${payload.count} 条`)
  if (payload.version !== undefined) parts.push(`第 ${payload.version} 版`)
  if (payload.verdict) parts.push(`评审 ${payload.verdict}`)
  if (payload.retrievalNote) parts.push(payload.retrievalNote)
  if (payload.error) parts.push(payload.error)
  return parts.join(' ｜ ')
}

async function fetchData() {
  loading.value = true
  try {
    const res = await getDiagnosis(taskId)
    const data = res.data.data
    task.value = data.task
    steps.value = data.steps
    evidence.value = data.evidence
    reports.value = data.reports
    links.value = data.links
    if (activeVersion.value === null) {
      activeVersion.value = (data.reports.find((r) => r.is_final) || data.reports.at(-1))?.version ?? null
    }
    schedulePolling()
  } catch (e) {
    loadFailed.value = true
    ElMessage.error(extractError(e))
  } finally {
    loading.value = false
  }
}

function schedulePolling() {
  clearTimeout(timer)
  if (!running.value) return
  timer = setTimeout(fetchData, 3000)
}

/** 点报告表格里的引用编号 → 滚到对应的那条证据并闪一下。 */
function scrollToEvidence(no) {
  const target = document.getElementById(`evidence-${no}`)
  if (!target) return
  target.scrollIntoView({ behavior: 'smooth', block: 'center' })
  highlightNo.value = no
  setTimeout(() => {
    highlightNo.value = null
  }, 1600)
}

/** 带着这台设备、这条告警与这份报告去开一个聊天会话——助手一上来就知道我们在聊哪次诊断。 */
async function askFollowUp() {
  try {
    const res = await createChatSession({
      device_id: task.value.device_id,
      alert_id: task.value.alert_id,
      diagnosis_report_id: activeReport.value?.id,
    })
    router.push(`/chat/${res.data.data.id}`)
  } catch (e) {
    ElMessage.error(extractError(e))
  }
}

// ---------- 从这份报告生成工单 ----------

const me = ref(getUser())
const canManage = computed(() => ['engineer', 'admin'].includes(me.value?.role))
const orderVisible = ref(false)
const orderSaving = ref(false)
const orderForm = reactive({ title: '', description: '', priority: 'medium' })

function openCreateOrder() {
  Object.assign(orderForm, {
    title: activeReport.value?.title || `故障诊断处理（任务 #${taskId}）`,
    description: activeReport.value?.summary || '',
    priority: activeReport.value?.risk_level === 'high' ? 'high' : 'medium',
  })
  orderVisible.value = true
}

async function handleCreateOrder() {
  if (!orderForm.title) {
    ElMessage.warning('请填写标题')
    return
  }
  orderSaving.value = true
  try {
    // ⚠️ 一定要带上 alert_id：工单与故障码之间只有「告警」这一条纽带，
    // 不带的话这张工单不会被后续诊断当作历史维修经验检索到
    const res = await createWorkOrder({
      device_id: task.value.device_id,
      alert_id: task.value.alert_id,
      diagnosis_report_id: activeReport.value?.id,
      title: orderForm.title,
      description: orderForm.description || undefined,
      priority: orderForm.priority,
    })
    ElMessage.success('工单已创建')
    orderVisible.value = false
    router.push(`/work-orders/${res.data.data.id}`)
  } catch (e) {
    ElMessage.error(extractError(e))
  } finally {
    orderSaving.value = false
  }
}

onMounted(fetchData)
onBeforeUnmount(() => clearTimeout(timer))
</script>

<template>
  <div class="page" v-loading="loading">
    <div class="page-header">
      <div>
        <h3>
          诊断任务 #{{ taskId }}
          <el-tag v-if="task" :type="{ SUCCEEDED: 'success', FAILED: 'danger', RUNNING: 'warning', QUEUED: 'info', CANCELLED: 'info' }[task.status]" size="small">
            {{ { QUEUED: '排队中', RUNNING: '执行中', SUCCEEDED: '已完成', FAILED: '失败', CANCELLED: '已取消' }[task?.status] || task?.status }}
          </el-tag>
        </h3>
        <p class="subtitle" v-if="task">
          {{ task.device_code || `设备 ${task.device_id}` }} ｜ 告警码 {{ task.alert_code || task.alert_id }}
          ｜ 触发方式 {{ { auto: '自动', manual: '手动', retry: '重试' }[task.trigger_type] || task.trigger_type }}
          ｜ 创建于 {{ formatTime(task.created_at) }}
        </p>
      </div>
      <div class="header-actions">
        <el-button @click="router.push('/diagnoses')">返回列表</el-button>
        <el-button v-if="activeReport" plain @click="askFollowUp">追问助手</el-button>
        <el-button v-if="canManage && activeReport" type="primary" plain @click="openCreateOrder">
          生成工单
        </el-button>
        <el-button type="primary" @click="fetchData">刷新</el-button>
      </div>
    </div>

    <el-empty v-if="loadFailed" description="诊断任务不存在或无权查看" />

    <template v-else-if="task">
      <el-alert
        v-if="task.status === 'FAILED'"
        type="error"
        :closable="false"
        title="这次诊断没有跑完"
        :description="task.error_message || '未记录失败原因'"
        class="block-gap"
      />

      <el-card v-if="running" shadow="never" class="block-gap">
        <template #header>正在诊断（每 3 秒自动刷新）</template>
        <el-steps :active="phaseIndex" simple>
          <el-step v-for="item in PHASES" :key="item.value" :title="item.label" />
        </el-steps>
      </el-card>

      <!-- 报告 -->
      <el-card shadow="never" class="block-gap">
        <template #header>
          <div class="card-header">
            <span>诊断报告</span>
            <el-radio-group v-if="reports.length > 1" v-model="activeVersion" size="small">
              <el-radio-button v-for="item in reports" :key="item.id" :value="item.version">
                v{{ item.version }}{{ item.is_final ? '（最终）' : '' }}
              </el-radio-button>
            </el-radio-group>
          </div>
        </template>

        <template v-if="activeReport">
          <el-alert
            v-if="review && review.verdict === 'fail'"
            type="warning"
            :closable="false"
            title="这份报告没有通过自动评审"
            :description="(review.issues || []).join('；') || '评审未给出具体问题'"
            class="block-gap"
          />
          <el-descriptions :column="4" border size="small">
            <el-descriptions-item label="风险等级">
              {{ RISK_LABELS[activeReport.risk_level] || '—' }}
            </el-descriptions-item>
            <el-descriptions-item label="是否停机">{{ shutdownLabel(activeReport.need_shutdown) }}</el-descriptions-item>
            <el-descriptions-item label="现场服务">
              {{ activeReport.need_field_service === true ? '需要' : activeReport.need_field_service === false ? '不需要' : '—' }}
            </el-descriptions-item>
            <el-descriptions-item label="生成模型">{{ activeReport.llm_model || '—' }}</el-descriptions-item>
          </el-descriptions>

          <h4 class="section-title">可能原因</h4>
          <el-table :data="activeReport.possible_causes" size="small" style="width: 100%">
            <el-table-column type="index" width="50" />
            <el-table-column prop="cause" label="原因" min-width="260" />
            <el-table-column label="置信度" width="90">
              <template #default="{ row }">{{ levelLabel(row.confidence) || '—' }}</template>
            </el-table-column>
            <el-table-column label="依据" width="160">
              <template #default="{ row }">
                <el-button
                  v-for="no in row.evidenceIndexes || []"
                  :key="no"
                  text
                  type="primary"
                  size="small"
                  @click="scrollToEvidence(no)"
                  >[{{ no }}]</el-button
                >
                <span v-if="!(row.evidenceIndexes || []).length" class="muted">—</span>
              </template>
            </el-table-column>
          </el-table>

          <h4 class="section-title">建议措施</h4>
          <el-table :data="activeReport.suggested_actions" size="small" style="width: 100%">
            <el-table-column type="index" width="50" />
            <el-table-column prop="action" label="措施" min-width="260" />
            <el-table-column label="优先级" width="90">
              <template #default="{ row }">{{ levelLabel(row.priority) || '—' }}</template>
            </el-table-column>
            <el-table-column label="依据" width="160">
              <template #default="{ row }">
                <el-button
                  v-for="no in row.evidenceIndexes || []"
                  :key="no"
                  text
                  type="primary"
                  size="small"
                  @click="scrollToEvidence(no)"
                  >[{{ no }}]</el-button
                >
                <span v-if="!(row.evidenceIndexes || []).length" class="muted">—</span>
              </template>
            </el-table-column>
          </el-table>

          <h4 class="section-title">报告全文</h4>
          <MarkdownContent :content="activeReport.content" mode="report" />
        </template>
        <el-empty v-else description="报告还没生成（任务可能仍在执行，或已失败）" />
      </el-card>

      <!-- 执行步骤 -->
      <el-card shadow="never" class="block-gap">
        <template #header>执行步骤（{{ steps.length }} 步）</template>
        <el-timeline v-if="steps.length">
          <el-timeline-item
            v-for="step in steps"
            :key="step.id"
            :timestamp="formatTime(step.created_at)"
            :type="STEP_STATUS_TYPE[step.status] || 'info'"
          >
            <span class="step-phase">{{ step.sequence }}. {{ phaseLabel(step.phase) }}</span>
            <el-tag :type="STEP_STATUS_TYPE[step.status] || 'info'" size="small" class="step-tag">
              {{ step.status }}
            </el-tag>
            <div class="step-summary">{{ stepSummary(step.payload) }}</div>
          </el-timeline-item>
        </el-timeline>
        <el-empty v-else description="还没有执行记录" />
      </el-card>

      <!-- 证据链 -->
      <el-card shadow="never">
        <template #header>
          证据链（{{ evidence.length }} 条）— 报告里的 [n] 就是这里的编号
        </template>
        <div v-if="evidence.length">
          <div
            v-for="item in evidence"
            :id="`evidence-${item.citation_no}`"
            :key="item.id"
            :class="['evidence-item', { 'is-cited': citedNos.has(item.citation_no), 'is-active': highlightNo === item.citation_no }]"
          >
            <div class="evidence-head">
              <span class="evidence-no">[{{ item.citation_no }}]</span>
              <el-tag size="small" type="info">{{ kindLabel(item.kind) }}</el-tag>
              <span class="evidence-title">{{ item.source_title || `来源 ${item.source_type}#${item.source_id}` }}</span>
              <el-tag v-if="citedNos.has(item.citation_no)" size="small" type="success">被报告引用</el-tag>
            </div>
            <div class="evidence-summary">{{ item.summary }}</div>
          </div>
        </div>
        <el-empty v-else description="这次诊断没有采集到证据" />
      </el-card>
    </template>

    <el-dialog v-model="orderVisible" title="从这份报告生成工单" width="560px">
      <el-form :model="orderForm" label-width="90px">
        <el-form-item label="标题" required>
          <el-input v-model="orderForm.title" maxlength="255" />
        </el-form-item>
        <el-form-item label="优先级">
          <el-select v-model="orderForm.priority" style="width: 100%">
            <el-option label="低" value="low" />
            <el-option label="中" value="medium" />
            <el-option label="高" value="high" />
            <el-option label="紧急" value="urgent" />
          </el-select>
        </el-form-item>
        <el-form-item label="故障描述">
          <el-input v-model="orderForm.description" type="textarea" :rows="4" />
        </el-form-item>
      </el-form>
      <p class="dialog-hint">
        会自动带上这台设备与本次告警，以及来源诊断报告——这样这张工单才会被后续诊断当成历史维修经验。
      </p>
      <template #footer>
        <el-button @click="orderVisible = false">取消</el-button>
        <el-button type="primary" :loading="orderSaving" @click="handleCreateOrder">创建工单</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.page { max-width: 1100px; }
.page-header { margin-bottom: 20px; display: flex; justify-content: space-between; align-items: flex-start; }
.page-header h3 { font-size: 20px; font-weight: 600; display: flex; align-items: center; gap: 8px; }
.subtitle { color: #909399; font-size: 13px; margin-top: 6px; }
.header-actions { display: flex; gap: 10px; }
.block-gap { margin-bottom: 16px; }
.card-header { display: flex; justify-content: space-between; align-items: center; }
.section-title { font-size: 14px; font-weight: 600; margin: 18px 0 10px; color: #303133; }
.step-phase { font-weight: 600; color: #303133; }
.step-tag { margin-left: 8px; }
.step-summary { color: #909399; font-size: 13px; margin-top: 4px; }
.muted { color: #c0c4cc; }
.evidence-item { border: 1px solid #ebeef5; border-left: 3px solid #dcdfe6; border-radius: 4px; padding: 10px 12px; margin-bottom: 10px; transition: border-color 0.3s, background-color 0.3s; }
.evidence-item.is-cited { border-left-color: #67c23a; background: #fafcf8; }
.evidence-item.is-active { border-color: #409eff; background: #ecf5ff; }
.evidence-head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.evidence-no { font-weight: 700; color: #409eff; }
.evidence-title { color: #303133; font-size: 13px; }
.evidence-summary { color: #606266; font-size: 13px; line-height: 1.7; margin-top: 6px; white-space: pre-wrap; }
.dialog-hint { color: #909399; font-size: 12px; line-height: 1.7; margin: 0 0 0 90px; }
</style>
