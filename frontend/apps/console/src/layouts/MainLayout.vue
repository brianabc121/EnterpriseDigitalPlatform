<script setup lang="ts">
import { ArrowDown } from '@element-plus/icons-vue'
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { api } from '../api'
import PasswordDialog from '../components/account/PasswordDialog.vue'
import NotificationBell from '../components/layout/NotificationBell.vue'
import { MENU_ICONS as icons } from '../menuIcons'
import { ORDERS_CHANGED } from '../orders'
import { roleTitle } from '../roleNames'
import { TASKS_CHANGED } from '../tasks'
import { TODOS_CHANGED } from '../todos'
import { useAuthStore } from '../stores/auth'
import { useWorkbenchStore } from '../stores/workbench'

const auth = useAuthStore()
const route = useRoute()
const router = useRouter()

// 按岗位显示的菜单（§25.15）；角标只给显示的菜单取数。
const menus = computed(() => auth.menus)
// 左上角"EDP 智能客服"下面显示当前员工的"角色（姓名）"，左对齐（§39.7），和员工卡片的标题一样；手机上侧边栏只有图标，
// 放在顶栏最左边。
const identity = computed(() => {
  const me = auth.me
  return me ? `${roleTitle(me.roles, (code) => me.role_names?.[code])}（${me.display_name}）` : ''
})
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
  void loadTaskBadge()
}

// 个人待办菜单的角标：我已逾期的事项（§27.2），与待办一起刷新；事项有变化时立即刷新。
const taskBadge = ref(0)

async function loadTaskBadge(): Promise<void> {
  if (!shown('tasks')) return
  const { data } = await api.GET('/api/v1/tasks/counts')
  if (data) taskBadge.value = data.overdue
}

function onTasksChanged(): void {
  void loadTaskBadge()
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
  tasks: taskBadge.value,
  orders: orderBadge.value,
  production: productionBadge.value,
  warehouse: warehouseBadge.value,
}))
const BADGE_TYPE: Record<string, 'danger' | 'warning'> = {
  todos: 'danger',
  tasks: 'danger',
  orders: 'warning',
  production: 'warning',
  warehouse: 'warning',
}

onMounted(() => {
  void loadTodoBadge()
  void loadTaskBadge()
  void loadOrderBadge()
  void loadProductionBadge()
  void loadWarehouseBadge()
  todoTimer = setInterval(() => {
    if (document.visibilityState !== 'visible') return
    void loadTodoBadge()
    void loadTaskBadge()
    void loadOrderBadge()
    void loadProductionBadge()
    void loadWarehouseBadge()
  }, TODO_POLL_MS)
  window.addEventListener(TODOS_CHANGED, onTodosChanged)
  window.addEventListener(TASKS_CHANGED, onTasksChanged)
  window.addEventListener(ORDERS_CHANGED, onOrdersChanged)
  narrowQuery?.addEventListener('change', onNarrow)
})
onBeforeUnmount(() => {
  clearInterval(todoTimer)
  window.removeEventListener(TODOS_CHANGED, onTodosChanged)
  window.removeEventListener(TASKS_CHANGED, onTasksChanged)
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
      <div class="brand">
        <span class="brand-name" data-testid="console-brand">{{ narrow ? 'EDP' : 'EDP 智能客服' }}</span>
        <span v-if="identity && !narrow" class="identity" data-testid="console-identity" :title="identity">{{ identity }}</span>
      </div>
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
              v-if="item.name === 'tasks' && taskBadge"
              :value="taskBadge"
              :max="99"
              class="menu-badge"
              data-testid="task-badge"
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
        <span class="left">
          <span v-if="identity && narrow" class="identity-chip" data-testid="console-identity">{{ identity }}</span>
          <span class="tenant">{{ auth.me?.tenant.name }}</span>
        </span>
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
  flex-shrink: 0;
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
  display: flex;
  flex-direction: column;
  justify-content: center;
  height: 60px;
  padding: 0 8px 0 20px;
  overflow: hidden;
}

.brand-name {
  white-space: nowrap;
  font-weight: 600;
  font-size: 16px;
  line-height: 22px;
  color: var(--el-color-primary);
}

/* "EDP 智能客服"下面的"角色（姓名）"，和它左对齐。 */
.identity {
  margin-top: 2px;
  font-size: 12px;
  line-height: 18px;
  color: var(--el-text-color-regular);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
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

/* 左边是企业名称；手机上前面还有"角色（姓名）"，放不下时先缩短企业名称。 */
.left {
  display: flex;
  flex: 1 1 auto;
  align-items: center;
  gap: 12px;
  min-width: 0;
  margin-right: 12px;
}

.identity-chip {
  flex: 0 0 auto;
  max-width: 100%;
  padding: 2px 10px;
  border-radius: 12px;
  background: var(--el-color-primary-light-9);
  color: var(--el-color-primary);
  font-size: 13px;
  font-weight: 500;
  line-height: 20px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.tenant {
  flex: 0 1 auto;
  min-width: 0;
  font-weight: 500;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
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

  .left {
    gap: 8px;
    margin-right: 8px;
  }

  .el-main {
    padding: 12px;
  }
}
</style>
