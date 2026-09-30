<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { api, formatDateTime } from '../api'
import DocumentDrawer from '../components/warehouse/DocumentDrawer.vue'
import DocumentEditor from '../components/warehouse/DocumentEditor.vue'
import { ordersChanged, shortageText, WORK_STATUS, WORK_STATUS_TAG } from '../orders'
import {
  completePlan,
  EMPTY_TEXT,
  expectedState,
  itemLabel,
  needsRequisition,
  PRODUCTION_VIEWS,
  progressText,
  viewOf,
  type ProductionItem,
  type ProductionOrder,
  type ProductionView,
} from '../production'
import { useAuthStore } from '../stores/auth'
import { briefText, STATUS_TAG, type DocumentKind } from '../warehouse'

/**
 * 加工（设计文档 §25.11、§25.13）：工人领取订单，先开领料单（商品有配方时按配方领料），逐个商品
 * 标记完成或缺货，全部完成后点"完成订单"开入库单，仓管确认入库后订单进入订单中心的"待发货"，客服
 * 在待办里收到提醒；登记缺货的订单进入"缺货"。现货商品直接从成品库存发货，不需要加工。
 * 工人只看加工需要的信息：商品、规格、数量、备注、期望时间和客户称呼，没有金额和收货信息。手机上
 * 一张卡片一个订单。站内信里的链接带 order（主管指派的订单、仓管确认或退回的单据），打开后直接
 * 显示这个订单。
 */
const PAGE_SIZE = 20
const POLL_MS = 60_000
const route = useRoute()
const router = useRouter()
const auth = useAuthStore()

type Result = { data?: ProductionOrder; error?: unknown }

const view = ref<ProductionView>('mine')
const items = ref<ProductionOrder[]>([])
const total = ref(0)
const page = ref(1)
const q = ref('')
const loading = ref(false)
const acting = ref('')
const counts = ref<Schemas['ProductionCounts'] | null>(null)
const shortage = reactive({
  open: false,
  orderId: '',
  item: null as ProductionItem | null,
  quantity: undefined as number | undefined,
  note: '',
  restockDate: '',
})
const editor = reactive({
  open: false,
  kind: 'requisition' as DocumentKind,
  mode: 'create' as 'create' | 'complete',
  orderId: '',
  orderNo: '',
  intro: '',
})
const docDrawer = reactive({ open: false, id: null as string | null })
let timer: ReturnType<typeof setInterval> | undefined

const manage = computed(() => auth.can('production:assign'))
const views = computed(() => PRODUCTION_VIEWS.filter(([name]) => name !== 'all' || manage.value))

function badge(name: ProductionView): number {
  const c = counts.value
  if (!c) return 0
  if (name === 'pool') return c.pool
  if (name === 'mine') return c.mine
  if (name === 'all') return c.all
  return 0
}

async function loadCounts(): Promise<void> {
  const { data } = await api.GET('/api/v1/production/counts')
  if (data) counts.value = data
}

