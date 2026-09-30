import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vite'

export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5174,
    // 接口传输加密的握手在 /api/v1/transport（§25.15）。
    proxy: { '/platform': 'http://localhost:8000', '/api/v1/transport': 'http://localhost:8000' },
  },
})
