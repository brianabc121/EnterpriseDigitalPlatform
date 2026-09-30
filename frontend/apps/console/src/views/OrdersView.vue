<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { api, formatDateTime } from '../api'
import OrderDrawer from '../components/orders/OrderDrawer.vue'
import OrderFormDialog from '../components/orders/OrderFormDialog.vue'
import PasswordExportDialog from '../components/shared/PasswordExportDialog.vue'
import {
  money,
  ORDER_SOURCE,
  ORDER_STATUS,
  ORDER_STATUS_TAG,
  ORDER_VIEWS,
  PAYMENT_METHOD,
  PAYMENT_STATUS,
  PAYMENT_STATUS_TAG,
  viewLabel,
  type Order,
  type OrderView,
} from '../orders'
import { useAuthStore } from '../stores/auth'

/**
 * 订单中心（设计文档 §25.9）：全部、待审核、处理中、待发货（工人加工完成，§25.11）、缺货、
 * 应收（未收清，按约定日期排序，逾期标红）、修改过的；按状态、来源、客户和时间筛选。
 * 站内信和待办里的链接带 id，打开后直接显示这个订单。
 */
const PAGE_SIZE = 20
const route = useRoute()
const router = useRouter()
const auth = useAuthStore()

const view = ref<OrderView>('all')
const items = ref<Order[]>([])
const total = ref(0)
const page = ref(1)
const loading = ref(false)
const counts = ref<Schemas['OrderCounts'] | null>(null)
const openId = ref<string | null>(null)
const creating = ref(false)
const exporting = ref(false)
const filters = reactive({
  status: '' as Schemas['OrderOut']['status'] | '',
  source: '' as Schemas['OrderOut']['source'] | '',
  dates: [] as string[],
  q: '',
})

// 没有发货环节时"待发货"显示为"待交付"。
const shipping = ref(true)
const canCreate = computed(() => auth.can('order:create'))
const canExport = computed(() => auth.can('order:export'))

function badge(name: OrderView): number {
  const c = counts.value
  if (!c) return 0
  if (name === 'pending_review') return c.pending_review
  if (name === 'processing') return c.processing
  if (name === 'awaiting_shipment') return c.awaiting_shipment
  if (name === 'out_of_stock') return c.out_of_stock
  if (name === 'receivable') return c.receivable
  return 0
}

const BADGE_TYPE: Partial<Record<OrderView, 'warning' | 'danger'>> = {
  pending_review: 'warning',
  awaiting_shipment: 'warning',
  out_of_stock: 'danger',
}

interface Filters {
  status?: Schemas['OrderOut']['status']
  source?: Schemas['OrderOut']['source']
  q?: string
  created_from?: string
  created_to?: string
}

function query(): Filters {
  const [from, to] = filters.dates
  return {
    status: filters.status || undefined,
    source: filters.source || undefined,
    q: filters.q.trim() || undefined,
    created_from: from || undefined,
    created_to: to || undefined,
  }
}

// 日期范围包含结束那一天。
const DAY = [new Date(2000, 0, 1, 0, 0, 0), new Date(2000, 0, 1, 23, 59, 59)]

async function loadCounts(): Promise<void> {
  const { data } = await api.GET('/api/v1/orders/counts')
  if (data) counts.value = data
}

async function loadSettings(): Promise<void> {
  const { data } = await api.GET('/api/v1/orders/settings')
  if (data) shipping.value = data.shipping_enabled
}

