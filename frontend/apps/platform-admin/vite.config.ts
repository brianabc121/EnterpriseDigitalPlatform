import vue from '@vitejs/plugin-vue'
import Components from 'unplugin-vue-components/vite'
import { ElementPlusResolver } from 'unplugin-vue-components/resolvers'
import { defineConfig } from 'vite'

export default defineConfig({
  plugins: [
    vue(),
    // Element Plus 按需引入：模板里用到的组件自动导入（样式整体引入，见 main.ts）。
    Components({ resolvers: [ElementPlusResolver({ importStyle: false })], dts: false }),
  ],
  server: {
    port: 5174,
    // 接口传输加密的握手在 /api/v1/transport（§25.15）。
    proxy: { '/platform': 'http://localhost:8000', '/api/v1/transport': 'http://localhost:8000' },
  },
})
