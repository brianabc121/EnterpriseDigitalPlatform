<script setup lang="ts">
import { computed } from 'vue'

import { formatDuration } from '../../labels'
import { amountText } from '../../opportunities'
import { useAuthStore } from '../../stores/auth'
import StatTile from '../charts/StatTile.vue'
import type { HomeData } from './home'
import HomeSection from './HomeSection.vue'
import LinkTile from './LinkTile.vue'

/** 管理员和主管的首页（§25.15）：团队的实时接待、待处理的事情和常用功能。 */
const props = defineProps<{ data: HomeData }>()

const auth = useAuthStore()
const modules = computed(() => auth.menus.filter((m) => m.name !== 'dashboard'))
const live = computed(() => props.data.live)
const reviewsOrders = computed(() => auth.can('order:review') && props.data.orders !== null)
const pendingDocuments = computed(() => {
  const w = props.data.warehouse
  return w ? w.pending_requisitions + w.pending_receipts : 0
})
const opportunities = computed(() => props.data.opportunities)
const wonHint = computed(() => {
  const amount = amountText(opportunities.value?.won_amount_this_month)
  return amount ? `金额 ${amount}` : undefined
})
const documentsTab = computed(() =>
  props.data.warehouse && !props.data.warehouse.pending_requisitions && props.data.warehouse.pending_receipts
    ? 'receipt'
    : 'requisition',
)
</script>

<template>
  <div data-testid="home-team">
    <HomeSection v-if="live" title="实时接待">
      <template #extra><span class="muted">每 15 秒自动刷新</span></template>
      <div class="tiles" data-testid="realtime-tiles">
        <StatTile
          label="排队中"
          :value="String(live.queued)"
          :hint="
            live.longest_wait_seconds !== null
              ? `最长已等 ${formatDuration(live.longest_wait_seconds)}`
              : '没有客户在等待'
          "
          testid="rt-queued"
        />
        <StatTile label="接待中" :value="String(live.serving)" testid="rt-serving" />
        <StatTile v-if="live.ai_serving" label="AI 接待中" :value="String(live.ai_serving)" testid="rt-ai-serving" />
        <StatTile
          label="在线坐席"
          :value="String(live.agents_online + live.agents_busy)"
          :hint="`忙碌 ${live.agents_busy}，小休 ${live.agents_away}`"
          testid="rt-agents"
        />
        <StatTile
          label="今日会话"
          :value="String(live.today_sessions)"
          :hint="`已结束 ${live.today_closed}`"
          testid="rt-today"
        />
        <StatTile
          label="今日满意度"
          :value="
            live.today_csat_avg === null || live.today_csat_avg === undefined
              ? '—'
              : `${live.today_csat_avg.toFixed(1)} 分`
          "
          :hint="`${live.today_csat_count} 个评价`"
        />
      </div>
    </HomeSection>

    <HomeSection v-if="reviewsOrders || data.todos || data.warehouse" title="待处理" testid="home-team-pending">
      <div class="tiles">
        <LinkTile
          v-if="reviewsOrders && data.orders"
          label="待审核的订单"
          :value="data.orders.pending_review"
          tone="warning"
          :to="{ path: '/orders', query: { view: 'pending_review' } }"
          testid="home-orders-review"
        />
        <LinkTile
          v-if="data.todos"
          label="待确认的待办"
          :value="data.todos.pending"
          hint="AI 从会话中登记，确认后生效"
          tone="warning"
          :to="{ path: '/todos', query: { view: 'pending' } }"
          testid="home-todos-pending"
        />
        <LinkTile
          v-if="data.warehouse"
          label="待确认的单据"
          :value="pendingDocuments"
          :hint="`领料单 ${data.warehouse.pending_requisitions}，入库单 ${data.warehouse.pending_receipts}`"
          tone="warning"
          :to="{ path: '/warehouse', query: { tab: documentsTab } }"
          testid="home-documents-pending"
        />
      </div>
    </HomeSection>

    <HomeSection v-if="opportunities" title="商机" testid="home-team-opportunities">
      <div class="tiles">
        <LinkTile
          label="进行中的商机"
          :value="opportunities.active"
          :hint="`我负责的 ${opportunities.mine}，待确认 ${opportunities.suggested}`"
          :to="{ path: '/opportunities' }"
          testid="home-opps-active"
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

    <HomeSection v-if="modules.length" title="常用功能">
      <el-space wrap>
        <router-link v-for="item in modules" :key="item.name" :to="item.path">
          <el-card shadow="hover" class="module">{{ item.title }}</el-card>
        </router-link>
      </el-space>
    </HomeSection>
  </div>
</template>

<style scoped>
.muted {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.module {
  width: 160px;
  text-align: center;
}

a {
  text-decoration: none;
}
</style>
