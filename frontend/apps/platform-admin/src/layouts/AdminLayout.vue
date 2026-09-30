<script setup lang="ts">
import { useRoute, useRouter } from 'vue-router'

import { useAuthStore } from '../stores/auth'

const auth = useAuthStore()
const route = useRoute()
const router = useRouter()

const MENU = [
  { name: 'tenants', label: '租户' },
  { name: 'plans', label: '套餐' },
  { name: 'invoices', label: '账单' },
  { name: 'channels', label: '渠道授权' },
  { name: 'providers', label: '模型供应商' },
  { name: 'prompts', label: '提示词' },
  { name: 'llm-usage', label: '大模型用量' },
  { name: 'content', label: '内容安全' },
  { name: 'health', label: '系统健康' },
  { name: 'audit', label: '审计日志' },
  { name: 'deletions', label: '删除记录' },
  { name: 'settings', label: '平台设置' },
  { name: 'security', label: '账号安全' },
]

function active(): string {
  return route.name === 'tenant' ? 'tenants' : String(route.name ?? '')
}

async function go(name: string): Promise<void> {
  await router.push({ name })
}

async function logout(): Promise<void> {
  auth.logout()
  await router.push({ name: 'login' })
}
</script>

<template>
  <el-container class="layout">
    <el-header class="header">
      <span class="brand">EDP 运营后台</span>
      <span class="user">
        {{ auth.me?.display_name }}
        <el-tag disable-transitions v-if="auth.me?.mfa_enabled" size="small" type="success">已启用二次验证</el-tag>
        <el-button link type="primary" @click="logout">退出</el-button>
      </span>
    </el-header>
    <el-container class="body">
      <el-aside width="160px" class="aside">
        <el-menu :default-active="active()" data-testid="platform-menu" @select="go">
          <el-menu-item
            v-for="item in MENU"
            :key="item.name"
            :index="item.name"
            :disabled="auth.needsMfaSetup && item.name !== 'security'"
            :data-testid="`menu-${item.name}`"
          >
            {{ item.label }}
          </el-menu-item>
        </el-menu>
      </el-aside>
      <el-main>
        <el-alert
          v-if="auth.needsMfaSetup"
          type="warning"
          :closable="false"
          show-icon
          title="平台要求运营账号启用二次验证，请先完成设置"
          class="notice"
        />
        <router-view />
      </el-main>
    </el-container>
  </el-container>
</template>

<style scoped>
.layout {
  height: 100%;
}

.header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  background: var(--el-bg-color);
  border-bottom: 1px solid var(--el-border-color-light);
}

.body {
  min-height: 0;
}

.aside {
  background: var(--el-bg-color);
  border-right: 1px solid var(--el-border-color-light);
}

.aside :deep(.el-menu) {
  border-right: none;
}

.brand {
  font-weight: 600;
  color: var(--el-color-primary);
}

.user {
  display: inline-flex;
  align-items: center;
  gap: 8px;
}

.notice {
  margin-bottom: 16px;
}
</style>
