<template>
  <div class="login-wrapper">
    <div class="login-card">
      <h2 class="login-title">FaDia Agent</h2>
      <p class="login-subtitle">工业设备故障智能诊断助手</p>
      <el-form ref="formRef" :model="form" :rules="rules" size="large" @keyup.enter="handleLogin">
        <el-form-item prop="username">
          <el-input v-model="form.username" placeholder="用户名" prefix-icon="User" />
        </el-form-item>
        <el-form-item prop="password">
          <el-input
            v-model="form.password"
            type="password"
            placeholder="密码"
            prefix-icon="Lock"
            show-password
          />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :loading="loading" class="login-btn" @click="handleLogin">
            登 录
          </el-button>
        </el-form-item>
      </el-form>
      <p class="login-hint">账号由售后技术人员创建，如有疑问请联系售后</p>
    </div>
  </div>
</template>

<script setup>
import { reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { login } from '../api/auth'
import { extractError } from '../api/index'
import { setToken, setUser } from '../utils/token'

const router = useRouter()
const loading = ref(false)
const form = reactive({ username: '', password: '' })
const rules = {
  username: [{ required: true, message: '请输入用户名', trigger: 'blur' }],
  password: [{ required: true, message: '请输入密码', trigger: 'blur' }],
}

async function handleLogin() {
  loading.value = true
  try {
    // 登录响应已带用户信息，无需再请求 /auth/me
    const res = await login(form.username, form.password)
    const { access_token, user } = res.data.data
    setToken(access_token)
    setUser(user)
    ElMessage.success('登录成功')
    router.push('/dashboard')
  } catch (e) {
    ElMessage.error(extractError(e))
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
.login-wrapper {
  height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  background: linear-gradient(135deg, #1a1b2e 0%, #2d3a5c 100%);
}

.login-card {
  width: 400px;
  padding: 48px 40px 32px;
  background: #fff;
  border-radius: 8px;
  box-shadow: 0 8px 32px rgba(0, 0, 0, 0.2);
}

.login-title {
  text-align: center;
  font-size: 24px;
  font-weight: 700;
  color: #1a1b2e;
  margin-bottom: 8px;
  letter-spacing: 1px;
}

.login-subtitle {
  text-align: center;
  font-size: 14px;
  color: #909399;
  margin-bottom: 32px;
}

.login-btn { width: 100%; }

.login-hint {
  text-align: center;
  font-size: 12px;
  color: #c0c4cc;
  margin-top: 8px;
}
</style>
