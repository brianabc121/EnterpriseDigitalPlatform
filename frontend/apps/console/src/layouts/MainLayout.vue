<script setup lang="ts">
import {
  ArrowDown,
  Avatar,
  ChatDotRound,
  Clock,
  Connection,
  DataLine,
  HomeFilled,
  MagicStick,
  Promotion,
  Reading,
  Setting,
  Tickets,
  User,
} from '@element-plus/icons-vue'
import { computed, ref, type Component } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { visibleMenus, type MenuIcon } from '../menu'
import { useAuthStore } from '../stores/auth'
import { useWorkbenchStore } from '../stores/workbench'

const auth = useAuthStore()
const route = useRoute()
const router = useRouter()

const icons: Record<MenuIcon, Component> = {
  home: HomeFilled,
  chat: ChatDotRound,
  history: Clock,
  ticket: Tickets,
  user: User,
  reading: Reading,
  ai: MagicStick,
  avatar: Avatar,
  chart: DataLine,
  integration: Connection,
  broadcast: Promotion,
  setting: Setting,
}
const menus = computed(() => visibleMenus(auth.permissions, auth.me?.features ?? {}))
const noticeClosed = ref(sessionStorage.getItem('edp:billing-notice') === auth.me?.billing_notice)

function closeNotice(): void {
  noticeClosed.value = true
  sessionStorage.setItem('edp:billing-notice', auth.me?.billing_notice ?? '')
}

async function logout(): Promise<void> {
  // 先离线：分配给自己但还没回复的会话立即退回队列，不必等心跳超时。
  await useWorkbenchStore().stop()
  await auth.logout()
  await router.push({ name: 'login' })
}
</script>

<template>
  <el-container class="layout">
    <el-aside width="208px" class="aside">
      <div class="brand">EDP 智能客服</div>
      <el-menu :default-active="route.path" router class="menu" data-testid="main-menu">
        <el-menu-item v-for="item in menus" :key="item.name" :index="item.path">
          <el-icon><component :is="icons[item.icon]" /></el-icon>
          <span>{{ item.title }}</span>
        </el-menu-item>
      </el-menu>
    </el-aside>
    <el-container>
      <el-header class="header">
        <span class="tenant">{{ auth.me?.tenant.name }}</span>
        <el-dropdown @command="logout">
          <span class="user">
            {{ auth.me?.display_name }}
            <el-icon><ArrowDown /></el-icon>
          </span>
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item command="logout">退出登录</el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
      </el-header>
      <el-main>
        <el-alert
          v-if="auth.me?.billing_notice && !noticeClosed"
          :title="auth.me.billing_notice"
          type="warning"
          show-icon
          class="billing-notice"
          data-testid="billing-banner"
          @close="closeNotice"
        >
          <router-link to="/settings" class="link">查看套餐</router-link>
        </el-alert>
        <router-view />
      </el-main>
    </el-container>
  </el-container>
</template>

<style scoped>
.billing-notice {
  margin-bottom: 12px;
}

.link {
  font-size: 12px;
}

.layout {
  height: 100%;
}

.aside {
  background: var(--el-bg-color);
  border-right: 1px solid var(--el-border-color-light);
}

.brand {
  height: 56px;
  line-height: 56px;
  padding: 0 20px;
  font-weight: 600;
  font-size: 16px;
  color: var(--el-color-primary);
}

.menu {
  border-right: none;
}

.header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  background: var(--el-bg-color);
  border-bottom: 1px solid var(--el-border-color-light);
}

.tenant {
  font-weight: 500;
}

.user {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  cursor: pointer;
}
</style>
