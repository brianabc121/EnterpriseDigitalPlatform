<script setup lang="ts">
import { errorMessage } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { computed, onMounted, ref, watch } from 'vue'

import { api } from '../../api'
import { useAuthStore } from '../../stores/auth'
import OrderDrawer from '../orders/OrderDrawer.vue'
import {
  defaultSort,
  DIMENSIONS,
  num,
  pct,
  SORT_OPTIONS,
  yuan,
  type BreakdownRow,
  type Dimension,
  type ProfitBreakdown,
  type ProfitPeriod,
} from '../../profit'

/**
 * 盈利报表的"毛利分析"页签（设计文档 §30.4）：按商品、客户、处理人、来源、渠道、订单汇总收入、
 * 成本、毛利和毛利率；毛利为负的行标红，有缺成本价的行标"缺成本"。订单默认按毛利率从低到高，
 * 先看到亏本和低毛利的订单。
 */
const props = defineProps<{ period: ProfitPeriod; version: number }>()

const PAGE_SIZE = 20
const auth = useAuthStore()
const by = ref<Dimension>('product')
const sortKey = ref(defaultSort('product'))
const page = ref(1)
const result = ref<ProfitBreakdown | null>(null)
const loading = ref(false)
const openId = ref<string | null>(null)
const canOpenOrders = computed(() => auth.can('order:read'))

async function load(): Promise<void> {
  const option = SORT_OPTIONS.find((o) => o.key === sortKey.value) ?? SORT_OPTIONS[0]!
  loading.value = true
  const { data, error } = await api.GET('/api/v1/profit/breakdown', {
    params: {
      query: {
        by: by.value,
        start: props.period.start,
        end: props.period.end,
        sort: option.sort,
        direction: option.direction,
        limit: PAGE_SIZE,
        offset: (page.value - 1) * PAGE_SIZE,
      },
    },
  })
  loading.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  result.value = data
}

function pickDimension(value: Dimension): void {
  by.value = value
  sortKey.value = defaultSort(value)
  page.value = 1
  void load()
}

function rowClass({ row }: { row: BreakdownRow }): string {
  return num(row.gross_profit) < 0 ? 'loss-row' : ''
}

const nameLabel = computed(() => DIMENSIONS.find(([key]) => key === by.value)?.[1] ?? '名称')

watch(sortKey, () => {
  page.value = 1
  void load()
})
watch(
  () => [props.period.start, props.period.end, props.version],
  () => {
    page.value = 1
    void load()
  },
)
onMounted(load)
</script>

<template>
  <div>
    <div class="toolbar">
      <el-radio-group
        :model-value="by"
        size="small"
        data-testid="breakdown-dims"
        @update:model-value="(v: string | number | boolean | undefined) => pickDimension(v as Dimension)"
      >
        <el-radio-button
          v-for="[key, label] in DIMENSIONS"
          :key="key"
          :value="key"
          :data-testid="`breakdown-dim-${key}`"
        >
          {{ label }}
        </el-radio-button>
      </el-radio-group>
      <el-select v-model="sortKey" size="small" class="sort" data-testid="breakdown-sort">
        <el-option v-for="o in SORT_OPTIONS" :key="o.key" :label="o.label" :value="o.key" />
      </el-select>
      <span v-if="result" class="total">总毛利 {{ yuan(result.gross_profit) }}</span>
    </div>

    <el-table
      v-loading="loading"
      :data="result?.items ?? []"
      :row-class-name="rowClass"
      row-key="key"
      data-testid="breakdown-table"
      empty-text="这段时间没有确认的订单"
    >
      <el-table-column :label="by === 'order' ? '订单' : nameLabel" min-width="200">
        <template #default="{ row }">
          <div class="name">
            <el-button
              v-if="by === 'order' && canOpenOrders && (row as BreakdownRow).order_id"
              link
              type="primary"
              data-testid="breakdown-order"
              @click="openId = (row as BreakdownRow).order_id ?? null"
            >
              {{ (row as BreakdownRow).label }}
            </el-button>
            <span v-else data-testid="breakdown-label">{{ (row as BreakdownRow).label }}</span>
            <el-tag
              v-if="(row as BreakdownRow).missing_cost > 0"
              size="small"
              type="warning"
              data-testid="breakdown-missing"
            >
              缺成本
            </el-tag>
          </div>
          <small v-if="(row as BreakdownRow).detail" class="muted">{{ (row as BreakdownRow).detail }}</small>
        </template>
      </el-table-column>
      <template v-if="by === 'order'">
        <el-table-column label="确认日期" width="110" prop="confirmed_on" />
        <el-table-column label="处理人" width="100">
          <template #default="{ row }">{{ (row as BreakdownRow).assignee_name || '未分派' }}</template>
        </el-table-column>
      </template>
      <el-table-column v-else label="订单" width="70" align="right" prop="orders" />
      <el-table-column v-if="by === 'product'" label="销量" width="80" align="right" prop="quantity" />
      <el-table-column label="销售收入" min-width="120" align="right">
        <template #default="{ row }">{{ yuan((row as BreakdownRow).revenue) }}</template>
      </el-table-column>
      <el-table-column label="销售成本" min-width="120" align="right">
        <template #default="{ row }">{{ yuan((row as BreakdownRow).cost) }}</template>
      </el-table-column>
      <el-table-column label="毛利" min-width="120" align="right">
        <template #default="{ row }">
          <span :class="{ negative: num((row as BreakdownRow).gross_profit) < 0 }" data-testid="breakdown-profit">
            {{ yuan((row as BreakdownRow).gross_profit) }}
          </span>
        </template>
      </el-table-column>
      <el-table-column label="毛利率" width="90" align="right">
        <template #default="{ row }">{{ pct((row as BreakdownRow).gross_margin) }}</template>
      </el-table-column>
      <el-table-column v-if="by !== 'order'" label="占总毛利" width="90" align="right">
        <template #default="{ row }">{{ pct((row as BreakdownRow).share) }}</template>
      </el-table-column>
      <template v-if="by === 'product'">
        <el-table-column label="平均售价" min-width="110" align="right">
          <template #default="{ row }">{{ yuan((row as BreakdownRow).avg_price) }}</template>
        </el-table-column>
        <el-table-column label="单位成本" min-width="110" align="right">
          <template #default="{ row }">{{ yuan((row as BreakdownRow).unit_cost) }}</template>
        </el-table-column>
      </template>
    </el-table>
    <el-pagination
      v-if="result && result.total > PAGE_SIZE"
      v-model:current-page="page"
      :page-size="PAGE_SIZE"
      :total="result.total"
      layout="total, prev, pager, next"
      class="pager"
      @current-change="load"
    />
    <OrderDrawer :order-id="openId" @close="openId = null" @changed="load" />
  </div>
</template>

<style scoped>
.toolbar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 12px;
  margin-bottom: 12px;
}

.sort {
  width: 150px;
}

.total {
  margin-left: auto;
  font-size: 13px;
  color: var(--el-text-color-secondary);
}

.name {
  display: flex;
  align-items: center;
  gap: 6px;
}

.muted {
  color: var(--el-text-color-secondary);
}

.negative {
  color: var(--el-color-danger);
  font-weight: 600;
}

:deep(.loss-row) td {
  background: var(--el-color-danger-light-9);
}

.pager {
  margin-top: 12px;
  justify-content: flex-end;
}
</style>
