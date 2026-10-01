import 'element-plus/dist/index.css'
import './styles.css'

import { vLoading } from 'element-plus'
import { createPinia } from 'pinia'
import { createApp } from 'vue'

import App from './App.vue'
import { router } from './router'

// Element Plus 组件按需引入（vite.config.ts）；指令手工注册，中文语言包在 App.vue 的 el-config-provider。
createApp(App).use(createPinia()).use(router).directive('loading', vLoading).mount('#app')
