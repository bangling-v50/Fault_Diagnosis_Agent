import axios from 'axios'
import { ElMessage } from 'element-plus'
import { getToken, removeToken, removeUser } from '../utils/token'

// 后端 FastAPI 路由直接挂在根上（没有 /api 前缀），靠 CORS 直连。
const api = axios.create({
  baseURL: 'http://127.0.0.1:8000',
  timeout: 30000,
})

api.interceptors.request.use((config) => {
  const token = getToken()
  if (token) {
    config.headers.Authorization = `Bearer ${token}`
  }
  return config
})

api.interceptors.response.use(
  (response) => response,
  (error) => {
    const status = error.response?.status
    if (status === 401) {
      removeToken()
      removeUser()
      if (!window.location.pathname.startsWith('/login')) {
        window.location.href = '/login'
      }
    } else if (status === 403) {
      ElMessage.error(extractError(error) || '无权限执行此操作')
    }
    return Promise.reject(error)
  },
)

/**
 * 从后端统一错误响应里取人话。
 * 形状：{ code, message, data: null, error }；detail 可能是字符串或 { code, message }。
 */
export function extractError(error) {
  const body = error?.response?.data
  if (!body) return error?.message || '网络异常'
  const detail = body.detail
  if (typeof detail === 'string') return detail
  if (detail && typeof detail === 'object') return detail.message || detail.code
  return body.message || '请求失败'
}

export default api
