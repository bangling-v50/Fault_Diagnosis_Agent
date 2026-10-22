<template>
  <div class="page">
    <div class="page-header">
      <div>
        <h3>用户管理</h3>
        <p class="subtitle">
          {{ isAdmin ? '管理员可管理全部账号' : '售后技术只能创建与管理工厂操作员' }}
        </p>
      </div>
      <el-button type="primary" @click="openCreate">新建账号</el-button>
    </div>

    <el-card shadow="never">
      <div class="filters">
        <el-input
          v-model="keyword"
          placeholder="搜索用户名 / 姓名 / 邮箱"
          clearable
          style="width: 240px"
          @keyup.enter="fetchData"
        />
        <el-select v-if="isAdmin" v-model="roleFilter" placeholder="角色" clearable style="width: 150px" @change="fetchData">
          <el-option v-for="r in ROLE_OPTIONS" :key="r.value" :label="r.label" :value="r.value" />
        </el-select>
        <el-select v-model="statusFilter" placeholder="状态" clearable style="width: 130px" @change="fetchData">
          <el-option label="正常" value="active" />
          <el-option label="已停用" value="disabled" />
        </el-select>
        <el-button @click="fetchData">查询</el-button>
      </div>

      <el-table :data="rows" stripe v-loading="loading" style="width: 100%">
        <el-table-column prop="username" label="用户名" width="140" />
        <el-table-column prop="real_name" label="姓名" width="110" />
        <el-table-column label="角色" width="120">
          <template #default="{ row }">{{ roleLabel(row.role) }}</template>
        </el-table-column>
        <el-table-column label="所属租户" width="110">
          <template #default="{ row }">{{ row.tenant_id ? `#${row.tenant_id}` : '—' }}</template>
        </el-table-column>
        <el-table-column prop="email" label="邮箱" min-width="170" />
        <el-table-column label="状态" width="90">
          <template #default="{ row }">
            <el-tag :type="row.status === 'active' ? 'success' : 'danger'" size="small">
              {{ row.status === 'active' ? '正常' : '已停用' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="创建时间" width="170">
          <template #default="{ row }">{{ formatTime(row.created_at) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="230" fixed="right">
          <template #default="{ row }">
            <el-button text type="primary" size="small" @click="openEdit(row)">编辑</el-button>
            <el-button text size="small" @click="openReset(row)">重置密码</el-button>
            <el-button
              v-if="row.status === 'active'"
              text
              type="danger"
              size="small"
              @click="toggle(row)"
            >
              停用
            </el-button>
            <el-button v-else text type="success" size="small" @click="toggle(row)">启用</el-button>
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

    <!-- 新建 -->
    <el-dialog v-model="createVisible" title="新建账号" width="480px">
      <el-form :model="createForm" label-width="90px">
        <el-form-item label="用户名">
          <el-input v-model="createForm.username" />
        </el-form-item>
        <el-form-item label="初始密码">
          <el-input v-model="createForm.password" placeholder="至少 8 位" show-password />
        </el-form-item>
        <el-form-item label="角色">
          <el-select v-model="createForm.role" style="width: 100%" :disabled="!isAdmin">
            <el-option
              v-for="r in creatableRoles"
              :key="r.value"
              :label="r.label"
              :value="r.value"
            />
          </el-select>
        </el-form-item>
        <el-form-item v-if="createForm.role === 'operator'" label="所属租户">
          <el-select v-model="createForm.tenant_id" style="width: 100%" placeholder="选择租户">
            <el-option
              v-for="t in tenants"
              :key="t.id"
              :label="`${t.tenant_name}（${t.tenant_code}）`"
              :value="t.id"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="姓名">
          <el-input v-model="createForm.real_name" />
        </el-form-item>
        <el-form-item label="邮箱">
          <el-input v-model="createForm.email" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="createVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="handleCreate">创建</el-button>
      </template>
    </el-dialog>

    <!-- 编辑 -->
    <el-dialog v-model="editVisible" title="编辑账号" width="460px">
      <el-form :model="editForm" label-width="80px">
        <el-form-item label="用户名">
          <el-input :model-value="editForm.username" disabled />
        </el-form-item>
        <el-form-item label="姓名">
          <el-input v-model="editForm.real_name" />
        </el-form-item>
        <el-form-item label="邮箱">
          <el-input v-model="editForm.email" />
        </el-form-item>
        <el-form-item v-if="isAdmin" label="角色">
          <el-select v-model="editForm.role" style="width: 100%">
            <el-option v-for="r in ROLE_OPTIONS" :key="r.value" :label="r.label" :value="r.value" />
          </el-select>
        </el-form-item>
        <el-form-item v-if="isAdmin && editForm.role === 'operator'" label="所属租户">
          <el-select v-model="editForm.tenant_id" style="width: 100%" placeholder="选择租户">
            <el-option
              v-for="t in tenants"
              :key="t.id"
              :label="`${t.tenant_name}（${t.tenant_code}）`"
              :value="t.id"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="状态">
          <el-select v-model="editForm.status" style="width: 100%">
            <el-option label="正常" value="active" />
            <el-option label="已停用" value="disabled" />
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="editVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="handleEdit">保存</el-button>
      </template>
    </el-dialog>

    <!-- 重置密码 -->
    <el-dialog v-model="resetVisible" title="重置密码" width="420px">
      <el-form label-width="80px">
        <el-form-item label="账号">
          <el-input :model-value="resetForm.username" disabled />
        </el-form-item>
        <el-form-item label="新密码">
          <el-input v-model="resetForm.password" placeholder="至少 8 位" show-password />
        </el-form-item>
      </el-form>
      <p class="dialog-hint">重置后该账号的所有登录会话会立即失效。</p>
      <template #footer>
        <el-button @click="resetVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="handleReset">重置</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { extractError } from '../api/index'
import { getTenants } from '../api/tenants'
import {
  createUser,
  disableUser,
  enableUser,
  getUsers,
  resetPassword,
  updateUser,
} from '../api/users'
import { getUser } from '../utils/token'

const ROLE_OPTIONS = [
  { label: '工厂操作员', value: 'operator' },
  { label: '售后技术', value: 'engineer' },
  { label: '管理员', value: 'admin' },
]

const me = ref(getUser())
const isAdmin = computed(() => me.value?.role === 'admin')
// engineer 只能建 operator；admin 可建任意角色
const creatableRoles = computed(() =>
  isAdmin.value ? ROLE_OPTIONS : ROLE_OPTIONS.filter((r) => r.value === 'operator'),
)

const loading = ref(false)
const saving = ref(false)
const rows = ref([])
const tenants = ref([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const keyword = ref('')
const roleFilter = ref('')
const statusFilter = ref('')

const createVisible = ref(false)
const editVisible = ref(false)
const resetVisible = ref(false)

const createForm = reactive({
  username: '', password: '', role: 'operator', tenant_id: null, real_name: '', email: '',
})
const editForm = reactive({
  id: null, username: '', real_name: '', email: '', role: 'operator', tenant_id: null, status: 'active',
})
const resetForm = reactive({ id: null, username: '', password: '' })

onMounted(async () => {
  fetchData()
  try {
    tenants.value = (await getTenants({ page: 1, page_size: 100 })).data.data.items
  } catch {
    /* 忽略 */
  }
})

async function fetchData() {
  loading.value = true
  try {
    const res = await getUsers({
      page: page.value,
      page_size: pageSize.value,
      role: roleFilter.value || undefined,
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
    username: '', password: '', role: 'operator', tenant_id: null, real_name: '', email: '',
  })
  createVisible.value = true
}

async function handleCreate() {
  if (!createForm.username || !createForm.password) {
    ElMessage.warning('请填写用户名与初始密码')
    return
  }
  saving.value = true
  try {
    await createUser({
      username: createForm.username,
      password: createForm.password,
      role: createForm.role,
      tenant_id: createForm.role === 'operator' ? createForm.tenant_id : undefined,
      real_name: createForm.real_name || undefined,
      email: createForm.email || undefined,
    })
    ElMessage.success('账号已创建')
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
    username: row.username,
    real_name: row.real_name || '',
    email: row.email || '',
    role: row.role,
    tenant_id: row.tenant_id,
    status: row.status,
  })
  editVisible.value = true
}

async function handleEdit() {
  saving.value = true
  try {
    const payload = {
      real_name: editForm.real_name || undefined,
      email: editForm.email || undefined,
      status: editForm.status,
    }
    if (isAdmin.value) {
      payload.role = editForm.role
      if (editForm.role === 'operator') payload.tenant_id = editForm.tenant_id
    }
    await updateUser(editForm.id, payload)
    ElMessage.success('账号已更新')
    editVisible.value = false
    fetchData()
  } catch (e) {
    ElMessage.error(extractError(e))
  } finally {
    saving.value = false
  }
}

function openReset(row) {
  Object.assign(resetForm, { id: row.id, username: row.username, password: '' })
  resetVisible.value = true
}

async function handleReset() {
  if (!resetForm.password) {
    ElMessage.warning('请输入新密码')
    return
  }
  saving.value = true
  try {
    await resetPassword(resetForm.id, resetForm.password)
    ElMessage.success('密码已重置，该账号旧会话已失效')
    resetVisible.value = false
  } catch (e) {
    ElMessage.error(extractError(e))
  } finally {
    saving.value = false
  }
}

async function toggle(row) {
  try {
    if (row.status === 'active') {
      await disableUser(row.id)
      ElMessage.success('账号已停用')
    } else {
      await enableUser(row.id)
      ElMessage.success('账号已启用')
    }
    fetchData()
  } catch (e) {
    ElMessage.error(extractError(e))
  }
}

function roleLabel(role) {
  return ROLE_OPTIONS.find((r) => r.value === role)?.label || role
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
.dialog-hint { color: #e6a23c; font-size: 13px; margin: 0; }
</style>
