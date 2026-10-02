<script setup lang="ts">
import { errorMessage } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { api, formatDateTime } from '../api'
import { downloadBlob } from '../download'
import StatTile from '../components/charts/StatTile.vue'
import PaymentDialog from '../components/finance/PaymentDialog.vue'
import StatementDrawer from '../components/finance/StatementDrawer.vue'
import OrderDrawer from '../components/orders/OrderDrawer.vue'
import {
  BUCKET_LABEL,
  BUCKETS,
  dueText,
  dueTone,
  isReceivableView,
  promiseText,
  RECEIVABLE_VIEWS,
  SORTS,
  type AgingBucket,
  type CustomerReceivable,
  type Receivable,
  type ReceivableSort,
  type ReceivableSummary,
  type ReceivableView,
} from '../finance'
import { money, ORDER_STATUS, ordersChanged, PAYMENT_METHOD, PAYMENT_METHODS, type PaymentMethod } from '../orders'
import { useAuthStore } from '../stores/auth'

/**
 * 应收账款（设计文档 §28）：全公司未收清的订单。顶部是应收合计、逾期、今天到期、7 天内到期、本月已收
 * 和账龄分段；"按订单"列出每笔应收（到期日、逾期天数、承诺付款日、最近跟进、催收），可以登记收款、
 * 跟进、催收；"按客户"汇总每个客户的未收和逾期，可以看对账单。首页和提醒的链接带 view、tab 参数。
 */
const PAGE_SIZE = 20
const route = useRoute()
const router = useRouter()
const auth = useAuthStore()

const tab = ref<'orders' | 'customers'>('orders')
const summary = ref<ReceivableSummary | null>(null)
const items = ref<Receivable[]>([])
const total = ref(0)
const page = ref(1)
const loading = ref(false)
const filters = reactive({
  view: 'open' as ReceivableView,
  bucket: '' as AgingBucket | '',
  method: '' as PaymentMethod | '',
  assignee: '',
  q: '',
})
const sort = ref<ReceivableSort>('due')
const customerFilter = ref<{ id: string; name: string } | null>(null)
const customers = ref<CustomerReceivable[]>([])
const customersTotal = ref(0)
const customersPage = ref(1)
const customersLoading = ref(false)
const customerQuery = reactive({ q: '', overdueOnly: false, sort: 'outstanding' as 'outstanding' | 'overdue' })
const staffOptions = ref<{ id: string; name: string }[]>([])
const openId = ref<string | null>(null)
const statementCustomer = ref<string | null>(null)
const payment = reactive({ open: false, item: null as Receivable | null })
const followup = reactive({ open: false, item: null as Receivable | null, promiseDate: '', note: '', saving: false })
const collect = reactive({ open: false, item: null as Receivable | null, assigneeId: '', saving: false })
const exporting = ref(false)

const canManage = computed(() => auth.can('finance:manage'))
const canPay = computed(() => auth.can('order:payment'))
const today = computed(() => summary.value?.today)
const bucketTotal = computed(() =>
  (summary.value?.buckets ?? []).reduce((sum, b) => sum + Number(b.amount), 0),
)

async function loadSummary(): Promise<void> {
  const { data } = await api.GET('/api/v1/finance/receivables/summary')
  if (data) summary.value = data
}

function query() {
  return {
    view: filters.view,
    bucket: filters.bucket || undefined,
    payment_method: filters.method || undefined,
    assignee_id: filters.assignee || undefined,
    customer_id: customerFilter.value?.id,
    q: filters.q.trim() || undefined,
  }
}

