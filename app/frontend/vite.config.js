import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 后端在 http://127.0.0.1:8000（无 /api 前缀，靠 CORS 直连）。
// dev 端口用 5174：5173 常被另一个项目占用。
export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5174,
  },
})
