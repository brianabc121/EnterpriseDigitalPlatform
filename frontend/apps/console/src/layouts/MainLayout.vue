<script setup lang="ts">
import {
  ArrowDown,
  Avatar,
  Box,
  ChatDotRound,
  Clock,
  Connection,
  DataLine,
  Document,
  Goods,
  HomeFilled,
  MagicStick,
  OfficeBuilding,
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
import type { MenuIcon } from '../menu'
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
  production: Box,
  warehouse: OfficeBuilding,
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
// 按岗位显示的菜单（§25.15）；角标只给显示的菜单取数。
const menus = computed(() => auth.menus)
const shown = (name: string): boolean => menus.value.some((item) => item.name === name)
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
  if (!shown('todos')) return
  const { data } = await api.GET('/api/v1/todos/counts')
  if (data) todoBadge.value = data.pending + data.overdue
}

function onTodosChanged(): void {
  void loadTodoBadge()
}

// 订单菜单的角标：待审核的订单（有审核权限时），与待办一起刷新；订单有变化时立即刷新。
const orderBadge = ref(0)

async function loadOrderBadge(): Promise<void> {
  if (!auth.can('order:review') || !shown('orders')) return
  const { data } = await api.GET('/api/v1/orders/counts')
  if (data) orderBadge.value = data.pending_review
}

// 加工菜单的角标：待领取的订单，给进不了订单中心的工人看（其他员工在订单中心看待发货和缺货），
// 与待办一起刷新；加工或订单有变化时立即刷新。
const productionBadge = ref(0)

async function loadProductionBadge(): Promise<void> {
  if (!shown('production') || auth.can('order:read')) return
  const { data } = await api.GET('/api/v1/production/counts')
  if (data) productionBadge.value = data.pool
}

// 仓库菜单的角标：待确认的领料单和入库单（给仓管看），与待办一起刷新；加工或订单有变化时立即刷新。
const warehouseBadge = ref(0)

async function loadWarehouseBadge(): Promise<void> {
  if (!auth.can('warehouse:confirm') || !shown('warehouse')) return
  const { data } = await api.GET('/api/v1/warehouse/counts')
  if (data) warehouseBadge.value = data.pending_requisitions + data.pending_receipts
}

function onOrdersChanged(): void {
  void loadOrderBadge()
  void loadProductionBadge()
  void loadWarehouseBadge()
}

// 手机上（工人在车间用手机打开"加工"）侧边栏收起成图标。
const NARROW = '(max-width: 768px)'
const narrowQuery = typeof window.matchMedia === 'function' ? window.matchMedia(NARROW) : null
const narrow = ref(narrowQuery?.matches ?? false)

function onNarrow(event: MediaQueryListEvent): void {
  narrow.value = event.matches
}

// 收起时菜单名称和角标只在悬停提示里，角标另外显示在图标右上角（工人在手机上也看得到待领取数）。
const badges = computed<Record<string, number>>(() => ({
  todos: todoBadge.value,
  orders: orderBadge.value,
  production: productionBadge.value,
  warehouse: warehouseBadge.value,
}))
const BADGE_TYPE: Record<string, 'danger' | 'warning'> = {
  todos: 'danger',
  orders: 'warning',
  production: 'warning',
  warehouse: 'warning',
}

onMounted(() => {
  void loadTodoBadge()
  void loadOrderBadge()
  void loadProductionBadge()
  void loadWarehouseBadge()
  todoTimer = setInterval(() => {
    if (document.visibilityState !== 'visible') return
    void loadTodoBadge()
    void loadOrderBadge()
    void loadProductionBadge()
    void loadWarehouseBadge()
  }, TODO_POLL_MS)
  window.addEventListener(TODOS_CHANGED, onTodosChanged)
  window.addEventListener(ORDERS_CHANGED, onOrdersChanged)
  narrowQuery?.addEventListener('change', onNarrow)
})
onBeforeUnmount(() => {
  clearInterval(todoTimer)
  window.removeEventListener(TODOS_CHANGED, onTodosChanged)
  window.removeEventListener(ORDERS_CHANGED, onOrdersChanged)
  narrowQuery?.removeEventListener('change', onNarrow)
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
    <el-aside :width="narrow ? '64px' : '208px'" class="aside">
      <div class="brand">{{ narrow ? 'EDP' : 'EDP 智能客服' }}</div>
      <el-menu
        :default-active="route.path"
        :collapse="narrow"
        :collapse-transition="false"
        router
        class="menu"
        data-testid="main-menu"
      >
        <el-menu-item v-for="item in menus" :key="item.name" :index="item.path">
          <el-icon><component :is="icons[item.icon]" /></el-icon>
          <el-badge
            v-if="narrow && badges[item.name]"
            :value="badges[item.name]"
            :max="99"
            :type="BADGE_TYPE[item.name]"
            class="corner-badge"
            :data-testid="`${item.name}-corner-badge`"
          />
          <template #title>
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
            <el-badge
              v-if="item.name === 'production' && productionBadge"
              :value="productionBadge"
              :max="99"
              type="warning"
              class="menu-badge"
              data-testid="production-badge"
            />
            <el-badge
              v-if="item.name === 'warehouse' && warehouseBadge"
              :value="warehouseBadge"
              :max="99"
              type="warning"
              class="menu-badge"
              data-testid="warehouse-badge"
            />
          </template>
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

.corner-badge {
  position: absolute;
  top: 4px;
  right: 4px;
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
  white-space: nowrap;
  overflow: hidden;
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

@media (max-width: 768px) {
  .brand {
    padding: 0;
    text-align: center;
  }

  .header {
    padding: 0 12px;
  }

  .tenant {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    margin-right: 8px;
  }

  .el-main {
    padding: 12px;
  }
}
</style>
