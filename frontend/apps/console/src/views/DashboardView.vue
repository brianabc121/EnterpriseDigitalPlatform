<script setup lang="ts">
import type { ConsoleProfile } from '@edp/api-client'
import { computed, onBeforeUnmount, onMounted, reactive } from 'vue'

import { api } from '../api'
import AgentHome from '../components/home/AgentHome.vue'
import FinanceHome from '../components/home/FinanceHome.vue'
import type { HomeData } from '../components/home/home'
import KeeperHome from '../components/home/KeeperHome.vue'
import KnowledgeHome from '../components/home/KnowledgeHome.vue'
import TeamHome from '../components/home/TeamHome.vue'
import WakeHome from '../components/home/WakeHome.vue'
import { PROFILE_LABEL } from '../menu'
import { ORDERS_CHANGED } from '../orders'
import { useAuthStore } from '../stores/auth'
import { TODOS_CHANGED } from '../todos'

/**
 * 首页（§25.15）：最上面是 AI 巡检发现的、需要我处理的问题（§33.5，有的时候才显示）；然后按员工的
 * 岗位依次显示——管理员和主管看团队的实时接待和待处理的事情，客服看自己的
 * 接待和待办，仓管看待确认的单据和库存不足，知识管理员看待审核和快到期的知识。工人没有首页。
 * 实时接待每 15 秒刷新；待办、订单、仓库的数量每分钟刷新，有变化时立即刷新。只取显示的菜单的数字。
 */
const LIVE_MS = 15_000
const COUNTS_MS = 60_000

const auth = useAuthStore()
const data = reactive<HomeData>({
  live: null,
  todos: null,
  orders: null,
  warehouse: null,
  receivables: null,
})
const profiles = computed(() => auth.profiles)
const has = (profile: ConsoleProfile): boolean => profiles.value.includes(profile)
const shown = (name: string): boolean => auth.menus.some((m) => m.name === name)
const team = computed(() => has('admin') || has('supervisor'))
// 财务的一块（§28.4）：财务岗位，以及默认担任财务的管理员。
const finance = computed(() => shown('receivables') && (has('finance') || has('admin')))
// 没有岗位的首页内容时（例如管理员给工人打开了首页）显示常用功能。
const fallback = computed(
  () => !team.value && !has('agent') && !has('finance') && !has('keeper') && !has('knowledge'),
)
const modules = computed(() => auth.menus.filter((m) => m.name !== 'dashboard'))
const roleText = computed(() => profiles.value.map((p) => PROFILE_LABEL[p]).join('、'))

let liveTimer: ReturnType<typeof setInterval> | undefined
let countsTimer: ReturnType<typeof setInterval> | undefined

async function loadLive(): Promise<void> {
  if (!team.value && !has('agent')) return
  const { data: live } = await api.GET('/api/v1/reports/realtime')
  if (live) data.live = live
}

async function loadCounts(): Promise<void> {
  const [todos, orders, warehouse, receivables] = await Promise.all([
    shown('todos') ? api.GET('/api/v1/todos/counts') : null,
    shown('orders') && auth.can('order:review') ? api.GET('/api/v1/orders/counts') : null,
    shown('warehouse') && (team.value || has('keeper')) ? api.GET('/api/v1/warehouse/counts') : null,
    finance.value ? api.GET('/api/v1/finance/receivables/summary') : null,
  ])
  data.todos = todos?.data ?? null
  data.orders = orders?.data ?? null
  data.warehouse = warehouse?.data ?? null
  data.receivables = receivables?.data ?? null
}

function onChanged(): void {
  void loadCounts()
}

function visible(): boolean {
  return document.visibilityState === 'visible'
}

onMounted(() => {
  void loadLive()
  void loadCounts()
  liveTimer = setInterval(() => {
    if (visible()) void loadLive()
  }, LIVE_MS)
  countsTimer = setInterval(() => {
    if (visible()) void loadCounts()
  }, COUNTS_MS)
  window.addEventListener(TODOS_CHANGED, onChanged)
  window.addEventListener(ORDERS_CHANGED, onChanged)
})
onBeforeUnmount(() => {
  clearInterval(liveTimer)
  clearInterval(countsTimer)
  window.removeEventListener(TODOS_CHANGED, onChanged)
  window.removeEventListener(ORDERS_CHANGED, onChanged)
})
</script>

<template>
  <div>
    <div class="page-header">
      <h2>你好，{{ auth.me?.display_name }}</h2>
      <span class="muted" data-testid="home-profiles">{{ auth.me?.tenant.name }} · {{ roleText }}</span>
    </div>

    <WakeHome :can-open-wake="shown('wake')" />
    <TeamHome v-if="team" :data="data" />
    <AgentHome v-if="has('agent')" :data="data" />
    <FinanceHome v-if="finance" :data="data" @changed="onChanged" />
    <KeeperHome v-if="has('keeper') && shown('warehouse')" :data="data" @changed="onChanged" />
    <KnowledgeHome v-if="has('knowledge') && shown('knowledge') && auth.can('kb:manage')" />

    <template v-if="fallback && modules.length">
      <h3 class="section">常用功能</h3>
      <el-space wrap>
        <router-link v-for="item in modules" :key="item.name" :to="item.path">
          <el-card shadow="hover" class="module">{{ item.title }}</el-card>
        </router-link>
      </el-space>
    </template>
  </div>
</template>

<style scoped>
.muted {
  color: var(--el-text-color-secondary);
  font-size: 13px;
}

.section {
  margin: 24px 0 12px;
  font-size: 15px;
}

.module {
  width: 160px;
  text-align: center;
}

a {
  text-decoration: none;
}
</style>
