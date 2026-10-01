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
    port: 5173,
    // 同源代理：刷新令牌 Cookie 只对同源请求生效。
    proxy: { '/api': 'http://localhost:8000' },
  },
})
