<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { computed, onMounted, ref, watch } from 'vue'

import { api } from '../../api'
import { formatDuration } from '../../labels'
import { money } from '../../orders'
import StatTile from '../charts/StatTile.vue'

/** 订单报表（设计文档 §25.10）：AI 下单、业务、收款、安全和当前积压。 */
const props = defineProps<{ range: [string, string]; tz?: string }>()

const report = ref<Schemas['OrderReport'] | null>(null)
const loading = ref(false)

async function load(): Promise<void> {
  loading.value = true
  const { data, error } = await api.GET('/api/v1/reports/orders', {
    params: { query: { start: props.range[0], end: props.range[1], tz: props.tz } },
  })
  loading.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  report.value = data
}

function minutes(value: number | null | undefined): string {
  return value === null || value === undefined ? '—' : formatDuration(value * 60)
}

function pct(value: number | null | undefined): string {
  return value === null || value === undefined ? '—' : `${value}%`
}

const reasonHint = computed(() =>
  (report.value?.ai.modify_reasons ?? []).map((r) => `${r.label} ${r.count}`).join('，'),
)

const TABLES = computed(() => {
  const b = report.value?.business
  if (!b) return []
  return [
    { title: '按来源', rows: b.by_source, amount: true },
    { title: '按渠道', rows: b.by_channel, amount: true },
    { title: '按处理人', rows: b.by_agent, amount: true },
    { title: '商品销量前 10', rows: b.top_products, amount: true, unit: '数量' },
    { title: '取消原因', rows: b.cancel_reasons, amount: false },
    { title: '商品缺口前 10（客户问到、商品库里没有）', rows: b.product_gaps, amount: false, unit: '次数' },
  ]
})

watch(() => props.range, load)
onMounted(load)
</script>

<template>
  <div v-loading="loading && !report" data-testid="order-report">
    <template v-if="report">
      <h3 class="section first">AI 下单</h3>
      <div class="tiles">
        <StatTile label="采集过订单的会话" :value="String(report.ai.intent_sessions)" />
        <StatTile label="AI 提交审核" :value="String(report.ai.submitted)" testid="order-tile-ai" />
        <StatTile label="确认率" :value="pct(report.ai.approval_rate)" hint="已处理的 AI 订单" />
        <StatTile label="最终完成" :value="pct(report.ai.completed_rate)" />
        <StatTile label="被员工修改" :value="pct(report.ai.modified_rate)" :hint="reasonHint" />
      </div>

      <h3 class="section">业务与收款</h3>
      <div class="tiles">
        <StatTile
          label="订单"
          :value="String(report.business.orders)"
          :hint="`金额 ${money(report.business.amount)}（不含取消）`"
          testid="order-tile-orders"
        />
        <StatTile label="平均审核时长" :value="minutes(report.business.avg_review_minutes)" hint="提交到确认" />
        <StatTile label="应收" :value="money(report.payments.receivable)" hint="已确认未收清" testid="order-tile-receivable" />
        <StatTile
          label="逾期应收"
          :value="money(report.payments.overdue_receivable)"
          :hint="`${report.payments.overdue_orders} 个暂欠订单`"
        />
        <StatTile
          label="待审核"
          :value="String(report.now.pending_review)"
          :hint="
            report.now.oldest_pending_minutes === null || report.now.oldest_pending_minutes === undefined
              ? '当前'
              : `最久 ${minutes(report.now.oldest_pending_minutes)}`
          "
        />
        <StatTile
          label="套价识别"
          :value="String(report.security.price_probes)"
          :hint="`回复拦截 ${report.security.replies_blocked}`"
          testid="order-tile-probes"
        />
      </div>

      <div class="grid">
        <el-card shadow="never" class="card">
          <template #header>收款方式（确认的订单）</template>
          <el-table :data="report.payments.by_method" size="small" empty-text="没有数据">
            <el-table-column prop="label" label="收款方式" />
            <el-table-column prop="count" label="订单" width="70" align="right" />
            <el-table-column label="金额" width="120" align="right">
              <template #default="{ row }">{{ money(row.amount) }}</template>
            </el-table-column>
          </el-table>
        </el-card>
        <el-card v-for="t in TABLES" :key="t.title" shadow="never" class="card">
          <template #header>{{ t.title }}</template>
          <el-table :data="t.rows" size="small" empty-text="没有数据">
            <el-table-column prop="label" label="" min-width="120" />
            <el-table-column prop="count" :label="t.unit ?? '订单'" width="70" align="right" />
            <el-table-column v-if="t.amount" label="金额" width="120" align="right">
              <template #default="{ row }">{{ money(row.amount) }}</template>
            </el-table-column>
          </el-table>
        </el-card>
      </div>
    </template>
  </div>
</template>

<style scoped>
.tiles {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(170px, 1fr));
  gap: 12px;
}

.grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(360px, 1fr));
  gap: 12px;
  margin-top: 16px;
}

.card :deep(.el-card__header) {
  padding: 10px 16px;
  font-size: 14px;
  font-weight: 600;
}

.section {
  margin: 24px 0 12px;
  font-size: 15px;
}

.section.first {
  margin-top: 0;
}
</style>
