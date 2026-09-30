import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vite'

// 只用于单元测试（组件由各应用的 Vite 直接编译）。
export default defineConfig({ plugins: [vue()] })
