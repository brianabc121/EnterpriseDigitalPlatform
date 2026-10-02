<script setup lang="ts">
import { errorMessage } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { computed, onMounted, ref, watch } from 'vue'

import { api } from '../../api'
import ProfitChart, { type ProfitPoint } from '../charts/ProfitChart.vue'
import StatTile from '../charts/StatTile.vue'
import {
  cellText,
  change,
  changeText,
  num,
  pct,
  shortRange,
  statementRows,
  yuan,
  yuanShort,
  type ProfitPeriod,
  type ProfitSummary,
  type ProfitTrend,
  type StatementRow,
} from '../../profit'

/**
 * 盈利报表的"利润表"页签（设计文档 §30.4）：顶部数字和比上期的变化、成本缺失的提示、本期 / 上期 /
 * 去年同期的利润表、12 个月的净利润柱状图和每月明细、口径说明。
 */
const props = defineProps<{ period: ProfitPeriod; version: number }>()

const summary = ref<ProfitSummary | null>(null)
const trend = ref<ProfitTrend | null>(null)
const loading = ref(false)

async function load(): Promise<void> {
  loading.value = true
  const query = { start: props.period.start, end: props.period.end, shift: props.period.shift }
  const [s, t] = await Promise.all([
    api.GET('/api/v1/profit/summary', { params: { query } }),
    api.GET('/api/v1/profit/trend', { params: { query: { end: props.period.end } } }),
  ])
  loading.value = false
  if (!s.data || !t.data) {
    ElMessage.error(errorMessage(s.error ?? t.error))
    return
  }
  summary.value = s.data
  trend.value = t.data
}

/** 今年、去年：上期就是去年同期，只显示一列。 */
const oneComparison = computed(() => props.period.shift === 12)
const statements = computed(() => {
  const s = summary.value
  if (!s) return []
  return oneComparison.value ? [s.current, s.last_year] : [s.current, s.previous, s.last_year]
})
const columns = computed(() => {
  const s = summary.value
  if (!s) return []
  const names = oneComparison.value ? ['本期', '去年同期'] : ['本期', '上期', '去年同期']
  return statements.value.map((st, i) => ({ name: names[i]!, range: shortRange(st.period) }))
})
const rows = computed<StatementRow[]>(() => statementRows(statements.value))

const compareLabel = computed(() => (oneComparison.value ? '比去年同期' : '比上期'))
function delta(pick: (s: ProfitSummary['current']) => string): string {
  const s = summary.value
  if (!s) return ''
  const base = oneComparison.value ? s.last_year : s.previous
  return changeText(change(pick(s.current), pick(base)), compareLabel.value)
}

function join(...parts: string[]): string {
  return parts.filter(Boolean).join(' · ')
}

const tiles = computed(() => {
  const s = summary.value
  if (!s) return []
  const c = s.current
  const loss = num(c.net_profit) < 0
  return [
    {
      key: 'revenue',
      label: '销售收入',
      value: yuan(c.revenue),
      hint: join(`订单 ${c.orders} 笔`, delta((x) => x.revenue)),
    },
    { key: 'cost', label: '销售成本', value: yuan(c.cost), hint: delta((x) => x.cost) },
    {
      key: 'gross',
      label: '毛利',
      value: yuan(c.gross_profit),
      hint: join(`毛利率 ${pct(c.gross_margin)}`, delta((x) => x.gross_profit)),
      tone: num(c.gross_profit) < 0 ? ('danger' as const) : undefined,
    },
    {
      key: 'expenses',
      label: '费用',
      value: yuan(c.expenses),
      hint: join(`其他收入 ${yuan(c.other_income)}`, delta((x) => x.expenses)),
    },
    {
      key: 'net',
      label: loss ? '净利润（亏损）' : '净利润',
      value: yuan(c.net_profit),
      hint: join(`净利率 ${pct(c.net_margin)}`, delta((x) => x.net_profit)),
      tone: loss ? ('danger' as const) : undefined,
    },
    {
      key: 'collected',
      label: '回款',
      value: yuan(s.collected),
      hint: `本期订单还有 ${yuan(s.uncollected)} 未收`,
    },
  ]
})