async function load(): Promise<void> {
  loading.value = true
  const { data, error } = await api.GET('/api/v1/orders', {
    params: {
      query: {
        view: view.value,
        ...query(),
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
  items.value = data.items
  total.value = data.total
}

async function refresh(): Promise<void> {
  await Promise.all([load(), loadCounts()])
}

function exporter(password: string) {
  const q = query()
  return api.POST('/api/v1/orders/export', {
    body: {
      password,
      view: view.value,
      status: q.status ?? null,
      source: q.source ?? null,
      q: q.q ?? null,
      created_from: q.created_from ?? null,
      created_to: q.created_to ?? null,
    },
    parseAs: 'blob',
  })
}

function close(): void {
  openId.value = null
  if (route.query.id) void router.replace({ query: { ...route.query, id: undefined } })
}

function applyQuery(): void {
  const v = route.query.view
  if (typeof v === 'string' && ORDER_VIEWS.some(([name]) => name === v)) view.value = v as OrderView
  const id = route.query.id
  openId.value = typeof id === 'string' && id ? id : openId.value
}

function onCreated(order: Schemas['OrderDetail']): void {
  void refresh()
  openId.value = order.id
}

watch(view, () => {
  page.value = 1
  void load()
})
watch(
  () => [filters.status, filters.source, filters.dates],
  () => {
    page.value = 1
    void load()
  },
)
watch(() => route.query, applyQuery)

onMounted(async () => {
  applyQuery()
  await Promise.all([refresh(), loadSettings()])
})
</script>

<template>
  <div>
    <div class="page-header">
      <h2>订单</h2>
      <span>
        <el-button v-if="canExport" data-testid="orders-export" @click="exporting = true">导出</el-button>
        <el-button v-if="canCreate" type="primary" data-testid="new-order" @click="creating = true"
          >新建订单</el-button
        >
      </span>
    </div>

    <el-tabs v-model="view" data-testid="order-views">
      <el-tab-pane v-for="[name, label] in ORDER_VIEWS" :key="name" :name="name">
        <template #label>
          <span :data-testid="`order-view-${name}`">
            {{ viewLabel(name, label, shipping) }}
            <el-badge
              v-if="badge(name)"
              :value="badge(name)"
              :type="BADGE_TYPE[name] ?? 'primary'"
              class="badge"
            />
          </span>
        </template>
      </el-tab-pane>
    </el-tabs>

    <el-alert
      v-if="view === 'receivable' && counts?.receivable_overdue"
      type="error"
      :closable="false"
      show-icon
      class="tip"
      :title="`有 ${counts.receivable_overdue} 个暂欠订单已过约定付款日期仍未收清（已生成催收待办）`"
    />

    <div class="filters">
      <el-select v-model="filters.status" clearable placeholder="状态" class="filter" data-testid="order-status-filter">
        <el-option v-for="(label, key) in ORDER_STATUS" :key="key" :label="label" :value="key" />
      </el-select>
      <el-select v-model="filters.source" clearable placeholder="来源" class="filter">
        <el-option v-for="(label, key) in ORDER_SOURCE" :key="key" :label="label" :value="key" />
      </el-select>
      <el-date-picker
        v-model="filters.dates"
        type="daterange"
        value-format="YYYY-MM-DDTHH:mm:ssZ"
        :default-time="DAY"
        start-placeholder="下单开始"
        end-placeholder="下单结束"
        class="dates"
      />
      <el-input
        v-model="filters.q"
        clearable
        placeholder="订单号、企业系统单号或客户"
        class="search"
        data-testid="order-search"
        @keyup.enter="load"
        @clear="load"
      />
    </div>

    <el-table
      v-loading="loading"
      :data="items"
      row-key="id"
      data-testid="orders-table"
      empty-text="暂无订单"
      class="table"
      @row-click="(row: Order) => (openId = row.id)"
    >
      <el-table-column prop="no" label="订单号" width="170" />
      <el-table-column label="客户" min-width="110">
        <template #default="{ row }">{{ row.customer_name ?? '—' }}</template>
      </el-table-column>
      <el-table-column label="商品" min-width="220">
        <template #default="{ row }">
          <div class="summary">{{ row.summary }}</div>
          <div class="flags">
            <el-tag v-if="row.price_pending" size="small" type="warning" effect="plain">待定价</el-tag>
            <el-tag v-if="row.modified" size="small" effect="plain">修改过</el-tag>
            <el-tag v-if="row.source === 'ai_chat'" size="small" type="info" effect="plain">AI</el-tag>
            <el-tag
              v-if="row.shortage && ['confirmed', 'fulfilling'].includes(row.status)"
              size="small"
              type="danger"
              effect="plain"
              data-testid="order-row-shortage"
              >缺货</el-tag
            >
            <el-tag
              v-if="row.processed_at && row.status === 'fulfilling'"
              size="small"
              type="success"
              effect="plain"
              data-testid="order-row-processed"
              >加工完成</el-tag
            >
            <el-tag
              v-else-if="row.worker_name && row.status === 'fulfilling'"
              size="small"
              type="info"
              effect="plain"
              >加工中 · {{ row.worker_name }}</el-tag
            >
          </div>
        </template>
      </el-table-column>
      <el-table-column label="合计" width="120" align="right">
        <template #default="{ row }">{{ money(row.total) }}</template>
      </el-table-column>
      <el-table-column label="收款" width="150">
        <template #default="{ row }">
          <div>{{ row.payment_method ? PAYMENT_METHOD[row.payment_method] : '待确认' }}</div>
          <el-tag size="small" :type="PAYMENT_STATUS_TAG[row.payment_status]" effect="plain">{{
            PAYMENT_STATUS[row.payment_status]
          }}</el-tag>
          <span v-if="Number(row.outstanding) > 0 && row.status !== 'draft'" class="muted">
            未收 {{ money(row.outstanding) }}</span
          >
          <div v-if="row.credit_due_date" :class="{ overdue: row.receivable_overdue }" class="muted">
            约定 {{ row.credit_due_date }}
          </div>
        </template>
      </el-table-column>
      <el-table-column label="状态" width="100">
        <template #default="{ row }">
          <el-tag size="small" :type="ORDER_STATUS_TAG[row.status]" data-testid="order-row-status">{{
            ORDER_STATUS[row.status]
          }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="处理人" width="110">
        <template #default="{ row }">
          <span v-if="row.assignee_name">{{ row.assignee_name }}</span>
          <span v-else class="muted">{{ row.skill_group_name ?? '待分派' }}</span>
        </template>
      </el-table-column>
      <el-table-column label="下单" width="160">
        <template #default="{ row }">{{ formatDateTime(row.created_at) }}</template>
      </el-table-column>
    </el-table>
    <div class="page-footer">
      <el-pagination
        v-model:current-page="page"
        :page-size="PAGE_SIZE"
        :total="total"
        layout="total, prev, pager, next"
        @current-change="load"
      />
    </div>

    <OrderDrawer :order-id="openId" @close="close" @changed="refresh" />
    <OrderFormDialog v-model="creating" @saved="onCreated" />
    <PasswordExportDialog
      v-model="exporting"
      title="导出订单"
      :hint="`导出当前视图和筛选条件下的订单（CSV）。收货信息${
        auth.can('customer:view_sensitive') ? '导出完整内容' : '导出掩码'
      }${auth.can('product:view_cost') ? '，包含成本合计' : ''}；导出操作会记入操作日志。`"
      :filename="`orders-${new Date().toISOString().slice(0, 10)}.csv`"
      :exporter="exporter"
    />
  </div>
</template>

<style scoped>
.tip {
  margin-bottom: 12px;
}

.filters {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 12px;
}

.filter {
  width: 130px;
}

.dates {
  width: 280px;
}

.search {
  width: 220px;
}

.badge {
  margin-left: 4px;
}

.table :deep(.el-table__row) {
  cursor: pointer;
}

.summary {
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.flags {
  display: flex;
  gap: 4px;
  margin-top: 2px;
}

.muted {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.overdue {
  color: var(--el-color-danger);
}
</style>
