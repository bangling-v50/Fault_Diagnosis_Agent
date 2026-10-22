<script setup>
import { ElMessage } from 'element-plus'
import { computed, onMounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { createCase, getCases } from '../api/cases'
import { getDevice } from '../api/devices'
import { extractError } from '../api/index'
import { getAssignableUsers } from '../api/users'
import { getWorkOrder, updateWorkOrder } from '../api/workOrders'
import { getUser } from '../utils/token'

const route = useRoute()
const router = useRouter()
const orderId = route.params.id

const STATUS_OPTIONS = [
  { label: '待受理', value: 'OPEN' },
  { label: '已派单', value: 'ASSIGNED' },
  { label: '处理中', value: 'IN_PROGRESS' },
  { label: '已解决', value: 'RESOLVED' },
]
const STATUS_LABELS = { OPEN: '待受理', ASSIGNED: '已派单', IN_PROGRESS: '处理中', RESOLVED: '已解决', CLOSED: '已关闭' }
const STATUS_TYPES = { OPEN: 'info', ASSIGNED: 'warning', IN_PROGRESS: 'warning', RESOLVED: 'success', CLOSED: 'success' }
const PRIORITY_LABELS = { low: '低', medium: '中', high: '高', urgent: '紧急' }
const CLOSED_STATUSES = ['RESOLVED', 'CLOSED']

const me = ref(getUser())
const canManage = computed(() => ['engineer', 'admin'].includes(me.value?.role))

const loading = ref(false)
const saving = ref(false)
const loadFailed = ref(false)
const order = ref(null)
const device = ref(null)
const assignees = ref([])
const linkedCase = ref(null)

const assignForm = reactive({ assigned_to: null })
const closeVisible = ref(false)
const closeForm = reactive({
  confirmed_root_cause: '', solution: '', resolution_result: '',
})
const caseVisible = ref(false)
const caseForm = reactive({
  root_cause: '', solution: '', resolution_result: '', summary: '', scope: 'tenant',
})

const isClosed = computed(() => order.value && CLOSED_STATUSES.includes(order.value.status))
const assigneeName = computed(() => {
  const person = assignees.value.find((item) => item.id === order.value?.assigned_to)
  return person ? person.real_name || person.username : null
})

function formatTime(t) {
  return t ? new Date(t).toLocaleString() : '—'
}

onMounted(async () => {
  loading.value = true
  try {
    order.value = (await getWorkOrder(orderId)).data.data
    assignForm.assigned_to = order.value.assigned_to
    try {
      device.value = (await getDevice(order.value.device_id)).data.data
    } catch {
      /* 设备可能已删 */
    }
    if (canManage.value) {
      try {
        assignees.value = (await getAssignableUsers()).data.data
      } catch {
        /* 拿不到候选人就只显示已派之人 */
      }
    }
    await loadCase()
  } catch (e) {
    loadFailed.value = true
    ElMessage.error(extractError(e))
  } finally {
    loading.value = false
  }
})

async function loadCase() {
  try {
    const res = await getCases({ page: 1, page_size: 1, work_order_id: orderId })
    linkedCase.value = res.data.data.items[0] || null
  } catch {
    /* 案例查询失败不影响工单详情 */
  }
}

async function saveAssignee() {
  saving.value = true
  try {
    order.value = (await updateWorkOrder(orderId, { assigned_to: assignForm.assigned_to }))
      .data.data
    ElMessage.success(assignForm.assigned_to ? '已派单' : '已取消指派')
  } catch (e) {
    ElMessage.error(extractError(e))
  } finally {
    saving.value = false
  }
}

async function changeStatus(status) {
  saving.value = true
  try {
    order.value = (await updateWorkOrder(orderId, { status })).data.data
    ElMessage.success('状态已更新')
  } catch (e) {
    ElMessage.error(extractError(e))
  } finally {
    saving.value = false
  }
}

function openClose() {
  Object.assign(closeForm, {
    confirmed_root_cause: order.value.confirmed_root_cause || '',
    solution: order.value.solution || '',
    resolution_result: order.value.resolution_result || '',
  })
  closeVisible.value = true
}

async function handleClose() {
  if (!closeForm.confirmed_root_cause || !closeForm.solution) {
    ElMessage.warning('结案必须填写确认根因与处理方案')
    return
  }
  saving.value = true
  try {
    order.value = (await updateWorkOrder(orderId, { status: 'CLOSED', ...closeForm })).data.data
    ElMessage.success('工单已结案')
    closeVisible.value = false
  } catch (e) {
    ElMessage.error(extractError(e))
  } finally {
    saving.value = false
  }
}

function openCase() {
  Object.assign(caseForm, {
    root_cause: order.value.confirmed_root_cause || '',
    solution: order.value.solution || '',
    resolution_result: order.value.resolution_result || '',
    summary: '',
    scope: 'tenant',
  })
  caseVisible.value = true
}

async function handleCreateCase() {
  if (!caseForm.root_cause || !caseForm.solution || !caseForm.resolution_result) {
    ElMessage.warning('根因、方案、处理结果都要填')
    return
  }
  saving.value = true
  try {
    const res = await createCase({
      work_order_id: Number(orderId),
      root_cause: caseForm.root_cause,
      solution: caseForm.solution,
      resolution_result: caseForm.resolution_result,
      summary: caseForm.summary || undefined,
      scope: caseForm.scope,
    })
    const { case: created, created: isNew } = res.data.data
    linkedCase.value = created
    caseVisible.value = false
    ElMessage.success(
      isNew
        ? `案例已沉淀（文档 #${created.document_id} 正在索引，完成后即可被检索到）`
        : '该案例已存在（同一张工单，或同机型同告警码同根因同方案的案例），未重复沉淀',
    )
  } catch (e) {
    ElMessage.error(extractError(e))
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <div class="page" v-loading="loading">
    <div class="page-header">
      <div>
        <h3>
          工单 #{{ orderId }}
          <el-tag v-if="order" :type="STATUS_TYPES[order.status] || 'info'" size="small">
            {{ STATUS_LABELS[order.status] || order.status }}
          </el-tag>
          <el-tag v-if="order" size="small" type="info">
            {{ PRIORITY_LABELS[order.priority] || order.priority }}
          </el-tag>
        </h3>
        <p class="subtitle" v-if="order">{{ order.title }}</p>
      </div>
      <el-button @click="router.push('/work-orders')">返回列表</el-button>
    </div>

    <el-empty v-if="loadFailed" description="工单不存在或无权查看" />

    <template v-else-if="order">
      <el-card shadow="never" class="block-gap">
        <template #header>工单信息</template>
        <el-descriptions :column="3" border size="small">
          <el-descriptions-item label="设备">
            {{ device ? `${device.device_code}（${device.device_name || '未命名'}）` : `设备 #${order.device_id}` }}
          </el-descriptions-item>
          <el-descriptions-item label="处理人">
            <span v-if="assigneeName">{{ assigneeName }}</span>
            <span v-else-if="order.assigned_to">用户 #{{ order.assigned_to }}</span>
            <span v-else class="muted">未派单</span>
          </el-descriptions-item>
          <el-descriptions-item label="创建时间">{{ formatTime(order.created_at) }}</el-descriptions-item>
          <el-descriptions-item label="关联告警">
            <el-button v-if="order.alert_id" text type="primary" size="small"
                       @click="router.push(`/alerts/${order.alert_id}`)">
              #{{ order.alert_id }}
            </el-button>
            <span v-else class="muted">未关联（不会被后续诊断当作历史经验）</span>
          </el-descriptions-item>
          <el-descriptions-item label="来源诊断报告">
            <span v-if="order.diagnosis_report_id">#{{ order.diagnosis_report_id }}</span>
            <span v-else class="muted">无（手工建单）</span>
          </el-descriptions-item>
          <el-descriptions-item label="结案时间">{{ formatTime(order.closed_at) }}</el-descriptions-item>
        </el-descriptions>

        <template v-if="order.description">
          <h4 class="section-title">故障描述</h4>
          <p class="paragraph">{{ order.description }}</p>
        </template>
      </el-card>

      <el-card v-if="canManage" shadow="never" class="block-gap">
        <template #header>处理</template>

        <div class="action-row">
          <span class="action-label">派单</span>
          <el-select v-model="assignForm.assigned_to" placeholder="选择处理人" clearable filterable
                     style="width: 240px">
            <el-option
              v-for="item in assignees"
              :key="item.id"
              :label="item.real_name ? `${item.real_name}（${item.username}）` : item.username"
              :value="item.id"
            />
          </el-select>
          <el-button type="primary" :loading="saving" @click="saveAssignee">保存</el-button>
        </div>

        <div class="action-row">
          <span class="action-label">状态</span>
          <el-button
            v-for="item in STATUS_OPTIONS"
            :key="item.value"
            :disabled="order.status === item.value"
            :loading="saving"
            size="small"
            @click="changeStatus(item.value)"
          >
            {{ item.label }}
          </el-button>
          <el-button
            v-if="!isClosed"
            type="success"
            size="small"
            @click="openClose"
          >
            结案…
          </el-button>
          <span v-else class="closed-hint">已结案</span>
        </div>

        <p class="action-hint">
          结案必须填写「确认根因」与「处理方案」——它们同时也是案例沉淀的输入。
        </p>
      </el-card>

      <!-- 沉淀为案例：只有已结案工单才能做 -->
      <el-card shadow="never" class="block-gap">
        <template #header>维修案例</template>

        <template v-if="linkedCase">
          <el-alert
            type="success"
            :closable="false"
            title="这张工单已经沉淀为维修案例"
            :description="`案例 #${linkedCase.id} ｜ 机型 ${linkedCase.device_model} ｜ 告警码 ${linkedCase.alert_code || '—'} ｜ 知识文档 #${linkedCase.document_id ?? '未生成'}`"
          />
          <div class="case-body">
            <p><strong>确认根因：</strong>{{ linkedCase.root_cause }}</p>
            <p><strong>处理方案：</strong>{{ linkedCase.solution }}</p>
            <p><strong>处理结果：</strong>{{ linkedCase.resolution_result }}</p>
            <p v-if="linkedCase.keywords?.length" class="muted">
              关键词：{{ linkedCase.keywords.join('、') }}
            </p>
          </div>
          <el-button text type="primary" size="small" @click="router.push('/knowledge')">
            到知识库查看这份文档 →
          </el-button>
        </template>

        <template v-else-if="isClosed && canManage">
          <p class="paragraph">
            把这次的根因与处理方案固化成案例，系统会自动生成一份知识文档并索引——
            之后同租户的诊断检索就能命中它。
          </p>
          <el-button type="primary" @click="openCase">沉淀为案例</el-button>
        </template>

        <p v-else-if="!isClosed" class="muted">
          工单结案后才能沉淀案例（没修完就沉淀的是猜测，不是经验）。
        </p>
        <p v-else class="muted">尚无案例。</p>
      </el-card>
    </template>

    <el-dialog v-model="closeVisible" title="结案" width="560px">
      <el-form :model="closeForm" label-width="90px">
        <el-form-item label="确认根因" required>
          <el-input v-model="closeForm.confirmed_root_cause" type="textarea" :rows="2"
                    placeholder="这次故障到底是什么引起的（要能被别人复用）" />
        </el-form-item>
        <el-form-item label="处理方案" required>
          <el-input v-model="closeForm.solution" type="textarea" :rows="3"
                    placeholder="具体怎么处理的，能照着做的那种" />
        </el-form-item>
        <el-form-item label="处理结果">
          <el-input v-model="closeForm.resolution_result" type="textarea" :rows="2"
                    placeholder="修完之后的验证情况，如「连续运行 72 小时未复现」" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="closeVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="handleClose">确认结案</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="caseVisible" title="沉淀为维修案例" width="600px">
      <el-form :model="caseForm" label-width="90px">
        <el-form-item label="确认根因" required>
          <el-input v-model="caseForm.root_cause" type="textarea" :rows="2" />
        </el-form-item>
        <el-form-item label="处理方案" required>
          <el-input v-model="caseForm.solution" type="textarea" :rows="3" />
        </el-form-item>
        <el-form-item label="处理结果" required>
          <el-input v-model="caseForm.resolution_result" type="textarea" :rows="2" />
        </el-form-item>
        <el-form-item label="摘要">
          <el-input v-model="caseForm.summary" placeholder="留空则用标题与根因自动拼一句" />
        </el-form-item>
        <el-form-item label="可见范围">
          <el-radio-group v-model="caseForm.scope">
            <el-radio value="tenant">仅本租户</el-radio>
            <el-radio value="global">所有租户共享</el-radio>
          </el-radio-group>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="caseVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="handleCreateCase">
          沉淀并生成知识文档
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.page { max-width: 1100px; }
.page-header { margin-bottom: 20px; display: flex; justify-content: space-between; align-items: flex-start; }
.page-header h3 { font-size: 20px; font-weight: 600; display: flex; align-items: center; gap: 8px; }
.subtitle { color: #909399; font-size: 13px; margin-top: 6px; }
.block-gap { margin-bottom: 16px; }
.section-title { font-size: 14px; font-weight: 600; margin: 18px 0 8px; color: #303133; }
.paragraph { color: #606266; font-size: 14px; line-height: 1.8; margin: 0; }
.muted { color: #c0c4cc; font-size: 13px; }
.action-row { display: flex; align-items: center; gap: 10px; margin-bottom: 14px; flex-wrap: wrap; }
.action-label { width: 48px; color: #909399; font-size: 13px; }
.action-hint { color: #e6a23c; font-size: 12px; margin: 0; }
.closed-hint { color: #67c23a; font-size: 13px; }
.case-body { margin: 12px 0; color: #606266; font-size: 14px; line-height: 1.9; }
.case-body p { margin: 0; }
</style>