const gap = computed(() => summary.value?.cost_gap ?? null)
const gapNames = computed(() =>
  (gap.value?.products ?? []).map((p) => (p.code ? `${p.name}（${p.code}）` : p.name)).join('、'),
)

const points = computed<ProfitPoint[]>(() =>
  (trend.value?.months ?? []).map((m) => {
    const [year, month] = m.month.split('-')
    return {
      label: `${Number(month)} 月`,
      title: `${year} 年 ${Number(month)} 月`,
      value: num(m.net_profit),
      details: [`销售收入 ${yuan(m.revenue)}`, `毛利 ${yuan(m.gross_profit)}`, `费用 ${yuan(m.expenses)}`],
    }
  }),
)
const months = computed(() => [...(trend.value?.months ?? [])].reverse())

function rowClass({ row }: { row: StatementRow }): string {
  return [row.strong ? 'strong' : '', row.level === 1 ? 'sub' : ''].join(' ')
}

watch(() => [props.period.start, props.period.end, props.period.shift, props.version], load)
onMounted(load)
defineExpose({ reload: load })
</script>

<template>
  <div v-loading="loading && !summary" :class="{ refreshing: loading && summary }">
    <div v-if="summary" class="tiles" data-testid="profit-tiles">
      <StatTile
        v-for="t in tiles"
        :key="t.key"
        :label="t.label"
        :value="t.value"
        :hint="t.hint"
        :tone="t.tone"
        :testid="`profit-tile-${t.key}`"
      />
    </div>

    <el-alert
      v-if="gap && gap.lines > 0"
      type="warning"
      :closable="false"
      show-icon
      class="gap"
      data-testid="profit-cost-gap"
      :title="`有 ${gap.lines} 个商品行没有成本价（涉及收入 ${yuan(gap.revenue)}），按 0 计算，毛利偏高`"
    >
      <template #default>
        <span>缺成本价的商品：{{ gapNames }}。</span>
        <router-link to="/products" class="link">到商品库补上成本价</router-link>
        <span>，补上后报表自动重算。</span>
      </template>
    </el-alert>

    <div v-if="summary" class="grid">
      <el-card shadow="never" class="card">
        <template #header>利润表</template>
        <el-table
          :data="rows"
          :row-class-name="rowClass"
          size="small"
          row-key="key"
          data-testid="profit-statement"
        >
          <el-table-column label="项目" min-width="140">
            <template #default="{ row }">
              <span :class="{ indent: (row as StatementRow).level === 1 }">{{ (row as StatementRow).label }}</span>
            </template>
          </el-table-column>
          <el-table-column
            v-for="(col, i) in columns"
            :key="col.name"
            align="right"
            min-width="120"
          >
            <template #header>
              <div class="col-head">
                <span>{{ col.name }}</span>
                <small>{{ col.range }}</small>
              </div>
            </template>
            <template #default="{ row }">
              <span
                :class="{
                  negative: (row as StatementRow).kind !== 'count' && num((row as StatementRow).values[i]) < 0,
                }"
              >
                {{ cellText(row as StatementRow, (row as StatementRow).values[i] ?? null) }}
              </span>
            </template>
          </el-table-column>
        </el-table>
      </el-card>

      <el-card shadow="never" class="card">
        <template #header>
          <div class="chart-head">
            <span>每月净利润</span>
            <span class="key">
              <i class="swatch gain" />盈利
              <i class="swatch loss" />亏损
            </span>
          </div>
        </template>
        <ProfitChart
          :points="points"
          :format="yuan"
          :axis-format="yuanShort"
          label="最近 12 个月每月的净利润，零线上方是盈利，下方是亏损"
          data-testid="profit-chart"
        />
      </el-card>
    </div>

    <el-collapse v-if="trend" class="collapse">
      <el-collapse-item title="每月明细" name="months">
        <el-table :data="months" size="small" data-testid="profit-months">
          <el-table-column prop="month" label="月份" width="90" />
          <el-table-column prop="orders" label="订单" width="70" align="right" />
          <el-table-column label="销售收入" min-width="110" align="right">
            <template #default="{ row }">{{ yuan(row.revenue) }}</template>
          </el-table-column>
          <el-table-column label="销售成本" min-width="110" align="right">
            <template #default="{ row }">{{ yuan(row.cost) }}</template>
          </el-table-column>
          <el-table-column label="毛利" min-width="110" align="right">
            <template #default="{ row }">{{ yuan(row.gross_profit) }}</template>
          </el-table-column>
          <el-table-column label="毛利率" width="80" align="right">
            <template #default="{ row }">{{ pct(row.gross_margin) }}</template>
          </el-table-column>
          <el-table-column label="其他收入" min-width="100" align="right">
            <template #default="{ row }">{{ yuan(row.other_income) }}</template>
          </el-table-column>
          <el-table-column label="费用" min-width="100" align="right">
            <template #default="{ row }">{{ yuan(row.expenses) }}</template>
          </el-table-column>
          <el-table-column label="净利润" min-width="110" align="right">
            <template #default="{ row }">
              <span :class="{ negative: num(row.net_profit) < 0 }">{{ yuan(row.net_profit) }}</span>
            </template>
          </el-table-column>
        </el-table>
      </el-collapse-item>
      <el-collapse-item title="口径说明" name="rules">
        <ul class="rules" data-testid="profit-rules">
          <li>销售收入：已确认、处理中、已发货、已完成的订单合计，按确认日期计入；取消的订单不算，退款只影响回款。</li>
          <li>销售成本：商品行数量 × 下单时记下的成本价；下单时没有成本价的用商品库现在的成本价，都没有的按 0 算并在上面提示。</li>
          <li>毛利 = 销售收入 − 销售成本；按商品看时订单优惠按金额比例分摊到每一行。</li>
          <li>费用、其他收入：收支登记里的支出、收入，按发生日期计入。净利润 = 毛利 + 其他收入 − 费用。</li>
          <li>回款：期间内登记的收款减退款，不参与利润；本期订单未收按现在的收款计算。</li>
          <li>日期按工作时间设置里的时区划分；上期是往前移同样的月数，去年同期是往前移 12 个月。</li>
        </ul>
      </el-collapse-item>
    </el-collapse>
  </div>