async function load(): Promise<void> {
  loading.value = true
  const { data, error } = await api.GET('/api/v1/production/orders', {
    params: {
      query: {
        view: view.value,
        q: q.value.trim() || undefined,
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

function onTab(): void {
  page.value = 1
  void load()
}

function search(): void {
  page.value = 1
  void load()
}

/**
 * 执行一个操作。标记商品后用返回的订单更新这张卡片；领取、放弃、完成订单后订单离开当前列表，
 * 重新加载。失败时（例如订单已被别人领取）也重新加载，显示最新的状态。
 */
async function act(
  order: ProductionOrder,
  request: () => Promise<Result>,
  message: string,
  leaves = false,
): Promise<boolean> {
  acting.value = order.id
  const { data, error } = await request()
  acting.value = ''
  if (!data) {
    ElMessage.error(errorMessage(error))
    await refresh()
    return false
  }
  ElMessage.success(message)
  ordersChanged()
  if (leaves) {
    await refresh()
  } else {
    const index = items.value.findIndex((o) => o.id === data.id)
    if (index >= 0) items.value[index] = data
    void loadCounts()
  }
  return true
}

const orderPath = (order: ProductionOrder) => ({ params: { path: { order_id: order.id } } })
const itemPath = (order: ProductionOrder, item: ProductionItem) => ({
  params: { path: { order_id: order.id, order_item_id: item.id } },
})

async function claim(order: ProductionOrder): Promise<void> {
  const ok = await act(
    order,
    () => api.POST('/api/v1/production/orders/{order_id}/claim', orderPath(order)),
    order.requisition_required
      ? `已领取 ${order.no}，先开领料单`
      : `已领取 ${order.no}，在"我的加工"里标记进度`,
    true,
  )
  // 商品有配方：领取后接着开领料单（按配方预填）。
  if (ok && order.requisition_required) openRequisition(order)
}

function openRequisition(order: ProductionOrder): void {
  Object.assign(editor, {
    open: true,
    kind: 'requisition',
    mode: 'create',
    orderId: order.id,
    orderNo: order.no,
    intro: '',
  })
}

function openDocument(id: string): void {
  Object.assign(docDrawer, { open: true, id })
}

function onDocument(): void {
  ordersChanged()
  void refresh()
}

async function release(order: ProductionOrder): Promise<void> {
  try {
    await ElMessageBox.confirm('放弃后订单回到"待领取"，已标记的进度会保留。', '放弃加工', {
      confirmButtonText: '放弃',
      type: 'warning',
    })
  } catch {
    return
  }
  await act(
    order,
    () => api.POST('/api/v1/production/orders/{order_id}/release', orderPath(order)),
    '已退回待领取',
    true,
  )
}

function done(order: ProductionOrder, item: ProductionItem): Promise<boolean> {
  return act(
    order,
    () =>
      api.POST('/api/v1/production/orders/{order_id}/items/{order_item_id}/done', itemPath(order, item)),
    `${itemLabel(item)} 已完成`,
  )
}

function undo(order: ProductionOrder, item: ProductionItem): Promise<boolean> {
  return act(
    order,
    () =>
      api.POST('/api/v1/production/orders/{order_id}/items/{order_item_id}/undo', itemPath(order, item)),
    `已撤销 ${itemLabel(item)} 的完成`,
  )
}

function restock(order: ProductionOrder, item: ProductionItem): Promise<boolean> {
  return act(
    order,
    () =>
      api.POST(
        '/api/v1/production/orders/{order_id}/items/{order_item_id}/restock',
        itemPath(order, item),
      ),
    `${itemLabel(item)} 已到货，回到待加工`,
  )
}

function openShortage(order: ProductionOrder, item: ProductionItem): void {
  Object.assign(shortage, {
    open: true,
    orderId: order.id,
    item,
    quantity: item.shortage_qty ?? undefined,
    note: item.shortage_note ?? '',
    restockDate: item.restock_date ?? '',
  })
}

async function submitShortage(): Promise<void> {
  const order = items.value.find((o) => o.id === shortage.orderId)
  const item = shortage.item
  if (!order || !item) return
  const edited = item.work_status === 'out_of_stock'
  const ok = await act(
    order,
    () =>
      api.PUT('/api/v1/production/orders/{order_id}/items/{order_item_id}/shortage', {
        ...itemPath(order, item),
        body: {
          quantity: shortage.quantity ?? null,
          note: shortage.note.trim() || null,
          restock_date: shortage.restockDate || null,
        },
      }),
    edited ? '已修改缺货信息' : '已登记缺货，客服会收到提醒',
  )
  if (ok) shortage.open = false
}

async function complete(order: ProductionOrder): Promise<void> {
  const plan = completePlan(order)
  if (plan.blocked) {
    ElMessage.warning(plan.blocked)
    return
  }
  if (plan.receipt) {
    // 开入库单：生产好的成品由仓管确认入库，之后订单交给客服发货。
    Object.assign(editor, {
      open: true,
      kind: 'receipt',
      mode: 'complete',
      orderId: order.id,
      orderNo: order.no,
      intro: plan.markAll ? plan.message : '',
    })
    return
  }
  try {
    await ElMessageBox.confirm(plan.message, '完成订单', {
      confirmButtonText: plan.markAll ? '一并完成' : '完成订单',
      type: plan.markAll ? 'warning' : 'info',
    })
  } catch {
    return
  }
  await act(
    order,
    () =>
      api.POST('/api/v1/production/orders/{order_id}/complete', {
        ...orderPath(order),
        body: { mark_all: plan.markAll, note: '' },
      }),
    `${order.no} 加工完成，已交给客服`,
    true,
  )
}

// 站内信链接（/production?order=…）：切到订单所在的列表，按订单号搜索出这一个订单。
async function openLinked(): Promise<void> {
  const id = route.query.order
  if (typeof id !== 'string' || !id) return
  const { data, error } = await api.GET('/api/v1/production/orders/{order_id}', {
    params: { path: { order_id: id } },
  })
  void router.replace({ query: { ...route.query, order: undefined } })
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  const target = viewOf(data, auth.me?.id)
  view.value = target === 'all' && !manage.value ? 'mine' : target
  q.value = data.no
  page.value = 1
}

watch(
  () => route.query.order,
  async (id) => {
    if (!id) return
    await openLinked()
    await refresh()
  },
)

onMounted(async () => {
  if (route.query.order) {
    await openLinked()
  } else {
    // 手上有加工中的订单时先看"我的加工"，没有时看"待领取"。
    await loadCounts()
    if (counts.value && !counts.value.mine && counts.value.pool) view.value = 'pool'
  }
  await refresh()
  timer = setInterval(() => {
    if (document.visibilityState !== 'visible' || shortage.open || acting.value) return
    if (editor.open || docDrawer.open) return
    void refresh()
  }, POLL_MS)
})
onBeforeUnmount(() => clearInterval(timer))
</script>

<template>
  <div class="production">
    <div class="page-header">
      <h2>加工</h2>
      <el-input
        v-model="q"
        clearable
        placeholder="订单号或商品"
        class="search"
        data-testid="production-search"
        @keyup.enter="search"
        @clear="search"
      />
    </div>

    <el-tabs v-model="view" data-testid="production-views" @tab-change="onTab">
      <el-tab-pane v-for="[name, label] in views" :key="name" :name="name">
        <template #label>
          <span :data-testid="`production-view-${name}`">
            {{ label }}
            <el-badge
              v-if="badge(name)"
              :value="badge(name)"
              :max="99"
              :type="name === 'pool' ? 'warning' : 'primary'"
              class="badge"
            />
          </span>
        </template>
      </el-tab-pane>
    </el-tabs>

    <el-alert
      v-if="view === 'mine' && counts?.mine_shortage"
      type="warning"
      :closable="false"
      show-icon
      class="tip"
      :title="`有 ${counts.mine_shortage} 个订单缺货：客服会跟进（等到货、换货或取消），到货后点「到货」继续加工。`"
    />

    <div v-loading="loading" class="cards" data-testid="production-list">
      <el-empty v-if="!loading && !items.length" :description="EMPTY_TEXT[view]" :image-size="80" />
      <el-card
        v-for="order in items"
        :key="order.id"
        shadow="never"
        class="card"
        :class="{ short: order.shortage && !order.processed_at }"
        data-testid="production-order"
        :data-order-no="order.no"
      >
        <div class="card-head">
          <span class="no">{{ order.no }}</span>
          <el-tag
            v-if="order.shortage && !order.processed_at"
            type="danger"
            size="small"
            data-testid="production-order-shortage"
            >缺货</el-tag
          >
          <el-tag v-if="order.processed_at" type="success" size="small" data-testid="production-order-processed"
            >加工完成</el-tag
          >
          <span class="progress" data-testid="production-progress">{{ progressText(order) }}</span>
        </div>
        <dl class="meta">
          <dt>客户</dt>
          <dd data-testid="production-customer">{{ order.customer_name ?? '—' }}</dd>
          <dt>期望时间</dt>
          <dd :class="order.processed_at ? '' : (expectedState(order.expected_at) ?? '')">
            {{ order.expected_at ? formatDateTime(order.expected_at) : '—' }}
          </dd>
          <template v-if="order.worker_name && view !== 'mine'">
            <dt>加工人</dt>
            <dd data-testid="production-worker">{{ order.worker_name }}</dd>
          </template>
          <template v-if="order.customer_note">
            <dt>客户备注</dt>
            <dd class="note" data-testid="production-customer-note">{{ order.customer_note }}</dd>
          </template>
          <template v-if="order.internal_note">
            <dt>内部备注</dt>
            <dd class="note" data-testid="production-internal-note">{{ order.internal_note }}</dd>
          </template>
          <template v-if="order.processed_at">
            <dt>完成时间</dt>
            <dd>{{ formatDateTime(order.processed_at) }}</dd>
          </template>
        </dl>

        <div v-if="order.documents.length || order.material_short.length" class="documents">
          <el-tag
            v-for="d in order.documents"
            :key="d.id"
            :type="STATUS_TAG[d.status]"
            effect="plain"
            size="small"
            class="document"
            data-testid="production-document"
            :data-no="d.no"
            @click="openDocument(d.id)"
            >{{ briefText(d) }}</el-tag
          >
          <el-tag
            v-if="order.material_short.length"
            type="warning"
            size="small"
            data-testid="production-material-short"
            >材料不够：{{ order.material_short.join('、') }}</el-tag
          >
        </div>
        <el-alert
          v-if="order.can_work && needsRequisition(order)"
          type="warning"
          :closable="false"
          show-icon
          class="tip"
          title="先开领料单：这个订单的商品有配方，按配方领料后再加工。"
          data-testid="production-need-requisition"
        />
        <el-alert
          v-if="order.receipt"
          :type="order.receipt.status === 'rejected' ? 'error' : 'info'"
          :closable="false"
          show-icon
          class="tip"
          :title="
            order.receipt.status === 'rejected'
              ? `入库单 ${order.receipt.no} 被仓管退回：${order.receipt.reject_reason ?? ''}`
              : `入库单 ${order.receipt.no} 等仓管确认，确认后订单交给客服发货`
          "
          data-testid="production-receipt"
        />

        <ul class="items">
          <li
            v-for="item in order.items"
            :key="item.id"
            class="item"
            data-testid="production-item"
            :data-item-name="item.name"
          >
            <div class="item-main">
              <div class="item-name">
                {{ item.name }} <span v-if="item.spec" class="muted">{{ item.spec }}</span>
              </div>
              <div v-if="item.raw_text" class="muted">客户原话：{{ item.raw_text }}</div>
              <div v-if="item.work_status === 'out_of_stock'" class="shortage" data-testid="production-item-shortage">
                {{ shortageText(item) }}
              </div>
              <div v-else-if="item.done_by_name && item.done_at" class="muted">
                {{ item.done_by_name }} {{ formatDateTime(item.done_at) }}
              </div>
            </div>
            <span class="qty">× {{ item.quantity }}</span>
            <el-tag
              v-if="item.ready_made && item.work_status !== 'out_of_stock'"
              size="small"
              type="success"
              effect="plain"
              data-testid="production-item-ready-made"
              >现货，不用加工</el-tag
            >
            <el-tag v-else size="small" :type="WORK_STATUS_TAG[item.work_status]" data-testid="production-item-status">{{
              WORK_STATUS[item.work_status]
            }}</el-tag>
            <el-tag v-if="item.stock_short" size="small" type="warning" effect="plain" data-testid="production-item-stock-short"
              >库存不足</el-tag
            >
            <div v-if="order.can_work && !order.receipt && !item.ready_made" class="item-actions">
              <el-button
                v-if="item.work_status === 'pending'"
                size="small"
                type="success"
                plain
                :disabled="acting === order.id || needsRequisition(order)"
                :title="needsRequisition(order) ? '请先开领料单' : ''"
                data-testid="production-item-done"
                @click="done(order, item)"
                >完成</el-button
              >
              <el-button
                v-if="item.work_status === 'done'"
                size="small"
                :disabled="acting === order.id"
                data-testid="production-item-undo"
                @click="undo(order, item)"
                >撤销</el-button
              >
              <el-button
                v-if="item.work_status !== 'done'"
                size="small"
                type="danger"
                plain
                :disabled="acting === order.id"
                data-testid="production-item-shortage-edit"
                @click="openShortage(order, item)"
                >{{ item.work_status === 'out_of_stock' ? '修改缺货' : '缺货' }}</el-button
              >
              <el-button
                v-if="item.work_status === 'out_of_stock'"
                size="small"
                type="primary"
                plain
                :disabled="acting === order.id"
                data-testid="production-item-restock"
                @click="restock(order, item)"
                >到货</el-button
              >
            </div>
          </li>
        </ul>

        <div v-if="order.can_claim || order.can_work" class="card-actions">
          <el-button
            v-if="order.can_claim"
            type="primary"
            :loading="acting === order.id"
            data-testid="production-claim"
            @click="claim(order)"
            >领取</el-button
          >
          <template v-if="order.can_work">
            <span v-if="order.shortage" class="muted blocked" data-testid="production-complete-blocked"
              >有缺货的商品，到货后才能完成</span
            >
            <el-button :disabled="acting === order.id" data-testid="production-release" @click="release(order)"
              >放弃</el-button
            >
            <el-button
              v-if="order.receipt"
              type="primary"
              plain
              data-testid="production-receipt-open"
              @click="openDocument(order.receipt.id)"
              >{{ order.receipt.status === 'rejected' ? '修改入库单' : '查看入库单' }}</el-button
            >
            <template v-else>
              <el-button
                :type="needsRequisition(order) ? 'primary' : 'default'"
                :disabled="acting === order.id"
                data-testid="production-open-requisition"
                @click="openRequisition(order)"
                >{{ order.documents.some((d) => d.kind === 'requisition') ? '补领材料' : '开领料单' }}</el-button
              >
              <el-button
                type="success"
                :disabled="order.shortage || needsRequisition(order)"
                :loading="acting === order.id"
                data-testid="production-complete"
                @click="complete(order)"
                >完成订单</el-button
              >
            </template>
          </template>
        </div>
      </el-card>
    </div>

    <div v-if="total > PAGE_SIZE" class="page-footer">
      <el-pagination
        v-model:current-page="page"
        :page-size="PAGE_SIZE"
        :total="total"
        layout="total, prev, pager, next"
        @current-change="load"
      />
    </div>

    <DocumentEditor
      v-model="editor.open"
      :kind="editor.kind"
      :mode="editor.mode"
      :order-id="editor.orderId"
      :order-no="editor.orderNo"
      :intro="editor.intro"
      @saved="onDocument"
    />
    <DocumentDrawer v-model="docDrawer.open" :document-id="docDrawer.id" @changed="onDocument" />

    <el-dialog
      v-model="shortage.open"
      :title="shortage.item?.work_status === 'out_of_stock' ? '修改缺货' : '登记缺货'"
      width="min(440px, 92vw)"
      append-to-body
      data-testid="shortage-dialog"
    >
      <p v-if="shortage.item" class="dialog-item">
        {{ itemLabel(shortage.item) }} <span class="muted">订购 {{ shortage.item.quantity }}</span>
      </p>
      <el-form label-position="top">
        <el-form-item label="缺多少（不填表示全部都缺）">
          <el-input-number
            v-model="shortage.quantity"
            :min="1"
            :max="shortage.item?.quantity ?? 1"
            :placeholder="`全部 ${shortage.item?.quantity ?? ''}`"
            class="full"
            data-testid="shortage-quantity"
          />
        </el-form-item>
        <el-form-item label="预计到货日期">
          <div class="full" data-testid="shortage-date">
            <el-date-picker
              v-model="shortage.restockDate"
              type="date"
              value-format="YYYY-MM-DD"
              placeholder="不确定可以不填"
              :disabled-date="(d: Date) => d.getTime() < Date.now() - 24 * 3600 * 1000"
              class="full"
            />
          </div>
        </el-form-item>
        <el-form-item label="说明">
          <el-input
            v-model="shortage.note"
            maxlength="200"
            show-word-limit
            placeholder="例如：缺哪种料、可以用什么替代"
            data-testid="shortage-note"
          />
        </el-form-item>
      </el-form>
      <p class="muted">登记后订单进入订单中心的"缺货"，客服会收到"缺货处理"待办。</p>
      <template #footer>
        <el-button @click="shortage.open = false">取消</el-button>
        <el-button
          type="danger"
          :loading="acting === shortage.orderId"
          data-testid="shortage-submit"
          @click="submitShortage"
          >确定</el-button
        >
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.production {
  max-width: 960px;
}

.search {
  width: 220px;
}

.badge {
  margin-left: 4px;
}

.tip {
  margin-bottom: 12px;
}

.cards {
  display: flex;
  flex-direction: column;
  gap: 12px;
  min-height: 120px;
}

.card.short {
  border-color: var(--el-color-danger-light-5);
}

.card-head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}

.no {
  font-weight: 600;
  font-family: monospace;
  font-size: 15px;
}

.progress {
  margin-left: auto;
  color: var(--el-text-color-secondary);
  font-size: 13px;
}

.meta {
  display: grid;
  grid-template-columns: 72px 1fr;
  gap: 4px 8px;
  margin: 10px 0;
  font-size: 13px;
}

.meta dt {
  color: var(--el-text-color-secondary);
}

.meta dd {
  margin: 0;
  word-break: break-word;
}

.note {
  white-space: pre-wrap;
}

.overdue {
  color: var(--el-color-danger);
  font-weight: 600;
}

.soon {
  color: var(--el-color-warning-dark-2);
}

.items {
  list-style: none;
  margin: 0;
  padding: 0;
  border-top: 1px solid var(--el-border-color-lighter);
}

.item {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  padding: 10px 0;
  border-bottom: 1px solid var(--el-border-color-lighter);
}

.item-main {
  flex: 1;
  min-width: 160px;
}

.item-name {
  font-size: 14px;
}

.qty {
  font-weight: 600;
  min-width: 40px;
  text-align: right;
}

.item-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.item-actions .el-button + .el-button {
  margin-left: 0;
}

.shortage {
  color: var(--el-color-danger);
  font-size: 12px;
  margin-top: 2px;
}

.muted {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.card-actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: flex-end;
  gap: 8px;
  margin-top: 12px;
}

.card-actions .el-button + .el-button {
  margin-left: 0;
}

.blocked {
  flex-basis: 100%;
  text-align: right;
}

.dialog-item {
  margin: 0 0 12px;
  font-weight: 500;
}

.documents {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 8px;
}

.document {
  cursor: pointer;
}

.full {
  width: 100%;
}

@media (max-width: 768px) {
  .page-header {
    flex-wrap: wrap;
    gap: 8px;
  }

  .search {
    width: 100%;
  }

  .card :deep(.el-card__body) {
    padding: 12px;
  }

  .item-actions {
    width: 100%;
    justify-content: flex-end;
  }

  .card-actions .el-button {
    flex: 1;
  }
}
</style>
