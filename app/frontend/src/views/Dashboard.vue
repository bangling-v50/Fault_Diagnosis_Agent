<template>
  <div class="page">
    <div class="page-header">
      <h3>概览</h3>
      <p class="subtitle">
        {{ user?.real_name || user?.username }}，欢迎回来
        <el-tag size="small" type="info">{{ roleLabel }}</el-tag>
      </p>
    </div>

    <el-row :gutter="16">
      <el-col :span="8">
        <el-card shadow="never" class="stat-card" v-loading="loading">
          <div class="stat-label">设备总数</div>
          <div class="stat-value">{{ stats.devices }}</div>
          <div class="stat-hint">{{ canManage ? '全部租户' : '本租户' }}</div>
        </el-card>
      </el-col>
      <el-col v-if="canManage" :span="8">
        <el-card shadow="never" class="stat-card" v-loading="loading">
          <div class="stat-label">账号总数</div>
          <div class="stat-value">{{ stats.users }}</div>
          <div class="stat-hint">可见范围内的账号</div>
        </el-card>
      </el-col>
      <el-col v-if="canManage" :span="8">
        <el-card shadow="never" class="stat-card" v-loading="loading">
          <div class="stat-label">租户总数</div>
          <div class="stat-value">{{ stats.tenants }}</div>
          <div class="stat-hint">含已停用</div>
        </el-card>
      </el-col>
    </el-row>

    <el-row :gutter="16" class="stat-row">
      <el-col :span="8">
        <el-card shadow="never" class="stat-card" v-loading="loading">
          <div class="stat-label">诊断任务</div>
          <div class="stat-value">{{ stats.diagnoses }}</div>
          <div class="stat-hint">{{ canManage ? '全部租户' : '本租户' }}（点菜单「故障诊断」看详情）</div>
        </el-card>
      </el-col>
      <el-col :span="8">
        <el-card shadow="never" class="stat-card" v-loading="loading">
          <div class="stat-label">执行中</div>
          <div class="stat-value">{{ stats.runningDiagnoses }}</div>
          <div class="stat-hint">排队 + 执行中的诊断</div>
        </el-card>
      </el-col>
      <el-col :span="8">
        <el-card shadow="never" class="stat-card" v-loading="loading">
          <div class="stat-label">未结工单</div>
          <div class="stat-value">{{ stats.openWorkOrders }}</div>
          <div class="stat-hint">{{ canManage ? '全部租户' : '本租户' }}（点菜单「维修工单」处理）</div>
        </el-card>
      </el-col>
    </el-row>

    <el-card shadow="never" class="todo-card">
      <template #header>待接入</template>
      <ul class="todo-list">
        <li>诊断失败后的重试入口</li>
        <li>告警/工单的实时推送（现在靠轮询）</li>
      </ul>
    </el-card>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { getDiagnoses } from '../api/diagnosis'
import { getDevices } from '../api/devices'
import { getTenants } from '../api/tenants'
import { getUsers } from '../api/users'
import { getWorkOrders } from '../api/workOrders'
import { getUser } from '../utils/token'

const user = ref(getUser())
const loading = ref(false)
const stats = ref({
  devices: 0, users: 0, tenants: 0, diagnoses: 0, runningDiagnoses: 0, openWorkOrders: 0,
})

const canManage = computed(() => ['engineer', 'admin'].includes(user.value?.role))
const roleLabel = computed(
  () =>
    ({ operator: '工厂操作员', engineer: '售后技术', admin: '管理员' }[user.value?.role] ||
    user.value?.role),
)

onMounted(async () => {
  loading.value = true
  try {
    // page_size=1 只为取 total
    const devices = await getDevices({ page: 1, page_size: 1 })
    stats.value.devices = devices.data.data.total
    if (canManage.value) {
      const users = await getUsers({ page: 1, page_size: 1 })
      stats.value.users = users.data.data.total
      const tenants = await getTenants({ page: 1, page_size: 1 })
      stats.value.tenants = tenants.data.data.total
    }
    // 诊断：operator 也能看，只是列表被后端裁到本租户
    const diagnoses = await getDiagnoses({ page: 1, page_size: 1 })
    stats.value.diagnoses = diagnoses.data.data.total
    const running = await getDiagnoses({ page: 1, page_size: 1, status: 'RUNNING' })
    const queued = await getDiagnoses({ page: 1, page_size: 1, status: 'QUEUED' })
    stats.value.runningDiagnoses = running.data.data.total + queued.data.data.total
    // 未结工单：把几个"还没完"的状态各取一次 total 加起来
    const openCounts = await Promise.all(
      ['OPEN', 'ASSIGNED', 'IN_PROGRESS'].map((status) =>
        getWorkOrders({ page: 1, page_size: 1, status }),
      ),
    )
    stats.value.openWorkOrders = openCounts.reduce((sum, res) => sum + res.data.data.total, 0)
  } catch {
    /* 出错时保持 0，不打断页面 */
  } finally {
    loading.value = false
  }
})
</script>

<style scoped>
.page { max-width: 1100px; }
.page-header { margin-bottom: 20px; }
.page-header h3 { font-size: 20px; font-weight: 600; }
.subtitle { color: #909399; font-size: 14px; margin-top: 6px; display: flex; gap: 8px; align-items: center; }

.stat-card { text-align: left; }
.stat-row { margin-top: 16px; }
.stat-label { font-size: 14px; color: #909399; }
.stat-value { font-size: 32px; font-weight: 700; color: #1a1b2e; margin: 8px 0 4px; }
.stat-hint { font-size: 12px; color: #c0c4cc; }

.todo-card { margin-top: 16px; }
.todo-list { margin: 0; padding-left: 18px; color: #606266; line-height: 2; font-size: 14px; }
</style>