</template>

<style scoped>
.tiles {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(170px, 1fr));
  gap: 12px;
  margin-bottom: 12px;
}

/* 手机上数字两列，金额不折行。 */
@media (max-width: 600px) {
  .tiles {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .tiles :deep(.value) {
    font-size: 18px;
    white-space: nowrap;
  }
}

.gap {
  margin-bottom: 12px;
}

.link {
  color: var(--el-color-primary);
}

.grid {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  gap: 12px;
}

@media (max-width: 1100px) {
  .grid {
    grid-template-columns: minmax(0, 1fr);
  }
}

.card {
  min-width: 0;
}

.col-head {
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  line-height: 1.3;
}

.col-head small {
  font-weight: normal;
  color: var(--el-text-color-secondary);
}

.indent {
  padding-left: 1.5em;
  color: var(--el-text-color-secondary);
}

:deep(.strong) td {
  font-weight: 600;
}

.negative {
  color: var(--el-color-danger);
}

.chart-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.key {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.swatch {
  display: inline-block;
  width: 10px;
  height: 10px;
  margin-left: 6px;
  border-radius: 2px;
}

.swatch.gain {
  background: #2a78d6;
}

.swatch.loss {
  background: #e34948;
}

:global(html.dark) .swatch.gain {
  background: #3987e5;
}

:global(html.dark) .swatch.loss {
  background: #e66767;
}

.collapse {
  margin-top: 12px;
}

.rules {
  margin: 0;
  padding-left: 1.2em;
  line-height: 1.8;
  color: var(--el-text-color-regular);
}

.refreshing {
  opacity: 0.6;
  transition: opacity 0.2s;
}
</style>
