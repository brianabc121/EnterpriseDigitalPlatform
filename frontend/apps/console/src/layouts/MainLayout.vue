<script setup lang="ts">
import {
  ArrowDown,
  Avatar,
  ChatDotRound,
  Clock,
  Connection,
  DataLine,
  Document,
  Goods,
  HomeFilled,
  MagicStick,
  Promotion,
  Reading,
  Setting,
  ShoppingCart,
  Tickets,
  User,
} from '@element-plus/icons-vue'
import { computed, onBeforeUnmount, onMounted, ref, type Component } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { api } from '../api'
import PasswordDialog from '../components/account/PasswordDialog.vue'
import NotificationBell from '../components/layout/NotificationBell.vue'
import { visibleMenus, type MenuIcon } from '../menu'
import { ORDERS_CHANGED } from '../orders'
import { TODOS_CHANGED } from '../todos'
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
  order: ShoppingCart,
  goods: Goods,
  user: User,
  reading: Reading,
  ai: MagicStick,
  avatar: Avatar,
  chart: DataLine,
  integration: Connection,
  broadcast: Promotion,
  audit: Document,
  setting: Setting,
}
const menus = computed(() => visibleMenus(auth.permissions, auth.me?.features ?? {}))
const noticeClosed = ref(sessionStorage.getItem('edp:billing-notice') === auth.me?.billing_notice)

function closeNotice(): void {
  noticeClosed.value = true
  sessionStorage.setItem('edp:billing-notice', auth.me?.billing_notice ?? '')
}

const passwordOpen = ref(false)

// 待办菜单的角标：等我确认的加上我已逾期的，每分钟刷新一次；待办有变化时立即刷新。
const TODO_POLL_MS = 60_000
const todoBadge = ref(0)
let todoTimer: ReturnType<typeof setInterval> | undefined

async function loadTodoBadge(): Promise<void> {
  if (!auth.can('todo:read')) return
  const { data } = await api.GET('/api/v1/todos/counts')
  if (data) todoBadge.value = data.pending + data.overdue
}

function onTodosChanged(): void {
  void loadTodoBadge()
}

// 订单菜单的角标：待审核的订单（有审核权限时），与待办一起刷新；订单有变化时立即刷新。
const orderBadge = ref(0)

async function loadOrderBadge(): Promise<void> {
  if (!auth.can('order:review') || auth.me?.features?.orders === false) return
  const { data } = await api.GET('/api/v1/orders/counts')
  if (data) orderBadge.value = data.pending_review
}

function onOrdersChanged(): void {
  void loadOrderBadge()
}

onMounted(() => {
  void loadTodoBadge()
  void loadOrderBadge()
  todoTimer = setInterval(() => {
    if (document.visibilityState !== 'visible') return
    void loadTodoBadge()
    void loadOrderBadge()
  }, TODO_POLL_MS)
  window.addEventListener(TODOS_CHANGED, onTodosChanged)
  window.addEventListener(ORDERS_CHANGED, onOrdersChanged)
})
onBeforeUnmount(() => {
  clearInterval(todoTimer)
  window.removeEventListener(TODOS_CHANGED, onTodosChanged)
  window.removeEventListener(ORDERS_CHANGED, onOrdersChanged)
})

async function onCommand(command: string): Promise<void> {
  if (command === 'password') {
    passwordOpen.value = true
    return
  }
  await logout()
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
          <el-badge
            v-if="item.name === 'todos' && todoBadge"
            :value="todoBadge"
            :max="99"
            class="menu-badge"
            data-testid="todo-badge"
          />
          <el-badge
            v-if="item.name === 'orders' && orderBadge"
            :value="orderBadge"
            :max="99"
            type="warning"
            class="menu-badge"
            data-testid="order-badge"
          />
        </el-menu-item>
      </el-menu>
    </el-aside>
    <el-container>
      <el-header class="header">
        <span class="tenant">{{ auth.me?.tenant.name }}</span>
        <span class="right">
          <NotificationBell />
          <el-dropdown data-testid="user-menu" @command="onCommand">
          <span class="user">
            {{ auth.me?.display_name }}
            <el-icon><ArrowDown /></el-icon>
          </span>
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item command="password">修改密码</el-dropdown-item>
              <el-dropdown-item command="logout" divided>退出登录</el-dropdown-item>
            </el-dropdown-menu>
          </template>
          </el-dropdown>
        </span>
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
      <PasswordDialog v-model="passwordOpen" />
    </el-container>
  </el-container>
</template>

<style scoped>
.billing-notice {
  margin-bottom: 12px;
}

.menu-badge {
  margin-left: 8px;
  line-height: 1;
}

.right {
  display: flex;
  align-items: center;
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