async function load(): Promise<void> {
  loading.value = true
  const { data, error } = await api.GET('/api/v1/finance/receivables', {
    params: {
      query: { ...query(), sort: sort.value, limit: PAGE_SIZE, offset: (page.value - 1) * PAGE_SIZE },
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

async function loadCustomers(): Promise<void> {
  customersLoading.value = true
  const { data, error } = await api.GET('/api/v1/finance/receivables/customers', {
    params: {
      query: {
        q: customerQuery.q.trim() || undefined,
        overdue_only: customerQuery.overdueOnly,
        sort: customerQuery.sort,
        limit: PAGE_SIZE,
        offset: (customersPage.value - 1) * PAGE_SIZE,
      },
    },
  })
  customersLoading.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  customers.value = data.items
  customersTotal.value = data.total
}

async function loadStaff(): Promise<void> {
  const { data } = await api.GET('/api/v1/finance/staff')
  if (data) staffOptions.value = data.items
}

function refresh(): void {
  void loadSummary()
  void load()
  if (tab.value === 'customers') void loadCustomers()
}

function pickBucket(bucket: AgingBucket): void {
  filters.bucket = filters.bucket === bucket ? '' : bucket
  tab.value = 'orders'
}

function pickView(view: ReceivableView): void {
  filters.view = view
  tab.value = 'orders'
}

function showCustomer(customer: CustomerReceivable): void {
  customerFilter.value = { id: customer.customer_id, name: customer.customer_name }
  tab.value = 'orders'
}

function openPayment(item: Receivable): void {
  Object.assign(payment, { open: true, item })
}

function openFollowup(item: Receivable): void {
  Object.assign(followup, { open: true, item, promiseDate: item.promise_date ?? '', note: '' })
}

async function saveFollowup(): Promise<void> {
  const item = followup.item
  if (!item) return
  if (!followup.promiseDate && !followup.note.trim()) {
    ElMessage.warning('请填写承诺付款日或备注')
    return
  }
  followup.saving = true
  const { data, error } = await api.POST('/api/v1/finance/receivables/{order_id}/followup', {
    params: { path: { order_id: item.id } },
    body: { promise_date: followup.promiseDate || null, note: followup.note.trim() },
  })
  followup.saving = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  ElMessage.success('已记录跟进')
  followup.open = false
  ordersChanged()
  refresh()
}

function openCollect(item: Receivable): void {
  Object.assign(collect, { open: true, item, assigneeId: item.assignee_id ?? '' })
}

async function saveCollect(): Promise<void> {
  const item = collect.item
  if (!item) return
  collect.saving = true
  const { data, error } = await api.POST('/api/v1/finance/receivables/{order_id}/collect', {
    params: { path: { order_id: item.id } },
    body: { assignee_id: collect.assigneeId || null },
  })
  collect.saving = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  ElMessage.success(`已生成催收待办 ${data.collection_todo?.no ?? ''}`)
  collect.open = false
  ordersChanged()
  refresh()
}

async function exportCsv(): Promise<void> {
  exporting.value = true
  const { data, error } = await api.POST('/api/v1/finance/receivables/export', {
    body: {
      view: filters.view,
      bucket: filters.bucket || null,
      payment_method: filters.method || null,
      assignee_id: filters.assignee || null,
      customer_id: customerFilter.value?.id ?? null,
      q: filters.q.trim() || null,
    },
    parseAs: 'blob',
  })
  exporting.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  downloadBlob(data, `receivables-${today.value ?? new Date().toISOString().slice(0, 10)}.csv`)
}

function close(): void {
  openId.value = null
  if (route.query.id) void router.replace({ query: { ...route.query, id: undefined } })
}

function applyQuery(): void {
  const view = route.query.view
  if (isReceivableView(view)) filters.view = view
  if (route.query.tab === 'customers') tab.value = 'customers'
  const id = route.query.id
  openId.value = typeof id === 'string' && id ? id : openId.value
}

watch(
  () => [filters.view, filters.bucket, filters.method, filters.assignee, sort.value, customerFilter.value],
  () => {
    page.value = 1
    void load()
  },
)
watch(
  () => [customerQuery.overdueOnly, customerQuery.sort],
  () => {
    customersPage.value = 1
    void loadCustomers()
  },
)
watch(tab, (value) => {
  if (value === 'customers') void loadCustomers()
})
watch(() => route.query, applyQuery)

onMounted(() => {
  applyQuery()
  void loadStaff()
  refresh()
})
</script>

<template>
  <div>
    <div class="page-header">
      <h2>应收账款</h2>
      <span>
        <el-button v-if="canManage" :loading="exporting" data-testid="receivables-export" @click="exportCsv"
          >导出</el-button
        >
      </span>
    </div>

    <div v-if="summary" class="tiles" data-testid="receivable-summary">
      <StatTile
        label="应收合计"
        :value="money(summary.open.amount)"
        :hint="`${summary.open.count} 笔未收清`"
        testid="receivable-open"
      />
      <button type="button" class="tile danger" data-testid="receivable-overdue" @click="pickView('overdue')">
        <span class="label">已逾期</span>
        <span class="value">{{ money(summary.overdue.amount) }}</span>
        <span class="hint">{{ summary.overdue.count }} 笔</span>
      </button>
      <button type="button" class="tile warning" data-testid="receivable-due-today" @click="pickView('due_today')">
        <span class="label">今天到期</span>
        <span class="value">{{ money(summary.due_today.amount) }}</span>
        <span class="hint">{{ summary.due_today.count }} 笔</span>
      </button>
      <button type="button" class="tile" data-testid="receivable-due-soon" @click="pickView('due_soon')">
        <span class="label">7 天内到期</span>
        <span class="value">{{ money(summary.due_soon.amount) }}</span>
        <span class="hint">{{ summary.due_soon.count }} 笔</span>
      </button>
      <StatTile
        label="本月已收"
        :value="money(summary.received_this_month)"
        hint="登记的收款减退款"
        testid="receivable-received"
      />
    </div>

    <div v-if="summary" class="aging" data-testid="receivable-aging">
      <span class="aging-title">账龄</span>
      <div class="bar">
        <button
          v-for="b in summary.buckets"
          :key="b.bucket"
          type="button"
          class="segment"
          :class="[b.bucket, { active: filters.bucket === b.bucket }]"
          :style="{ flexGrow: bucketTotal > 0 ? Math.max(Number(b.amount) / bucketTotal, 0.08) : 1 }"
          :title="`${b.label}：${money(b.amount)}（${b.count} 笔）`"
          :data-testid="`aging-${b.bucket}`"
          @click="pickBucket(b.bucket)"
        >
          <span class="seg-label">{{ b.label }}</span>
          <span class="seg-amount">{{ money(b.amount) }} · {{ b.count }}</span>
        </button>
      </div>
    </div>

    <el-tabs v-model="tab" data-testid="receivable-tabs">
      <el-tab-pane label="按订单" name="orders" />
      <el-tab-pane label="按客户" name="customers" />
    </el-tabs>

    <template v-if="tab === 'orders'">
      <div class="filters">
        <el-select v-model="filters.view" class="filter" data-testid="receivable-view">
          <el-option v-for="[name, label] in RECEIVABLE_VIEWS" :key="name" :label="label" :value="name" />
        </el-select>
        <el-select v-model="filters.bucket" clearable placeholder="账龄" class="filter" data-testid="receivable-bucket">
          <el-option v-for="b in BUCKETS" :key="b" :label="BUCKET_LABEL[b]" :value="b" />
        </el-select>
        <el-select v-model="filters.method" clearable placeholder="收款方式" class="filter">
          <el-option v-for="[value, label] in PAYMENT_METHODS" :key="value" :label="label" :value="value" />
        </el-select>
        <el-select v-model="filters.assignee" clearable filterable placeholder="处理人" class="filter">
          <el-option v-for="s in staffOptions" :key="s.id" :label="s.name" :value="s.id" />
        </el-select>
        <el-select v-model="sort" class="filter" data-testid="receivable-sort">
          <el-option v-for="[value, label] in SORTS" :key="value" :label="label" :value="value" />
        </el-select>
        <el-input
          v-model="filters.q"
          clearable
          placeholder="订单号、企业系统单号或客户"
          class="search"
          data-testid="receivable-search"
          @keyup.enter="load"
          @clear="load"
        />
        <el-tag v-if="customerFilter" closable data-testid="receivable-customer-filter" @close="customerFilter = null"
          >客户：{{ customerFilter.name }}</el-tag
        >
      </div>

      <div class="table-wrap">
        <el-table v-loading="loading" :data="items" row-key="id" data-testid="receivables-table" empty-text="没有未收清的订单">
          <el-table-column label="订单号" width="170">
            <template #default="{ row }">
              <el-button link type="primary" :data-testid="`receivable-open-${row.no}`" @click="openId = row.id">{{
                row.no
              }}</el-button>
              <div class="muted">{{ ORDER_STATUS[row.status] }}</div>
            </template>
          </el-table-column>
          <el-table-column label="客户" min-width="120">
            <template #default="{ row }">
              {{ row.customer_name ?? '—' }}
              <div v-if="row.customer_company" class="muted">{{ row.customer_company }}</div>
            </template>
          </el-table-column>
          <el-table-column label="收款方式" width="90">
            <template #default="{ row }">{{ row.payment_method ? PAYMENT_METHOD[row.payment_method] : '—' }}</template>
          </el-table-column>
          <el-table-column label="合计 / 已收" width="150" align="right">
            <template #default="{ row }">
              {{ money(row.total) }}
              <div class="muted">已收 {{ money(Number(row.paid_amount) - Number(row.refunded_amount)) }}</div>
            </template>
          </el-table-column>
          <el-table-column label="未收" width="110" align="right">
            <template #default="{ row }">
              <b :data-testid="`receivable-outstanding-${row.no}`">{{ money(row.outstanding) }}</b>
            </template>
          </el-table-column>
          <el-table-column label="到期" width="150">
            <template #default="{ row }">
              <span :class="dueTone(row, today)" :data-testid="`receivable-due-${row.no}`">{{ dueText(row, today) }}</span>
              <div v-if="row.due_date" class="muted">{{ row.due_date }} · 账龄 {{ row.age_days }} 天</div>
              <div v-else class="muted">账龄 {{ row.age_days }} 天</div>
            </template>
          </el-table-column>
          <el-table-column label="承诺付款" width="150">
            <template #default="{ row }">
              <span :class="{ danger: row.promise_overdue }">{{ promiseText(row) || '—' }}</span>
            </template>
          </el-table-column>
          <el-table-column label="处理人 / 跟进" min-width="170">
            <template #default="{ row }">
              {{ row.assignee_name ?? '待分派' }}
              <div v-if="row.followed_up_at" class="muted" :data-testid="`receivable-followup-${row.no}`">
                {{ formatDateTime(row.followed_up_at) }} {{ row.follow_up_note ?? '' }}
              </div>
              <div v-if="row.collection_todo" class="muted">催收待办 {{ row.collection_todo.no }}</div>
            </template>
          </el-table-column>
          <el-table-column label="" width="190" fixed="right">
            <template #default="{ row }">
              <el-button v-if="canPay" link type="primary" size="small" :data-testid="`receivable-pay-${row.no}`" @click="openPayment(row)"
                >登记收款</el-button
              >
              <el-button v-if="canManage" link type="primary" size="small" :data-testid="`receivable-followup-btn-${row.no}`" @click="openFollowup(row)"
                >跟进</el-button
              >
              <el-button
                v-if="canManage && !row.collection_todo"
                link
                type="primary"
                size="small"
                :data-testid="`receivable-collect-${row.no}`"
                @click="openCollect(row)"
                >催收</el-button
              >
            </template>
          </el-table-column>
        </el-table>
      </div>

      <div v-loading="loading" class="cards" data-testid="receivable-cards">
        <div v-for="row in items" :key="row.id" class="card">
          <div class="card-head">
            <el-button link type="primary" @click="openId = row.id">{{ row.no }}</el-button>
            <b>{{ money(row.outstanding) }}</b>
          </div>
          <div>{{ row.customer_name ?? '—' }} · {{ row.payment_method ? PAYMENT_METHOD[row.payment_method] : '—' }}</div>
          <div :class="dueTone(row, today)">{{ dueText(row, today) }}<span v-if="row.due_date" class="muted"> · {{ row.due_date }}</span></div>
          <div v-if="row.promise_date" :class="{ danger: row.promise_overdue }">{{ promiseText(row) }}</div>
          <div v-if="row.followed_up_at" class="muted">{{ formatDateTime(row.followed_up_at) }} {{ row.follow_up_note ?? '' }}</div>
          <div class="card-actions">
            <el-button v-if="canPay" size="small" type="primary" @click="openPayment(row)">登记收款</el-button>
            <el-button v-if="canManage" size="small" @click="openFollowup(row)">跟进</el-button>
            <el-button v-if="canManage && !row.collection_todo" size="small" @click="openCollect(row)">催收</el-button>
          </div>
        </div>
        <el-empty v-if="!loading && !items.length" description="没有未收清的订单" />
      </div>

      <div class="page-footer">
        <el-pagination
          v-model:current-page="page"
          :page-size="PAGE_SIZE"
          :total="total"
          layout="total, prev, pager, next"
          @current-change="load"
        />
      </div>
    </template>

    <template v-else>
      <div class="filters">
        <el-input
          v-model="customerQuery.q"
          clearable
          placeholder="客户或公司"
          class="search"
          data-testid="receivable-customer-search"
          @keyup.enter="loadCustomers"
          @clear="loadCustomers"
        />
        <el-select v-model="customerQuery.sort" class="filter">
          <el-option label="按未收合计" value="outstanding" />
          <el-option label="按逾期金额" value="overdue" />
        </el-select>
        <el-checkbox v-model="customerQuery.overdueOnly" data-testid="receivable-overdue-only">只看有逾期的</el-checkbox>
      </div>

      <div class="table-wrap">
        <el-table v-loading="customersLoading" :data="customers" row-key="customer_id" data-testid="receivable-customers" empty-text="没有未收清的客户">
          <el-table-column label="客户" min-width="140">
            <template #default="{ row }">
              {{ row.customer_name }}
              <div v-if="row.company" class="muted">{{ row.company }}</div>
            </template>
          </el-table-column>
          <el-table-column label="未收合计" width="120" align="right">
            <template #default="{ row }">
              <b :data-testid="`customer-outstanding-${row.customer_name}`">{{ money(row.outstanding) }}</b>
            </template>
          </el-table-column>
          <el-table-column label="逾期" width="120" align="right">
            <template #default="{ row }">
              <span :class="{ danger: Number(row.overdue) > 0 }">{{ money(row.overdue) }}</span>
            </template>
          </el-table-column>
          <el-table-column prop="orders" label="订单数" width="80" align="right" />
          <el-table-column label="最早到期" width="150">
            <template #default="{ row }">
              <template v-if="row.earliest_due">
                {{ row.earliest_due }}
                <div v-if="row.max_overdue_days" class="muted danger">最长逾期 {{ row.max_overdue_days }} 天</div>
              </template>
              <span v-else class="muted">未到期</span>
            </template>
          </el-table-column>
          <el-table-column label="最近收款" width="150">
            <template #default="{ row }">{{ row.last_paid_at ? formatDateTime(row.last_paid_at) : '—' }}</template>
          </el-table-column>
          <el-table-column label="最近跟进" min-width="160">
            <template #default="{ row }">
              <template v-if="row.followed_up_at">
                {{ formatDateTime(row.followed_up_at) }}
                <div v-if="row.follow_up_note" class="muted">{{ row.follow_up_note }}</div>
              </template>
              <span v-else class="muted">—</span>
            </template>
          </el-table-column>
          <el-table-column label="" width="150" fixed="right">
            <template #default="{ row }">
              <el-button link type="primary" size="small" :data-testid="`customer-statement-${row.customer_name}`" @click="statementCustomer = row.customer_id"
                >对账单</el-button
              >
              <el-button link type="primary" size="small" :data-testid="`customer-orders-${row.customer_name}`" @click="showCustomer(row)"
                >查看订单</el-button
              >
            </template>
          </el-table-column>
        </el-table>
      </div>

      <div v-loading="customersLoading" class="cards">
        <div v-for="row in customers" :key="row.customer_id" class="card">
          <div class="card-head">
            <span>{{ row.customer_name }}<span v-if="row.company" class="muted"> · {{ row.company }}</span></span>
            <b>{{ money(row.outstanding) }}</b>
          </div>
          <div>
            <span :class="{ danger: Number(row.overdue) > 0 }">逾期 {{ money(row.overdue) }}</span> · {{ row.orders }} 笔
            <span v-if="row.max_overdue_days" class="muted">· 最长逾期 {{ row.max_overdue_days }} 天</span>
          </div>
          <div class="card-actions">
            <el-button size="small" @click="statementCustomer = row.customer_id">对账单</el-button>
            <el-button size="small" @click="showCustomer(row)">查看订单</el-button>
          </div>
        </div>
        <el-empty v-if="!customersLoading && !customers.length" description="没有未收清的客户" />
      </div>

      <div class="page-footer">
        <el-pagination
          v-model:current-page="customersPage"
          :page-size="PAGE_SIZE"
          :total="customersTotal"
          layout="total, prev, pager, next"
          @current-change="loadCustomers"
        />
      </div>
    </template>

    <PaymentDialog
      v-model="payment.open"
      :order-id="payment.item?.id ?? null"
      :outstanding="payment.item?.outstanding"
      :paid="payment.item ? Number(payment.item.paid_amount) - Number(payment.item.refunded_amount) : 0"
      @saved="refresh"
    />

    <el-dialog v-model="followup.open" title="记录跟进" width="440px" data-testid="followup-dialog">
      <p v-if="followup.item" class="muted">
        订单 {{ followup.item.no }} · {{ followup.item.customer_name ?? '—' }} · 未收 {{ money(followup.item.outstanding) }}
      </p>
      <el-form label-width="90px" @submit.prevent="saveFollowup">
        <el-form-item label="承诺付款日">
          <span data-testid="followup-date">
            <el-date-picker v-model="followup.promiseDate" type="date" value-format="YYYY-MM-DD" placeholder="客户承诺哪天付" />
          </span>
        </el-form-item>
        <el-form-item label="备注">
          <div class="note" data-testid="followup-note">
            <el-input v-model="followup.note" type="textarea" :rows="2" maxlength="500" placeholder="例如：电话联系，客户说月底结" />
          </div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="followup.open = false">取消</el-button>
        <el-button type="primary" :loading="followup.saving" data-testid="followup-submit" @click="saveFollowup">保存</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="collect.open" title="催收" width="440px" data-testid="collect-dialog">
      <p v-if="collect.item" class="muted">
        生成一条"催收"待办：订单 {{ collect.item.no }}，未收 {{ money(collect.item.outstanding) }}。
      </p>
      <el-form label-width="72px" @submit.prevent="saveCollect">
        <el-form-item label="交给">
          <el-select v-model="collect.assigneeId" clearable filterable placeholder="订单处理人" data-testid="collect-assignee">
            <el-option v-for="s in staffOptions" :key="s.id" :label="s.name" :value="s.id" />
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="collect.open = false">取消</el-button>
        <el-button type="primary" :loading="collect.saving" data-testid="collect-submit" @click="saveCollect">生成催收待办</el-button>
      </template>
    </el-dialog>

    <StatementDrawer :customer-id="statementCustomer" @close="statementCustomer = null" />
    <OrderDrawer :order-id="openId" @close="close" @changed="refresh" />
  </div>
</template>

<style scoped>
.tiles {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(170px, 1fr));
  gap: 12px;
  margin-bottom: 12px;
}

.tile {
  display: flex;
  flex-direction: column;
  min-width: 0;
  padding: 14px 16px;
  text-align: left;
  font: inherit;
  cursor: pointer;
  background: var(--el-bg-color);
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 8px;
  color: inherit;
}

.tile:hover {
  border-color: var(--el-color-primary);
}

.tile .label,
.tile .hint {
  font-size: 13px;
  color: var(--el-text-color-secondary);
}

.tile .hint {
  margin-top: 4px;
  font-size: 12px;
}

.tile .value {
  margin-top: 6px;
  font-size: 22px;
  font-weight: 600;
}

.tile.danger .value {
  color: var(--el-color-danger);
}

.tile.warning .value {
  color: var(--el-color-warning);
}

.aging {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 8px;
}

.aging-title {
  font-size: 13px;
  color: var(--el-text-color-secondary);
  white-space: nowrap;
}

.bar {
  display: flex;
  flex: 1;
  gap: 3px;
  min-width: 0;
}

.segment {
  flex-basis: 0;
  min-width: 0;
  padding: 6px 8px;
  font: inherit;
  font-size: 12px;
  text-align: left;
  cursor: pointer;
  color: var(--el-text-color-primary);
  background: var(--el-fill-color);
  border: 1px solid transparent;
  border-radius: 6px;
  overflow: hidden;
}

.segment.current {
  background: var(--el-color-success-light-8);
}

.segment.d1_30 {
  background: var(--el-color-warning-light-8);
}

.segment.d31_60 {
  background: var(--el-color-warning-light-5);
}

.segment.d61_90 {
  background: var(--el-color-danger-light-7);
}

.segment.d90_plus {
  background: var(--el-color-danger-light-5);
}

.segment.active {
  border-color: var(--el-color-primary);
}

.seg-label,
.seg-amount {
  display: block;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.seg-amount {
  color: var(--el-text-color-secondary);
}

.filters {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  margin-bottom: 12px;
}

.filter {
  width: 140px;
}

.search {
  width: 220px;
}

.muted {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.danger {
  color: var(--el-color-danger);
}

.warning {
  color: var(--el-color-warning);
}

.note {
  width: 100%;
}

.cards {
  display: none;
}

.card {
  padding: 10px 12px;
  margin-bottom: 8px;
  background: var(--el-bg-color);
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 8px;
}

.card-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 4px;
}

.card-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 8px;
}

.card-actions :deep(.el-button + .el-button) {
  margin-left: 0;
}

/* 手机：数字两列、账龄竖排、列表变卡片。 */
@media (max-width: 640px) {
  .tiles {
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 8px;
  }

  .aging {
    flex-direction: column;
    align-items: stretch;
  }

  .bar {
    flex-wrap: wrap;
  }

  .segment {
    flex-basis: calc(50% - 3px);
  }

  .table-wrap {
    display: none;
  }

  .cards {
    display: block;
  }

  .filter,
  .search {
    width: calc(50% - 4px);
  }
}
</style>
