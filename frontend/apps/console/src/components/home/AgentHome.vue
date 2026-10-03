<script setup lang="ts">
import { computed } from 'vue'
import { useRouter } from 'vue-router'

import { formatDuration } from '../../labels'
import { amountText } from '../../opportunities'
import { useAuthStore } from '../../stores/auth'
import StatTile from '../charts/StatTile.vue'
import type { HomeData } from './home'
import HomeSection from './HomeSection.vue'
import LinkTile from './LinkTile.vue'

/**
 * 客服的首页（§25.15）：我的接待（排队、正在接待、今天的会话）、我的待办（逾期、今天到期、待认领）、
 * 待审核的订单，以及快捷入口（工作台、新建订单、新建待办）。
 */
const props = defineProps<{ data: HomeData }>()

const auth = useAuthStore()
const router = useRouter()
const shown = (name: string): boolean => auth.menus.some((m) => m.name === name)
const live = computed(() => props.data.live)
const reviewsOrders = computed(() => auth.can('order:review') && props.data.orders !== null)
const canOrder = computed(() => shown('orders') && auth.can('order:create'))
const canTodo = computed(() => shown('todos') && (auth.can('todo:handle') || auth.can('todo:assign')))
const opportunities = computed(() => props.data.opportunities)
const wonHint = computed(() => {
  const amount = amountText(opportunities.value?.won_amount_this_month)
  return amount ? `金额 ${amount}` : undefined
})

function go(path: string, query: Record<string, string> = {}): void {
  void router.push({ path, query })
}
</script>

<template>
  <div data-testid="home-agent">
    <HomeSection v-if="live" title="我的接待">
      <div class="tiles">
        <StatTile
          label="排队中"
          :value="String(live.queued)"
          :hint="
            live.longest_wait_seconds !== null
              ? `最长已等 ${formatDuration(live.longest_wait_seconds)}`
              : '没有客户在等待'
          "
          testid="rt-my-queued"
        />
        <StatTile label="我正在接待" :value="String(live.my_serving)" testid="rt-my-serving" />
        <StatTile label="我今天的会话" :value="String(live.my_today_sessions)" testid="rt-my-today" />
      </div>
    </HomeSection>

    <HomeSection v-if="data.todos || reviewsOrders" title="我的待办" testid="home-agent-todos">
      <div class="tiles">
        <template v-if="data.todos">
          <LinkTile
            label="已逾期"
            :value="data.todos.overdue"
            tone="danger"
            :to="{ path: '/todos', query: { view: 'mine', due: 'overdue' } }"
            testid="home-todos-overdue"
          />
          <LinkTile
            label="今天到期"
            :value="data.todos.due_today"
            tone="warning"
            :to="{ path: '/todos', query: { view: 'mine', due: 'today' } }"
            testid="home-todos-today"
          />
          <LinkTile
            label="我的未完成"
            :value="data.todos.mine"
            :to="{ path: '/todos', query: { view: 'mine' } }"
            testid="home-todos-mine"
          />
          <LinkTile
            v-if="data.todos.pool"
            label="待认领"
            :value="data.todos.pool"
            :to="{ path: '/todos', query: { view: 'pool' } }"
            testid="home-todos-pool"
          />
        </template>
        <LinkTile
          v-if="reviewsOrders && data.orders"
          label="待审核的订单"
          :value="data.orders.pending_review"
          tone="warning"
          :to="{ path: '/orders', query: { view: 'pending_review' } }"
          testid="home-agent-orders-review"
        />
      </div>
    </HomeSection>

    <HomeSection v-if="opportunities" title="我的商机" testid="home-agent-opportunities">
      <div class="tiles">
        <LinkTile
          label="我负责的商机"
          :value="opportunities.mine"
          :to="{ path: '/opportunities', query: { view: 'mine' } }"
          testid="home-opps-mine"
        />
        <LinkTile
          label="本周要跟进"
          :value="opportunities.week"
          tone="warning"
          :hint="`今天 ${opportunities.today}，已逾期 ${opportunities.overdue}`"
          :to="{ path: '/opportunities', query: { view: 'week' } }"
          testid="home-opps-week"
        />
        <LinkTile
          label="停滞"
          :value="opportunities.stale"
          tone="danger"
          hint="超过阶段的停滞天数没有动态"
          :to="{ path: '/opportunities', query: { view: 'stale' } }"
          testid="home-opps-stale"
        />
        <LinkTile
          label="本月赢单"
          :value="opportunities.won_this_month"
          :hint="wonHint"
          :to="{ path: '/opportunities', query: { view: 'won', mode: 'list' } }"
          testid="home-opps-won"
        />
      </div>
    </HomeSection>

    <HomeSection title="快捷入口">
      <el-space wrap>
        <el-button v-if="shown('workbench')" type="primary" data-testid="home-open-workbench" @click="go('/workbench')"
          >打开工作台</el-button
        >
        <el-button v-if="canOrder" data-testid="home-new-order" @click="go('/orders', { new: '1' })">新建订单</el-button>
        <el-button v-if="canTodo" data-testid="home-new-todo" @click="go('/todos', { new: '1' })">新建待办</el-button>
        <el-button v-if="shown('customers')" @click="go('/customers')">客户</el-button>
        <el-button v-if="shown('knowledge')" @click="go('/knowledge')">知识库</el-button>
      </el-space>
    </HomeSection>
  </div>
</template>
