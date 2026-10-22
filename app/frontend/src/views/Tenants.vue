<template>
  <div class="page">
    <div class="page-header">
      <h3>租户管理</h3>
      <el-button type="primary" @click="openCreate">新建租户</el-button>
    </div>

    <el-card shadow="never">
      <div class="filters">
        <el-input
          v-model="keyword"
          placeholder="搜索编码 / 名称"
          clearable
          style="width: 220px"
          @keyup.enter="fetchData"
        />
        <el-select v-model="statusFilter" placeholder="状态" clearable style="width: 130px" @change="fetchData">
          <el-option label="启用" value="active" />
          <el-option label="停用" value="inactive" />
        </el-select>
        <el-button @click="fetchData">查询</el-button>
      </div>

      <el-table :data="rows" stripe v-loading="loading" style="width: 100%">
        <el-table-column prop="tenant_code" label="租户编码" width="150" />
        <el-table-column prop="tenant_name" label="租户名称" min-width="180" />
        <el-table-column prop="region" label="区域" width="110" />
        <el-table-column prop="contact_person" label="联系人" width="110" />
        <el-table-column prop="contact_phone" label="联系电话" width="140" />
        <el-table-column label="状态" width="90">
          <template #default="{ row }">
            <el-tag :type="row.status === 'active' ? 'success' : 'info'" size="small">
              {{ row.status === 'active' ? '启用' : '停用' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="创建时间" width="170">
          <template #default="{ row }">{{ formatTime(row.created_at) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="160" fixed="right">
          <template #default="{ row }">
            <el-button text type="primary" size="small" @click="openEdit(row)">编辑</el-button>
            <el-button
              v-if="row.status === 'active'"
              text
              type="danger"
              size="small"
              @click="toggle(row, 'deactivate')"
            >
              停用
            </el-button>
            <el-button v-else text type="success" size="small" @click="toggle(row, 'activate')">
              启用
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

    <el-dialog v-model="dialogVisible" :title="form.id ? '编辑租户' : '新建租户'" width="480px">
      <el-form :model="form" label-width="90px">
        <el-form-item label="租户编码">
          <el-input v-model="form.tenant_code" :disabled="!!form.id" placeholder="唯一编码，如 acme-factory" />
        </el-form-item>
        <el-form-item label="租户名称">
          <el-input v-model="form.tenant_name" />
        </el-form-item>
        <el-form-item label="区域">
          <el-input v-model="form.region" />
        </el-form-item>
        <el-form-item label="地址">
          <el-input v-model="form.address" type="textarea" :rows="2" />
        </el-form-item>
        <el-form-item label="联系人">
          <el-input v-model="form.contact_person" />
        </el-form-item>
        <el-form-item label="联系电话">
          <el-input v-model="form.contact_phone" />
        </el-form-item>
        <el-form-item v-if="form.id" label="状态">
          <el-select v-model="form.status" style="width: 100%">
            <el-option label="启用" value="active" />
            <el-option label="停用" value="inactive" />
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="handleSave">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { extractError } from '../api/index'
import {
  activateTenant,
  createTenant,
  deactivateTenant,
  getTenants,
  updateTenant,
} from '../api/tenants'

const loading = ref(false)
const saving = ref(false)
const rows = ref([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const keyword = ref('')
const statusFilter = ref('')

const dialogVisible = ref(false)
const form = reactive({
  id: null,
  tenant_code: '',
  tenant_name: '',
  region: '',
  address: '',
  contact_person: '',
  contact_phone: '',
  status: 'active',
})

onMounted(fetchData)

async function fetchData() {
  loading.value = true
  try {
    const res = await getTenants({
      page: page.value,
      page_size: pageSize.value,
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

function resetForm() {
  Object.assign(form, {
    id: null, tenant_code: '', tenant_name: '', region: '',
    address: '', contact_person: '', contact_phone: '', status: 'active',
  })
}

function openCreate() {
  resetForm()
  dialogVisible.value = true
}

function openEdit(row) {
  Object.assign(form, {
    id: row.id,
    tenant_code: row.tenant_code,
    tenant_name: row.tenant_name || '',
    region: row.region || '',
    address: row.address || '',
    contact_person: row.contact_person || '',
    contact_phone: row.contact_phone || '',
    status: row.status,
  })
  dialogVisible.value = true
}

async function handleSave() {
  saving.value = true
  try {
    if (form.id) {
      await updateTenant(form.id, {
        tenant_name: form.tenant_name || undefined,
        region: form.region || undefined,
        address: form.address || undefined,
        contact_person: form.contact_person || undefined,
        contact_phone: form.contact_phone || undefined,
        status: form.status,
      })
      ElMessage.success('租户已更新')
    } else {
      await createTenant({
        tenant_code: form.tenant_code,
        tenant_name: form.tenant_name,
        region: form.region || undefined,
        address: form.address || undefined,
        contact_person: form.contact_person || undefined,
        contact_phone: form.contact_phone || undefined,
      })
      ElMessage.success('租户已创建')
    }
    dialogVisible.value = false
    fetchData()
  } catch (e) {
    ElMessage.error(extractError(e))
  } finally {
    saving.value = false
  }
}

async function toggle(row, action) {
  try {
    if (action === 'deactivate') {
      await deactivateTenant(row.id)
      ElMessage.success('租户已停用（其下账号将无法登录）')
    } else {
      await activateTenant(row.id)
      ElMessage.success('租户已启用')
    }
    fetchData()
  } catch (e) {
    ElMessage.error(extractError(e))
  }
}

function formatTime(t) {
  return t ? new Date(t).toLocaleString() : ''
}
</script>

<style scoped>
.page { max-width: 1200px; }
.page-header { margin-bottom: 20px; display: flex; justify-content: space-between; align-items: center; }
.page-header h3 { font-size: 20px; font-weight: 600; }
.filters { display: flex; gap: 12px; margin-bottom: 16px; }
.pagination-wrap { display: flex; justify-content: flex-end; margin-top: 16px; }
</style>
