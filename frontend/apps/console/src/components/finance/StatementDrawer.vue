<script setup lang="ts">
import { errorMessage } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { computed, ref, watch } from 'vue'

import { api, formatDateTime } from '../../api'
import { dueText, type CustomerStatement } from '../../finance'
import { money } from '../../orders'
import { printHtml, statementHtml } from '../../print'
import { useAuthStore } from '../../stores/auth'

/**
 * 客户对账单（§28.4）：期间（默认本月）、期初未收、期间内的订单和收款、期末未收，以及目前未收清的
 * 订单；可以打印。
 */
const props = defineProps<{ customerId: string | null }>()
const emit = defineEmits<{ close: [] }>()

const KIND: Record<string, string> = { order: '订单', payment: '收款', refund: '退款' }
const auth = useAuthStore()
const statement = ref<CustomerStatement | null>(null)
const loading = ref(false)
const range = ref<[string, string] | null>(null)

const open = computed({
  get: () => props.customerId !== null,
  set: (value) => {
    if (!value) emit('close')
  },
})
const title = computed(() =>
  statement.value ? `对账单 · ${statement.value.customer_name}` : '对账单',
)

async function load(): Promise<void> {
  const id = props.customerId
  if (!id) return
  loading.value = true
  const [from, to] = range.value ?? [undefined, undefined]
  const { data, error } = await api.GET('/api/v1/finance/customers/{customer_id}/statement', {
    params: { path: { customer_id: id }, query: { from, to } },
  })
  loading.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    emit('close')
    return
  }
  statement.value = data
  range.value = [data.period_from, data.period_to]
}

function print(): void {
  if (!statement.value) return
  if (!printHtml(statementHtml(statement.value, auth.me?.tenant.name ?? ''))) {
    ElMessage.warning('浏览器拦截了打印窗口，请允许弹出窗口后再试')
  }
}

watch(
  () => props.customerId,
  (id) => {
    statement.value = null
    range.value = null
    if (id) void load()
  },
  { immediate: true },
)
</script>

<template>
  <el-drawer v-model="open" :title="title" size="min(720px, 100%)" destroy-on-close data-testid="statement-drawer">
    <div v-loading="loading">
      <div class="toolbar">
        <span data-testid="statement-range">
          <el-date-picker
            v-model="range"
            type="daterange"
            value-format="YYYY-MM-DD"
            start-placeholder="开始"
            end-placeholder="结束"
            :clearable="false"
            class="range"
            @change="load"
          />
        </span>
        <el-button :disabled="!statement" data-testid="statement-print" @click="print">打印</el-button>
      </div>

      <template v-if="statement">
        <dl class="summary" data-testid="statement-summary">
          <div><dt>客户</dt><dd>{{ statement.customer_name }}<span v-if="statement.company" class="muted"> · {{ statement.company }}</span></dd></div>
          <div><dt>期初未收</dt><dd>{{ money(statement.opening) }}</dd></div>
          <div><dt>本期订单</dt><dd>{{ money(statement.orders_amount) }}</dd></div>
          <div><dt>本期收款</dt><dd>{{ money(statement.received) }}<span v-if="Number(statement.refunded) > 0" class="muted">（退款 {{ money(statement.refunded) }}）</span></dd></div>
          <div><dt>期末未收</dt><dd><b data-testid="statement-closing">{{ money(statement.closing) }}</b></dd></div>
        </dl>

        <el-table :data="statement.lines" size="small" empty-text="期间内没有订单和收款" data-testid="statement-lines">
          <el-table-column prop="date" label="日期" width="100" />
          <el-table-column label="类型" width="60">
            <template #default="{ row }">{{ KIND[row.kind] ?? row.kind }}</template>
          </el-table-column>
          <el-table-column prop="order_no" label="订单号" width="150" />
          <el-table-column prop="description" label="摘要" min-width="140" show-overflow-tooltip />
          <el-table-column label="金额" width="110" align="right">
            <template #default="{ row }">
              <span :class="{ paid: row.kind === 'payment' }">{{ row.kind === 'payment' ? '−' : '' }}{{ money(row.amount) }}</span>
            </template>
          </el-table-column>
          <el-table-column label="未收余额" width="110" align="right">
            <template #default="{ row }">{{ money(row.balance) }}</template>
          </el-table-column>
        </el-table>

        <h4>未收清的订单</h4>
        <el-table :data="statement.open_orders" size="small" empty-text="没有未收清的订单" data-testid="statement-open">
          <el-table-column prop="no" label="订单号" width="150" />
          <el-table-column label="到期" min-width="120">
            <template #default="{ row }">
              <span :class="{ danger: row.overdue_days > 0 }">{{ dueText(row) }}</span>
              <span v-if="row.due_date" class="muted"> · {{ row.due_date }}</span>
            </template>
          </el-table-column>
          <el-table-column label="合计" width="100" align="right">
            <template #default="{ row }">{{ money(row.total) }}</template>
          </el-table-column>
          <el-table-column label="未收" width="100" align="right">
            <template #default="{ row }"><b>{{ money(row.outstanding) }}</b></template>
          </el-table-column>
        </el-table>

        <p class="muted foot">制表：{{ statement.generated_by }} · {{ formatDateTime(statement.generated_at) }}</p>
      </template>
    </div>
  </el-drawer>
</template>

<style scoped>
.toolbar {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 12px;
}

.range {
  max-width: 300px;
}

.summary {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(160px, 1fr));
  gap: 8px 16px;
  margin: 0 0 12px;
}

.summary dt {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.summary dd {
  margin: 2px 0 0;
}

h4 {
  margin: 16px 0 8px;
}

.muted {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.paid {
  color: var(--el-color-success);
}

.danger {
  color: var(--el-color-danger);
}

.foot {
  margin-top: 12px;
}
</style>
