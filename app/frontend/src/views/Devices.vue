<template>
  <div class="page">
    <div class="page-header">
      <div>
        <h3>设备</h3>
        <p class="subtitle">
          {{ canManage ? '售后技术可跨租户管理设备' : '当前显示本租户设备（只读）' }}
        </p>
      </div>
      <el-button v-if="canManage" type="primary" @click="openCreate">新增设备</el-button>
    </div>

    <el-card shadow="never">
      <div class="filters">
        <el-input
          v-model="keyword"
          placeholder="搜索编码 / 名称 / 型号 / 序列号"
          clearable
          style="width: 250px"
          @keyup.enter="fetchData"
        />
        <el-select
          v-if="canManage"
          v-model="tenantFilter"
          placeholder="租户"
          clearable
          style="width: 190px"
          @change="fetchData"
        >
          <el-option
            v-for="t in tenants"
            :key="t.id"
            :label="`${t.tenant_name}（${t.tenant_code}）`"
            :value="t.id"
          />
        </el-select>
        <el-select v-model="statusFilter" placeholder="状态" clearable style="width: 130px" @change="fetchData">
          <el-option label="在线" value="online" />
          <el-option label="离线" value="offline" />
          <el-option label="已停用" value="disabled" />
        </el-select>
        <el-button @click="fetchData">查询</el-button>
      </div>

      <el-table :data="rows" stripe v-loading="loading" style="width: 100%">
        <el-table-column prop="device_code" label="设备编码" width="150" />
        <el-table-column prop="device_name" label="设备名称" min-width="150" />
        <el-table-column prop="device_model" label="型号" width="120" />
        <el-table-column prop="serial_number" label="序列号" width="140" />
        <el-table-column prop="install_location" label="安装位置" min-width="140" />
        <el-table-column label="租户" width="90">
          <template #default="{ row }">#{{ row.tenant_id }}</template>
        </el-table-column>
        <el-table-column label="状态" width="90">
          <template #default="{ row }">
            <el-tag :type="statusType(row.status)" size="small">{{ statusLabel(row.status) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="创建时间" width="170">
          <template #default="{ row }">{{ formatTime(row.created_at) }}</template>
        </el-table-column>
        <el-table-column v-if="canManage" label="操作" width="90" fixed="right">
          <template #default="{ row }">
            <el-button text type="primary" size="small" @click="openEdit(row)">编辑</el-button>
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

    <!-- 新增 -->
    <el-dialog v-model="createVisible" title="新增设备" width="520px">
      <el-form :model="createForm" label-width="90px">
        <el-form-item label="所属租户">
          <el-select v-model="createForm.tenant_id" style="width: 100%" placeholder="选择租户">
            <el-option
              v-for="t in tenants"
              :key="t.id"
              :label="`${t.tenant_name}（${t.tenant_code}）`"
              :value="t.id"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="设备编码">
          <el-input v-model="createForm.device_code" placeholder="唯一编码，如 CNC-001" />
        </el-form-item>
        <el-form-item label="设备名称">
          <el-input v-model="createForm.device_name" />
        </el-form-item>
        <el-form-item label="型号">
          <el-input v-model="createForm.device_model" />
        </el-form-item>
        <el-form-item label="序列号">
          <el-input v-model="createForm.serial_number" />
        </el-form-item>
        <el-form-item label="安装位置">
          <el-input v-model="createForm.install_location" />
        </el-form-item>
        <el-form-item label="安装日期">
          <el-date-picker
            v-model="createForm.install_date"
            type="datetime"
            style="width: 100%"
            value-format="YYYY-MM-DDTHH:mm:ssZ"
            placeholder="选择日期时间"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="createVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="handleCreate">创建</el-button>
      </template>
    </el-dialog>

    <!-- 编辑 -->
    <el-dialog v-model="editVisible" title="编辑设备" width="500px">
      <el-form :model="editForm" label-width="90px">
        <el-form-item label="设备编码">
          <el-input :model-value="editForm.device_code" disabled />
        </el-form-item>
        <el-form-item label="设备名称">
          <el-input v-model="editForm.device_name" />
        </el-form-item>
        <el-form-item label="型号">
          <el-input v-model="editForm.device_model" />
        </el-form-item>
        <el-form-item label="序列号">
          <el-input v-model="editForm.serial_number" />
        </el-form-item>
        <el-form-item label="安装位置">
          <el-input v-model="editForm.install_location" />
        </el-form-item>
        <el-form-item label="状态">
          <el-select v-model="editForm.status" style="width: 100%">
            <el-option label="在线" value="online" />
            <el-option label="离线" value="offline" />
            <el-option label="已停用" value="disabled" />
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="editVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="handleEdit">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { createDevice, getDevices, updateDevice } from '../api/devices'
import { extractError } from '../api/index'
import { getTenants } from '../api/tenants'
import { getUser } from '../utils/token'

const me = ref(getUser())
const canManage = computed(() => ['engineer', 'admin'].includes(me.value?.role))

const loading = ref(false)
const saving = ref(false)
const rows = ref([])
const tenants = ref([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const keyword = ref('')
const tenantFilter = ref(null)
const statusFilter = ref('')

const createVisible = ref(false)
const editVisible = ref(false)
const createForm = reactive({
  tenant_id: null, device_code: '', device_name: '', device_model: '',
  serial_number: '', install_location: '', install_date: null,
})
const editForm = reactive({
  id: null, device_code: '', device_name: '', device_model: '',
  serial_number: '', install_location: '', status: 'offline',
})

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

async function fetchData() {
  loading.value = true
  try {
    const res = await getDevices({
      page: page.value,
      page_size: pageSize.value,
      tenant_id: canManage.value ? tenantFilter.value || undefined : undefined,
      status: statusFilter.value || undefined,
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

function openCreate() {
  Object.assign(createForm, {
    tenant_id: null, device_code: '', device_name: '', device_model: '',
    serial_number: '', install_location: '', install_date: null,
  })
  createVisible.value = true
}

async function handleCreate() {
  if (!createForm.tenant_id || !createForm.device_code) {
    ElMessage.warning('请选择租户并填写设备编码')
    return
  }
  saving.value = true
  try {
    await createDevice({
      tenant_id: createForm.tenant_id,
      device_code: createForm.device_code,
      device_name: createForm.device_name || undefined,
      device_model: createForm.device_model || undefined,
      serial_number: createForm.serial_number || undefined,
      install_location: createForm.install_location || undefined,
      install_date: createForm.install_date || undefined,
    })
    ElMessage.success('设备已创建')
    createVisible.value = false
    fetchData()
  } catch (e) {
    ElMessage.error(extractError(e))
  } finally {
    saving.value = false
  }
}

function openEdit(row) {
  Object.assign(editForm, {
    id: row.id,
    device_code: row.device_code,
    device_name: row.device_name || '',
    device_model: row.device_model || '',
    serial_number: row.serial_number || '',
    install_location: row.install_location || '',
    status: row.status,
  })
  editVisible.value = true
}

async function handleEdit() {
  saving.value = true
  try {
    await updateDevice(editForm.id, {
      device_name: editForm.device_name || undefined,
      device_model: editForm.device_model || undefined,
      serial_number: editForm.serial_number || undefined,
      install_location: editForm.install_location || undefined,
      status: editForm.status,
    })
    ElMessage.success('设备已更新')
    editVisible.value = false
    fetchData()
  } catch (e) {
    ElMessage.error(extractError(e))
  } finally {
    saving.value = false
  }
}

function statusLabel(status) {
  return { online: '在线', offline: '离线', disabled: '已停用' }[status] || status
}

function statusType(status) {
  return { online: 'success', offline: 'info', disabled: 'danger' }[status] || 'info'
}

function formatTime(t) {
  return t ? new Date(t).toLocaleString() : ''
}
</script>

<style scoped>
.page { max-width: 1250px; }
.page-header { margin-bottom: 20px; display: flex; justify-content: space-between; align-items: flex-start; }
.page-header h3 { font-size: 20px; font-weight: 600; }
.subtitle { color: #909399; font-size: 13px; margin-top: 6px; }
.filters { display: flex; gap: 12px; margin-bottom: 16px; }
.pagination-wrap { display: flex; justify-content: flex-end; margin-top: 16px; }
</style>
