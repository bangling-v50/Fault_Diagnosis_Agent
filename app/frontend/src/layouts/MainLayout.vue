<template>
  <el-container class="app-layout">
    <el-aside :width="isCollapse ? '64px' : '220px'" class="app-sidebar">
      <div class="logo" @click="$router.push('/dashboard')">
        <span v-if="!isCollapse" class="logo-text">FaDia Agent</span>
        <span v-else class="logo-short">FD</span>
      </div>
      <el-menu
        :default-active="activeMenu"
        :collapse="isCollapse"
        router
        background-color="#1a1b2e"
        text-color="#a0a4b8"
        active-text-color="#ffffff"
      >
        <el-menu-item index="/dashboard">
          <el-icon><Odometer /></el-icon>
          <span>概览</span>
        </el-menu-item>
        <el-menu-item index="/devices">
          <el-icon><Monitor /></el-icon>
          <span>设备</span>
        </el-menu-item>
        <el-menu-item index="/alerts">
          <el-icon><Bell /></el-icon>
          <span>告警中心</span>
        </el-menu-item>
        <el-menu-item index="/diagnoses">
          <el-icon><FirstAidKit /></el-icon>
          <span>故障诊断</span>
        </el-menu-item>
        <el-menu-item index="/work-orders">
          <el-icon><Tools /></el-icon>
          <span>维修工单</span>
        </el-menu-item>
        <el-menu-item index="/knowledge">
          <el-icon><Reading /></el-icon>
          <span>知识库</span>
        </el-menu-item>
        <el-menu-item index="/chat">
          <el-icon><ChatDotRound /></el-icon>
          <span>诊断助手</span>
        </el-menu-item>
        <el-menu-item v-if="canManage" index="/users">
          <el-icon><User /></el-icon>
          <span>用户管理</span>
        </el-menu-item>
        <el-menu-item v-if="canManage" index="/tenants">
          <el-icon><OfficeBuilding /></el-icon>
          <span>租户管理</span>
        </el-menu-item>
        <el-menu-item index="/settings">
          <el-icon><Setting /></el-icon>
          <span>个人资料</span>
        </el-menu-item>
      </el-menu>
    </el-aside>

    <el-container>
      <el-header class="app-topbar">
        <div class="topbar-left">
          <el-icon class="collapse-btn" @click="isCollapse = !isCollapse">
            <Fold v-if="!isCollapse" />
            <Expand v-else />
          </el-icon>
        </div>
        <div class="topbar-right">
          <span class="user-name">{{ user?.real_name || user?.username }}</span>
          <span class="user-role">{{ roleLabel }}</span>
          <el-button text @click="handleLogout">退出</el-button>
        </div>
      </el-header>

      <el-main class="app-main">
        <router-view />
      </el-main>
    </el-container>
  </el-container>
</template>

<script setup>
import { computed, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { logout } from '../api/auth'
import { getUser, removeToken, removeUser } from '../utils/token'

const route = useRoute()
const router = useRouter()
const isCollapse = ref(false)
const user = ref(getUser())

// engineer / admin 才看得到用户与租户管理
const canManage = computed(() => ['engineer', 'admin'].includes(user.value?.role))

const roleLabel = computed(
  () =>
    ({ operator: '工厂操作员', engineer: '售后技术', admin: '管理员' }[user.value?.role] ||
    user.value?.role),
)

// 菜单高亮：取**第一段路径**而不是整个 path。
// 详情页（/work-orders/123、/alerts/7、/diagnoses/9）的第一段与列表页相同，
// 直接用 route.path 匹配菜单项的 index 会让详情页整片菜单都不亮。
const activeMenu = computed(() => {
  const segment = route.path.split('/').filter(Boolean)[0]
  return segment ? `/${segment}` : '/dashboard'
})

async function handleLogout() {
  try {
    await logout() // 撤销服务端会话；token 可能已失效，失败也无所谓
  } catch {
    /* ignore */
  }
  removeToken()
  removeUser()
  ElMessage.success('已退出登录')
  router.push('/login')
}
</script>

<style scoped>
.app-layout { height: 100vh; }

.app-sidebar {
  background: #1a1b2e;
  overflow: hidden;
  transition: width 0.2s;
}

.logo {
  height: 56px;
  display: flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  border-bottom: 1px solid rgba(255, 255, 255, 0.06);
}
.logo-text { color: #fff; font-size: 17px; font-weight: 700; letter-spacing: 1px; }
.logo-short { color: #fff; font-size: 18px; font-weight: 700; }

.el-menu { border-right: none; }

.app-topbar {
  height: 56px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  background: #fff;
  border-bottom: 1px solid #ebeef5;
  padding: 0 20px;
}

.collapse-btn { font-size: 20px; cursor: pointer; color: #606266; }
.collapse-btn:hover { color: #409eff; }

.topbar-right { display: flex; align-items: center; gap: 12px; }
.user-name { font-size: 14px; color: #303133; }
.user-role {
  font-size: 12px;
  color: #909399;
  background: #f0f2f5;
  padding: 2px 8px;
  border-radius: 4px;
}

.app-main { background: #f5f6fa; padding: 20px; overflow-y: auto; }
</style>
