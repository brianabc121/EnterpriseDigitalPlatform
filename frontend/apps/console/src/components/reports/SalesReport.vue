<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { computed, onMounted, ref, watch } from 'vue'

import { api } from '../../api'
import { amountText, daysText, funnelRows } from '../../opportunities'
import { percent } from '../../reports'
import BarList from '../charts/BarList.vue'
import StatTile from '../charts/StatTile.vue'

/**
 * 销售报表（设计文档 §40.9，按客户的数据范围）：漏斗（期间内新建的商机到过各阶段的数量和转化率）、进行中的
 * 预计金额（按阶段、加权、本月和下月预计成交）、赢单率和平均周期、输单原因、按负责人、按来源。预计金额是人填
 * 的估计，和订单的实际金额分开；设置为只有管理者可见而自己不能看时不显示金额。
 */
const props = defineProps<{ range: [string, string]; tz?: string }>()

const report = ref<Schemas['SalesReport'] | null>(null)
const loading = ref(false)

async function load(): Promise<void> {
  loading.value = true
  const { data, error } = await api.GET('/api/v1/reports/sales', {
    params: { query: { start: props.range[0], end: props.range[1], tz: props.tz } },
  })
  loading.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  report.value = data
}

const amountVisible = computed(() => !!report.value?.amount_visible)
const created = computed(() => report.value?.funnel[0]?.count ?? 0)
const funnel = computed(() => funnelRows(report.value?.funnel ?? []))
const stageAmounts = computed(() =>
  (report.value?.amount.by_stage ?? []).map((b) => ({ label: b.label, value: Number(b.amount ?? 0) })),
)
const lostReasons = computed(() =>
  (report.value?.lost_reasons ?? []).map((b) => ({ label: b.label, value: b.count })),
)

function money(value: string | number | null | undefined): string {
  return amountText(value) || '—'
}

watch(() => props.range, load)
onMounted(load)
</script>

<template>
  <div v-loading="loading && !report" data-testid="sales-report">
    <template v-if="report">
      <h3 class="section first">成交</h3>
      <div class="tiles">
        <StatTile label="新建商机" :value="String(created)" hint="期间内" testid="sales-tile-created" />
        <StatTile
          label="赢单率"
          :value="percent(report.win.win_rate)"
          :hint="`期间内关闭 ${report.win.closed}：赢单 ${report.win.won}，输单 ${report.win.lost}`"
          testid="sales-tile-win-rate"
        />
        <StatTile label="平均成交周期" :value="daysText(report.win.avg_days)" hint="转入到赢单" />
        <StatTile v-if="amountVisible" label="赢单平均金额" :value="money(report.win.avg_amount)" hint="预计金额" />
        <StatTile label="进行中" :value="String(report.amount.open_count)" hint="现在" testid="sales-tile-open" />
        <StatTile
          v-if="amountVisible"
          label="加权预计金额"
          :value="money(report.amount.weighted)"
          hint="Σ 预计金额 × 成交概率"
          testid="sales-tile-weighted"
        />
        <StatTile
          v-if="amountVisible"
          label="本月预计成交"
          :value="money(report.amount.this_month)"
          :hint="`下月 ${money(report.amount.next_month)}`"
        />
      </div>

      <div class="grid">
        <el-card shadow="never" class="card">
          <template #header>漏斗（期间内新建的商机到过各阶段）</template>
          <div class="funnel" data-testid="sales-funnel">
            <div
              v-for="row in funnel"
              :key="row.code"
              class="funnel-row"
              :title="`${row.name}：${row.count}${row.rateText ? `（相对上一阶段 ${row.rateText}）` : ''}`"
              :data-testid="`sales-funnel-${row.code}`"
            >
              <span class="name">{{ row.name }}</span>
              <span class="track"><span class="bar" :style="{ width: row.width }" /></span>
              <span class="count">{{ row.count }}</span>
              <span class="rate">{{ row.rateText }}</span>
            </div>
            <p v-if="funnel.length === 0" class="empty">没有数据</p>
          </div>
        </el-card>
        <el-card v-if="amountVisible" shadow="never" class="card">
          <template #header>进行中的预计金额（元，按阶段）</template>
          <BarList :items="stageAmounts" label="按阶段的预计金额" empty="没有进行中的商机" />
        </el-card>
        <el-card shadow="never" class="card">
          <template #header>输单原因</template>
          <BarList :items="lostReasons" label="输单原因" empty="期间内没有输单" />
        </el-card>
        <el-card shadow="never" class="card">
          <template #header>按来源</template>
          <el-table :data="report.by_source" size="small" empty-text="没有数据" data-testid="sales-by-source">
            <el-table-column prop="label" label="来源" min-width="100" />
            <el-table-column prop="count" label="新建" width="70" align="right" />
            <el-table-column prop="won" label="赢单" width="70" align="right" />
            <el-table-column label="赢单率" width="80" align="right">
              <template #default="{ row }">{{ percent(row.win_rate) }}</template>
            </el-table-column>
          </el-table>
        </el-card>
        <el-card shadow="never" class="card wide">
          <template #header>按负责人</template>
          <el-table :data="report.by_owner" size="small" empty-text="没有数据" data-testid="sales-by-owner">
            <el-table-column prop="name" label="负责人" min-width="100" />
            <el-table-column prop="created" label="新建" width="70" align="right" />
            <el-table-column prop="active" label="进行中" width="80" align="right" />
            <el-table-column prop="won" label="赢单" width="70" align="right" />
            <el-table-column prop="lost" label="输单" width="70" align="right" />
            <el-table-column v-if="amountVisible" label="赢单金额" width="120" align="right">
              <template #default="{ row }">{{ money(row.won_amount) }}</template>
            </el-table-column>
            <el-table-column label="平均周期" width="90" align="right">
              <template #default="{ row }">{{ daysText(row.avg_days) }}</template>
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

.card.wide {
  grid-column: 1 / -1;
}

.section {
  margin: 24px 0 12px;
  font-size: 15px;
}

.section.first {
  margin-top: 0;
}

.funnel {
  --viz-series: #2a78d6;
  display: grid;
  gap: 8px;
}

:global(html.dark) .funnel {
  --viz-series: #3987e5;
}

.funnel-row {
  display: grid;
  grid-template-columns: 72px 1fr 48px 48px;
  align-items: center;
  gap: 8px;
  font-size: 13px;
}

.funnel-row .name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.track {
  height: 16px;
  background: var(--el-fill-color-light);
  border-radius: 4px;
}

.bar {
  display: block;
  height: 100%;
  background: var(--viz-series);
  border-radius: 0 4px 4px 0;
}

.count {
  text-align: right;
}

.rate {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  text-align: right;
}

.empty {
  margin: 0;
  font-size: 13px;
  color: var(--el-text-color-secondary);
}
</style>
