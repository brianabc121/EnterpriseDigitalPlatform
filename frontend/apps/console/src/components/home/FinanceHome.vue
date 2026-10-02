<script setup lang="ts">
import { onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'

import { api, formatDateTime } from '../../api'
import type { RecentPayment } from '../../finance'
import { money, PAYMENT_CHANNEL } from '../../orders'
import StatTile from '../charts/StatTile.vue'
import type { HomeData } from './home'
import HomeSection from './HomeSection.vue'
import LinkTile from './LinkTile.vue'

/**
 * 财务的首页（§28.4）：应收合计、逾期、今天到期、本月已收（点开就是筛选好的应收账款），最近登记的
 * 收款，以及快捷入口。管理员的首页也有这一块（管理员就是默认的财务）。
 */
const props = defineProps<{ data: HomeData }>()
defineEmits<{ changed: [] }>()

const LIMIT = 5
const router = useRouter()
const recent = ref<RecentPayment[]>([])

async function loadRecent(): Promise<void> {
  const { data } = await api.GET('/api/v1/finance/receivables/recent-payments', {
    params: { query: { limit: LIMIT } },
  })
  if (data) recent.value = data.items
}

function go(query: Record<string, string> = {}): void {
  void router.push({ path: '/receivables', query })
}

// 本月已收变了（登记了收款）时重新取最近的收款。
watch(
  () => props.data.receivables?.received_this_month,
  (value, before) => {
    if (before !== undefined && value !== before) void loadRecent()
  },
)
onMounted(loadRecent)
</script>

<template>
  <div data-testid="home-finance">
    <HomeSection title="应收账款" testid="home-finance-receivables">
      <template #extra>
        <el-button link type="primary" data-testid="home-finance-open" @click="go()">打开应收账款</el-button>
      </template>
      <div v-if="data.receivables" class="tiles">
        <StatTile
          label="应收合计"
          :value="money(data.receivables.open.amount)"
          :hint="`${data.receivables.open.count} 笔未收清`"
          testid="home-finance-open"
        />
        <LinkTile
          label="已逾期"
          :value="data.receivables.overdue.count"
          tone="danger"
          :hint="money(data.receivables.overdue.amount)"
          :to="{ path: '/receivables', query: { view: 'overdue' } }"
          testid="home-finance-overdue"
        />
        <LinkTile
          label="今天到期"
          :value="data.receivables.due_today.count"
          tone="warning"
          :hint="money(data.receivables.due_today.amount)"
          :to="{ path: '/receivables', query: { view: 'due_today' } }"
          testid="home-finance-due-today"
        />
        <StatTile
          label="本月已收"
          :value="money(data.receivables.received_this_month)"
          hint="登记的收款减退款"
          testid="home-finance-received"
        />
      </div>
      <el-skeleton v-else :rows="2" animated />
    </HomeSection>

    <HomeSection v-if="recent.length" title="最近登记的收款" testid="home-finance-payments">
      <el-table :data="recent" size="small">
        <el-table-column label="时间" width="150">
          <template #default="{ row }">{{ formatDateTime(row.paid_at) }}</template>
        </el-table-column>
        <el-table-column prop="order_no" label="订单" width="160" />
        <el-table-column label="客户" min-width="100">
          <template #default="{ row }">{{ row.customer_name ?? '—' }}</template>
        </el-table-column>
        <el-table-column label="金额" width="110" align="right">
          <template #default="{ row }">
            <span :class="{ refund: row.kind === 'refund' }">{{ row.kind === 'refund' ? '−' : '' }}{{ money(row.amount) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="渠道" width="90">
          <template #default="{ row }">{{ PAYMENT_CHANNEL[row.channel] ?? row.channel }}</template>
        </el-table-column>
        <el-table-column label="登记人" width="100">
          <template #default="{ row }">{{ row.recorded_by_name ?? '—' }}</template>
        </el-table-column>
      </el-table>
    </HomeSection>

    <HomeSection title="快捷入口" testid="home-finance-links">
      <el-space wrap>
        <el-button type="primary" data-testid="home-finance-overdue-list" @click="go({ view: 'overdue' })">逾期的应收</el-button>
        <el-button data-testid="home-finance-customers" @click="go({ tab: 'customers' })">按客户查看</el-button>
        <el-button @click="go({ view: 'due_soon' })">7 天内到期</el-button>
      </el-space>
    </HomeSection>
  </div>
</template>

<style scoped>
.refund {
  color: var(--el-color-danger);
}
</style>
