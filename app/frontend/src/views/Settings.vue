<template>
  <div class="page">
    <div class="page-header">
      <h3>个人资料</h3>
    </div>

    <el-card shadow="never" class="card">
      <el-descriptions :column="2" border>
        <el-descriptions-item label="用户名">{{ profile.username }}</el-descriptions-item>
        <el-descriptions-item label="角色">{{ roleLabel }}</el-descriptions-item>
        <el-descriptions-item label="所属租户">
          {{ profile.tenant_id ? `#${profile.tenant_id}` : '—' }}
        </el-descriptions-item>
        <el-descriptions-item label="状态">
          <el-tag :type="profile.status === 'active' ? 'success' : 'danger'" size="small">
            {{ profile.status === 'active' ? '正常' : '已停用' }}
          </el-tag>
        </el-descriptions-item>
      </el-descriptions>
    </el-card>

    <el-card shadow="never" class="card">
      <template #header>修改资料</template>
      <el-form :model="form" label-width="80px" style="max-width: 420px">
        <el-form-item label="姓名">
          <el-input v-model="form.real_name" placeholder="真实姓名" />
        </el-form-item>
        <el-form-item label="邮箱">
          <el-input v-model="form.email" placeholder="用于接收通知（可选）" />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :loading="saving" @click="handleSave">保存</el-button>
        </el-form-item>
      </el-form>
      <el-alert
        type="info"
        :closable="false"
        show-icon
        title="角色与所属租户由售后技术人员管理，不能自助修改；忘记密码请联系售后重置。"
      />
    </el-card>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { extractError } from '../api/index'
import { getCurrentUser, updateProfile } from '../api/auth'
import { setUser } from '../utils/token'

const profile = ref({})
const saving = ref(false)
const form = reactive({ real_name: '', email: '' })

const roleLabel = computed(
  () =>
    ({ operator: '工厂操作员', engineer: '售后技术', admin: '管理员' }[profile.value?.role] ||
    profile.value?.role),
)

onMounted(load)

async function load() {
  const res = await getCurrentUser()
  profile.value = res.data.data
  form.real_name = profile.value.real_name || ''
  form.email = profile.value.email || ''
}

async function handleSave() {
  saving.value = true
  try {
    const res = await updateProfile({
      real_name: form.real_name || undefined,
      email: form.email || undefined,
    })
    profile.value = res.data.data
    setUser(res.data.data) // 同步本地缓存的展示信息
    ElMessage.success('资料已更新')
  } catch (e) {
    ElMessage.error(extractError(e))
  } finally {
    saving.value = false
  }
}
</script>

<style scoped>
.page { max-width: 900px; }
.page-header { margin-bottom: 20px; }
.page-header h3 { font-size: 20px; font-weight: 600; }
.card { margin-bottom: 16px; }
</style>
