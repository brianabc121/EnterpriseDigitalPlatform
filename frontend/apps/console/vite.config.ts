import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vite'

export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5173,
    // 同源代理：刷新令牌 Cookie 只对同源请求生效。
    proxy: { '/api': 'http://localhost:8000' },
  },
})
