import { createRouter, createWebHistory } from 'vue-router'
import { getToken, getUser } from '../utils/token'

// 角色：operator(工厂操作员) / engineer(售后技术) / admin(平台管理员)
const routes = [
  {
    path: '/login',
    name: 'Login',
    component: () => import('../views/Login.vue'),
    meta: { guest: true },
  },
  {
    path: '/',
    component: () => import('../layouts/MainLayout.vue'),
    redirect: '/dashboard',
    children: [
      { path: 'dashboard', name: 'Dashboard', component: () => import('../views/Dashboard.vue'), meta: { title: '概览' } },
      { path: 'devices', name: 'Devices', component: () => import('../views/Devices.vue'), meta: { title: '设备' } },
      // 下面这些页面任何登录用户可见；能看哪些数据由后端按租户裁剪（operator 只看本租户），
      // 所以**不设** meta.roles——与 MainLayout 里那些不带 v-if 的菜单项保持一致。
      // 注意：详情页与列表页同前缀（/work-orders/123），MainLayout 的高亮靠前缀匹配认它们。
      { path: 'alerts', name: 'Alerts', component: () => import('../views/Alerts.vue'), meta: { title: '告警中心' } },
      { path: 'alerts/:id', name: 'AlertDetail', component: () => import('../views/AlertDetail.vue'), meta: { title: '告警详情' } },
      { path: 'diagnoses', name: 'Diagnoses', component: () => import('../views/Diagnoses.vue'), meta: { title: '故障诊断' } },
      { path: 'diagnoses/:id', name: 'DiagnosisDetail', component: () => import('../views/DiagnosisDetail.vue'), meta: { title: '诊断详情' } },
      { path: 'work-orders', name: 'WorkOrders', component: () => import('../views/WorkOrders.vue'), meta: { title: '维修工单' } },
      { path: 'work-orders/:id', name: 'WorkOrderDetail', component: () => import('../views/WorkOrderDetail.vue'), meta: { title: '工单详情' } },
      // 知识库的**写操作**（上传/删除/重索引）在页面内按角色隐藏，读对所有人开放
      { path: 'knowledge', name: 'Knowledge', component: () => import('../views/Knowledge.vue'), meta: { title: '知识库' } },
      // 聊天按用户私有（后端校验"是不是本人的会话"），所以这里也不设角色门槛。
      // 列表与具体会话是同一个组件，靠路由参数区分。
      { path: 'chat', name: 'Chat', component: () => import('../views/Chat.vue'), meta: { title: '诊断助手' } },
      { path: 'chat/:id', name: 'ChatSession', component: () => import('../views/Chat.vue'), meta: { title: '诊断助手' } },
      {
        path: 'users',
        name: 'Users',
        component: () => import('../views/Users.vue'),
        meta: { title: '用户管理', roles: ['engineer', 'admin'] },
      },
      {
        path: 'tenants',
        name: 'Tenants',
        component: () => import('../views/Tenants.vue'),
        meta: { title: '租户管理', roles: ['engineer', 'admin'] },
      },
      { path: 'settings', name: 'Settings', component: () => import('../views/Settings.vue'), meta: { title: '个人资料' } },
    ],
  },
  { path: '/:pathMatch(.*)*', name: 'NotFound', component: () => import('../views/NotFound.vue') },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
})

router.beforeEach((to) => {
  const token = getToken()
  if (to.meta.guest && token) return '/dashboard'
  if (!to.meta.guest && !token) return '/login'
  const roles = to.meta.roles
  if (roles) {
    const role = getUser()?.role
    if (role !== 'admin' && !roles.includes(role)) return '/dashboard'
  }
  return true
})

export default router
