<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, reactive, ref, watch } from 'vue'
import { useRouter } from 'vue-router'

import { api, formatDateTime } from '../../api'
import {
  copyText,
  describeOrderEvent,
  money,
  ORDER_SOURCE,
  ORDER_STATUS,
  ORDER_STATUS_TAG,
  ordersChanged,
  PAYMENT_CHANNEL,
  PAYMENT_CHANNELS,
  PAYMENT_METHOD,
  PAYMENT_STATUS,
  PAYMENT_STATUS_TAG,
  RECEIVER_FIELDS,
  shortageText,
  WORK_STATUS,
  WORK_STATUS_TAG,
  type OrderDetail,
  type PaymentMethod,
} from '../../orders'
import { lineStockText, shortLines } from '../../inventory'
import { useAuthStore } from '../../stores/auth'
import { TODO_STATUS } from '../../todos'
import SessionDrawer from '../sessions/SessionDrawer.vue'
import OrderFormDialog from './OrderFormDialog.vue'
import RevisionsDialog from './RevisionsDialog.vue'

/**
 * 订单详情（设计文档 §25.9）：商品行与金额、收款方式与收款记录、收货信息、依据的对话、动态、
 * 修改记录（任意两个版本对比）、关联的待办和跟踪链接，以及按权限和状态显示的处理操作。
 * 加工（§25.11）：每个商品的加工进度和缺货，加工人与加工完成时间；登记到货、指派加工人。
 */
const props = withDefaults(defineProps<{ orderId: string | null; size?: string }>(), {
  size: '760px',
})
const emit = defineEmits<{ close: []; changed: [] }>()

// AI 和企业系统创建的订单没有员工作为创建人。
const CREATOR: Record<string, string> = { ai: 'AI', api: '企业系统' }

const auth = useAuthStore()
const router = useRouter()
const detail = ref<OrderDetail | null>(null)
const loading = ref(false)
const acting = ref(false)
const revealed = ref<Record<string, string> | null>(null)
const viewing = ref<string | null>(null)
const editing = ref(false)
const revisionsOpen = ref(false)
const settings = ref<Schemas['OrderSettings'] | null>(null)
const options = ref<Schemas['AssigneeOptions']>({ staff: [], groups: [] })

const confirm = reactive({
  open: false,
  method: '' as PaymentMethod | '',
  deposit: '',
  dueDate: '',
  note: '',
  notify: true,
})
const ship = reactive({ open: false, company: '', trackingNo: '', notify: true })
const cancel = reactive({ open: false, reason: '', notify: true })
const payment = reactive({
  open: false,
  kind: 'payment' as 'payment' | 'refund',
  amount: '',
  channel: 'wechat' as Schemas['PaymentIn']['channel'],
  paidAt: '',
  referenceNo: '',
  proofUrl: '',
  note: '',
})
const assign = reactive({ open: false, mode: 'staff' as 'staff' | 'group', staffId: '', groupId: '' })
const notice = reactive({ open: false, text: '' })
const worker = reactive({ open: false, id: '', options: [] as Schemas['WorkerOption'][] })

const open = computed({
  get: () => props.orderId !== null,
  set: (value) => {
    if (!value) emit('close')
  },
})
const names = computed(() => new Map(options.value.staff.map((s) => [s.id, s.name])))
const receiver = computed(() => revealed.value ?? detail.value?.receiver ?? {})
const review = computed(() => auth.can('order:review'))
const methods = computed(() =>
  (settings.value?.payment_methods ?? ['online', 'cod', 'deposit', 'credit']).map(
    (m) => [m, PAYMENT_METHOD[m] ?? m] as const,
  ),
)
// 已确认之后才有加工进度；待审核的订单不显示"加工"一列。
const producing = computed(() => {
  const d = detail.value
  return !!d && !['draft', 'pending_review'].includes(d.status)
})
const active = computed(() => ['confirmed', 'fulfilling'].includes(detail.value?.status ?? ''))
// 库存不足的商品（§25.12，只提示、不拦截）。
const short = computed(() => shortLines(detail.value?.items ?? []))
const handler = computed(() => {
  const d = detail.value
  if (!d) return ''
  if (d.assignee_name) return d.assignee_name
  return d.skill_group_name ? `${d.skill_group_name}（待认领）` : '待分派'
})

async function load(): Promise<void> {
  const id = props.orderId
  if (!id) return
  loading.value = true
  const { data, error } = await api.GET('/api/v1/orders/{order_id}', {
    params: { path: { order_id: id } },
  })
  loading.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    emit('close')
    return
  }
  detail.value = data
}

watch(
  () => props.orderId,
  async (id) => {
    detail.value = null
    revealed.value = null
    if (!id) return
    await load()
    if (!settings.value) {
      const { data } = await api.GET('/api/v1/orders/settings')
      if (data) settings.value = data
    }
    if (!options.value.staff.length && auth.can('todo:read')) {
      const { data } = await api.GET('/api/v1/todos/assignees')
      if (data) options.value = data
    }
  },
  { immediate: true },
)

type Result = { data?: unknown; error?: unknown }

/** 执行一个操作：成功后刷新详情；返回了通知结果时提示是否已经告知客户。 */
async function run(request: () => Promise<Result>, message: string): Promise<boolean> {
  acting.value = true
  const { data, error } = await request()
  acting.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return false
  }
  const noticeResult = (data as { notice?: Schemas['OrderNotice'] | null }).notice
  if (noticeResult && noticeResult.status !== 'sent') {
    ElMessage.warning(`${message}；${noticeResult.reason ?? '未能通知客户'}`)
  } else {
    ElMessage.success(noticeResult ? `${message}，已通知客户` : message)
  }
  ordersChanged()
  await load()
  emit('changed')
  return true
}

const path = () => ({ params: { path: { order_id: props.orderId ?? '' } } })

function submit(): Promise<boolean> {
  return run(() => api.POST('/api/v1/orders/{order_id}/submit', path()), '已提交审核')
}

function start(): Promise<boolean> {
  return run(() => api.POST('/api/v1/orders/{order_id}/start', path()), '已开始处理')
}

function openConfirm(): void {
  const d = detail.value
  if (!d) return
  const suggested = d.payment_method ?? d.payment_hint ?? methods.value[0]?.[0] ?? ''
  Object.assign(confirm, { open: true, method: suggested, deposit: '', dueDate: '', note: '', notify: true })
}

async function submitConfirm(): Promise<void> {
  if (!confirm.method) {
    ElMessage.warning('请选择收款方式')
    return
  }
  const ok = await run(
    () =>
      api.POST('/api/v1/orders/{order_id}/confirm', {
        ...path(),
        body: {
          payment_method: confirm.method as PaymentMethod,
          deposit_amount: confirm.method === 'deposit' ? confirm.deposit || null : null,
          credit_due_date: confirm.method === 'credit' ? confirm.dueDate || null : null,
          note: confirm.note.trim() || null,
          notify_customer: confirm.notify,
        },
      }),
    '订单已确认',
  )
  if (ok) confirm.open = false
}

async function submitShip(): Promise<void> {
  if (!ship.company.trim() || !ship.trackingNo.trim()) {
    ElMessage.warning('请填写物流公司和单号')
    return
  }
  const ok = await run(
    () =>
      api.POST('/api/v1/orders/{order_id}/ship', {
        ...path(),
        body: {
          shipping_company: ship.company.trim(),
          tracking_no: ship.trackingNo.trim(),
          notify_customer: ship.notify,
        },
      }),
    '已登记发货',
  )
  if (ok) ship.open = false
}

async function complete(): Promise<void> {
  let notify = true
  try {
    await ElMessageBox.confirm('确认订单已经完成？', '完成订单', {
      confirmButtonText: '完成并通知客户',
      cancelButtonText: '只完成，不通知',
      distinguishCancelAndClose: true,
    })
  } catch (action) {
    if (action !== 'cancel') return
    notify = false
  }
  await run(
    () =>
      api.POST('/api/v1/orders/{order_id}/complete', {
        ...path(),
        body: { notify_customer: notify },
      }),
    '订单已完成',
  )
}

async function submitCancel(): Promise<void> {
  if (!cancel.reason.trim()) {
    ElMessage.warning('请填写取消原因')
    return
  }
  const ok = await run(
    () =>
      api.POST('/api/v1/orders/{order_id}/cancel', {
        ...path(),
        body: { reason: cancel.reason.trim(), notify_customer: cancel.notify },
      }),
    '订单已取消',
  )
  if (ok) cancel.open = false
}

function openPayment(kind: 'payment' | 'refund'): void {
  const d = detail.value
  if (!d) return
  const paid = Number(d.paid_amount) - Number(d.refunded_amount)
  Object.assign(payment, {
    open: true,
    kind,
    amount: kind === 'payment' ? d.outstanding : paid > 0 ? paid.toFixed(2) : '',
    channel: 'wechat',
    paidAt: '',
    referenceNo: '',
    proofUrl: '',
    note: '',
  })
}

async function submitPayment(): Promise<void> {
  if (!(Number(payment.amount) > 0)) {
    ElMessage.warning('请填写金额')
    return
  }
  const ok = await run(
    () =>
      api.POST('/api/v1/orders/{order_id}/payments', {
        ...path(),
        body: {
          kind: payment.kind,
          amount: payment.amount,
          channel: payment.channel,
          paid_at: payment.paidAt || null,
          reference_no: payment.referenceNo.trim() || null,
          proof_url: payment.proofUrl.trim() || null,
          note: payment.note.trim() || null,
        },
      }),
    payment.kind === 'refund' ? '已登记退款' : '已登记收款',
  )
  if (ok) payment.open = false
}

async function voidPayment(paymentId: string): Promise<void> {
  let reason: string
  try {
    const result = await ElMessageBox.prompt('作废原因（会写入修改记录）', '作废收款记录', {
      confirmButtonText: '作废',
      cancelButtonText: '取消',
      inputPattern: /\S/,
      inputErrorMessage: '请填写原因',
    })
    reason = (result as { value: string }).value.trim()
  } catch {
    return
  }
  await run(
    () =>
      api.POST('/api/v1/orders/{order_id}/payments/{payment_id}/void', {
        params: { path: { order_id: props.orderId ?? '', payment_id: paymentId } },
        body: { reason },
      }),
    '已作废',
  )
}

async function reveal(): Promise<void> {
  const { data, error } = await api.POST('/api/v1/orders/{order_id}/reveal', path())
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  revealed.value = data.receiver
}

async function regenerate(): Promise<void> {
  try {
    await ElMessageBox.confirm('重新生成后，旧的跟踪链接立即失效。', '重新生成跟踪链接', {
      confirmButtonText: '重新生成',
      cancelButtonText: '取消',
    })
  } catch {
    return
  }
  await run(() => api.POST('/api/v1/orders/{order_id}/tracking-link', path()), '已重新生成')
}

async function copyLink(): Promise<void> {
  const url = detail.value?.tracking_url
  if (url && (await copyText(url))) ElMessage.success('已复制跟踪链接')
}

async function submitAssign(): Promise<void> {
  const body =
    assign.mode === 'staff'
      ? { assignee_id: assign.staffId || null, skill_group_id: null }
      : { assignee_id: null, skill_group_id: assign.groupId || null }
  const ok = await run(
    () => api.POST('/api/v1/orders/{order_id}/assign', { ...path(), body }),
    '已转交',
  )
  if (ok) assign.open = false
}

async function submitNotice(): Promise<void> {
  if (!notice.text.trim()) return
  acting.value = true
  const { data, error } = await api.POST('/api/v1/orders/{order_id}/notify', {
    ...path(),
    body: { text: notice.text.trim() },
  })
  acting.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  if (data.status === 'sent') ElMessage.success('已发送给客户')
  else ElMessage.warning(data.reason ?? '未能通知客户')
  notice.open = false
  await load()
}

async function restock(itemId: string): Promise<void> {
  await run(
    () =>
      api.POST('/api/v1/orders/{order_id}/items/{order_item_id}/restock', {
        params: { path: { order_id: props.orderId ?? '', order_item_id: itemId } },
      }),
    '已登记到货，回到待加工',
  )
}

async function openWorker(): Promise<void> {
  worker.id = detail.value?.worker_id ?? ''
  worker.open = true
  if (!worker.options.length) {
    const { data } = await api.GET('/api/v1/production/workers')
    if (data) worker.options = data.items
  }
}

async function submitWorker(): Promise<void> {
  const ok = await run(
    () =>
      api.POST('/api/v1/production/orders/{order_id}/assign', {
        ...path(),
        body: { worker_id: worker.id || null },
      }),
    worker.id ? '已指派加工人' : '已退回待领取',
  )
  if (ok) worker.open = false
}

function openTodo(id: string): void {
  void router.push({ path: '/todos', query: { view: 'all', id } })
}

async function onSaved(): Promise<void> {
  await load()
  emit('changed')
}
</script>

<template>
  <el-drawer v-model="open" :size="size" :title="detail ? `订单 ${detail.no}` : '订单'" data-testid="order-drawer">
    <div v-loading="loading" class="body">
      <template v-if="detail">
        <div class="head">
          <el-tag :type="ORDER_STATUS_TAG[detail.status]" data-testid="order-status">{{
            ORDER_STATUS[detail.status]
          }}</el-tag>
          <el-tag :type="PAYMENT_STATUS_TAG[detail.payment_status]" effect="plain" data-testid="order-payment-status">{{
            PAYMENT_STATUS[detail.payment_status]
          }}</el-tag>
          <el-tag type="info" effect="plain">{{ ORDER_SOURCE[detail.source] ?? detail.source }}</el-tag>
          <el-tag v-if="detail.modified" type="warning" effect="plain">修改过</el-tag>
          <el-tag v-if="detail.ai_error" type="danger" effect="plain">AI 识别错误</el-tag>
          <el-tag v-if="detail.receivable_overdue" type="danger">暂欠逾期</el-tag>
          <el-tag v-if="detail.shortage && active" type="danger" data-testid="order-shortage">缺货</el-tag>
          <el-tag v-if="detail.processed_at && detail.status === 'fulfilling'" type="success" data-testid="order-processed"
            >加工完成</el-tag
          >
        </div>
        <el-alert
          v-if="detail.missing.length && ['draft', 'pending_review'].includes(detail.status)"
          type="warning"
          :closable="false"
          show-icon
          class="tip"
          :title="`提交或确认前还需要：${detail.missing.join('、')}`"
        />

        <section class="block">
          <h4>商品</h4>
          <el-table :data="detail.items" size="small" data-testid="order-items">
            <el-table-column label="商品" min-width="200">
              <template #default="{ row }">
                <div>
                  {{ row.name }} <span class="muted">{{ row.spec }}</span>
                  <el-tag v-if="!row.matched" size="small" type="warning">未匹配商品库</el-tag>
                </div>
                <div v-if="row.raw_text" class="muted">客户原话：{{ row.raw_text }}</div>
                <div v-if="producing && row.work_status === 'out_of_stock'" class="short" data-testid="order-item-shortage">
                  缺货：{{ shortageText(row) }}
                </div>
                <div
                  v-if="lineStockText(row)"
                  :class="row.stock_short ? 'short' : 'muted'"
                  data-testid="order-item-stock"
                >
                  {{ lineStockText(row) }}
                </div>
              </template>
            </el-table-column>
            <el-table-column prop="quantity" label="数量" width="70" align="right" />
            <el-table-column label="建议零售价" width="110" align="right">
              <template #default="{ row }">{{ money(row.list_price) }}</template>
            </el-table-column>
            <el-table-column label="单价" width="110" align="right">
              <template #default="{ row }">
                <span :class="{ changed: row.list_price !== row.unit_price }">{{
                  row.unit_price === null ? '待定价' : money(row.unit_price)
                }}</span>
              </template>
            </el-table-column>
            <el-table-column label="金额" width="110" align="right">
              <template #default="{ row }">{{ money(row.amount) }}</template>
            </el-table-column>
            <el-table-column v-if="detail.cost_amount !== null" label="成本价" width="100" align="right">
              <template #default="{ row }">{{ money(row.cost_price) }}</template>
            </el-table-column>
            <el-table-column v-if="producing" label="加工" width="100">
              <template #default="{ row }">
                <el-tag size="small" :type="WORK_STATUS_TAG[row.work_status]" data-testid="order-item-work">{{
                  WORK_STATUS[row.work_status]
                }}</el-tag>
                <div v-if="row.work_status === 'out_of_stock' && detail.allowed.restock">
                  <el-button
                    link
                    type="primary"
                    size="small"
                    :disabled="acting"
                    data-testid="order-item-restock"
                    @click="restock(row.id)"
                    >登记到货</el-button
                  >
                </div>
                <div v-else-if="row.done_by_name" class="muted">{{ row.done_by_name }}</div>
              </template>
            </el-table-column>
          </el-table>
          <dl class="amounts">
            <dt>商品金额</dt>
            <dd>{{ money(detail.items_amount) }}</dd>
            <dt>优惠</dt>
            <dd>{{ money(detail.discount) }}</dd>
            <dt>合计</dt>
            <dd><b data-testid="order-total-amount">{{ money(detail.total) }}</b></dd>
            <template v-if="detail.cost_amount !== null">
              <dt>成本合计</dt>
              <dd>{{ money(detail.cost_amount) }}</dd>
            </template>
          </dl>
        </section>

        <section class="block">
          <div class="block-head">
            <h4>收款</h4>
            <span v-if="detail.allowed.payment">
              <el-button link type="primary" size="small" data-testid="order-add-payment" @click="openPayment('payment')"
                >登记收款</el-button
              >
              <el-button
                v-if="Number(detail.paid_amount) > Number(detail.refunded_amount)"
                link
                type="primary"
                size="small"
                @click="openPayment('refund')"
                >登记退款</el-button
              >
            </span>
          </div>
          <dl>
            <dt>收款方式</dt>
            <dd data-testid="order-payment-method-text">
              {{ detail.payment_method ? PAYMENT_METHOD[detail.payment_method] : '待确认' }}
              <span v-if="!detail.payment_method && detail.payment_hint" class="muted"
                >（客户提到：{{ PAYMENT_METHOD[detail.payment_hint] }}）</span
              >
            </dd>
            <template v-if="detail.deposit_amount">
              <dt>定金</dt>
              <dd>{{ money(detail.deposit_amount) }}</dd>
            </template>
            <template v-if="detail.credit_due_date">
              <dt>约定付款日期</dt>
              <dd :class="{ overdue: detail.receivable_overdue }">
                {{ detail.credit_due_date }}
                <span v-if="detail.credit_approved_by_name" class="muted"
                  >（{{ detail.credit_approved_by_name }} 同意暂欠）</span
                >
              </dd>
            </template>
            <dt>已收 / 未收</dt>
            <dd>
              {{ money(Number(detail.paid_amount) - Number(detail.refunded_amount)) }} /
              <span data-testid="order-outstanding">{{ money(detail.outstanding) }}</span>
            </dd>
          </dl>
          <el-table v-if="detail.payments.length" :data="detail.payments" size="small" data-testid="order-payments">
            <el-table-column label="类型" width="60">
              <template #default="{ row }">{{ row.kind === 'refund' ? '退款' : '收款' }}</template>
            </el-table-column>
            <el-table-column label="金额" width="100" align="right">
              <template #default="{ row }">
                <span :class="{ voided: row.voided_at }">{{ money(row.amount) }}</span>
              </template>
            </el-table-column>
            <el-table-column label="渠道" width="80">
              <template #default="{ row }">{{ PAYMENT_CHANNEL[row.channel] }}</template>
            </el-table-column>
            <el-table-column label="时间" width="150">
              <template #default="{ row }">{{ formatDateTime(row.paid_at) }}</template>
            </el-table-column>
            <el-table-column label="流水号 / 登记人" min-width="140">
              <template #default="{ row }">
                {{ row.reference_no ?? '' }} <span class="muted">{{ row.recorded_by_name ?? '' }}</span>
                <div v-if="row.voided_at" class="muted">已作废：{{ row.void_reason }}</div>
              </template>
            </el-table-column>
            <el-table-column v-if="detail.allowed.payment" label="" width="60">
              <template #default="{ row }">
                <el-button v-if="!row.voided_at" link type="danger" size="small" @click="voidPayment(row.id)"
                  >作废</el-button
                >
              </template>
            </el-table-column>
          </el-table>
        </section>

        <section class="block">
          <div class="block-head">
            <h4>收货与客户</h4>
            <el-button
              v-if="detail.allowed.reveal && !revealed"
              link
              type="primary"
              size="small"
              data-testid="order-reveal"
              @click="reveal"
              >查看完整信息（记审计）</el-button
            >
          </div>
          <dl>
            <dt>客户</dt>
            <dd>
              <router-link v-if="detail.customer_id" :to="`/customers?customer=${detail.customer_id}`">{{
                detail.customer_name
              }}</router-link>
              <span v-else>—</span>
            </dd>
            <template v-for="[field, label] in RECEIVER_FIELDS" :key="field">
              <dt>{{ label }}</dt>
              <dd :data-testid="`order-receiver-${field}`">{{ receiver[field] ?? '—' }}</dd>
            </template>
            <template v-if="detail.expected_at">
              <dt>期望时间</dt>
              <dd>{{ formatDateTime(detail.expected_at) }}</dd>
            </template>
            <template v-if="detail.customer_note">
              <dt>客户要求</dt>
              <dd>{{ detail.customer_note }}</dd>
            </template>
            <template v-if="detail.internal_note">
              <dt>内部备注</dt>
              <dd>{{ detail.internal_note }}</dd>
            </template>
          </dl>
        </section>

        <section class="block">
          <dl>
            <dt>处理人</dt>
            <dd data-testid="order-handler">{{ handler }}</dd>
            <dt>下单</dt>
            <dd>
              {{ formatDateTime(detail.created_at) }}
              <span class="muted">{{ CREATOR[detail.created_by_type] ?? detail.created_by_name ?? '' }}</span>
            </dd>
            <template v-if="detail.confirmed_at">
              <dt>确认</dt>
              <dd>{{ formatDateTime(detail.confirmed_at) }}</dd>
            </template>
            <template v-if="producing && (detail.worker_name || detail.allowed.assign_worker)">
              <dt>加工人</dt>
              <dd data-testid="order-worker">
                <span v-if="detail.worker_name"
                  >{{ detail.worker_name }}
                  <span v-if="detail.claimed_at" class="muted">{{ formatDateTime(detail.claimed_at) }} 开始</span></span
                >
                <span v-else class="muted">待领取</span>
                <el-button
                  v-if="detail.allowed.assign_worker"
                  link
                  type="primary"
                  size="small"
                  data-testid="order-assign-worker"
                  @click="openWorker"
                  >{{ detail.worker_name ? '改派' : '指派' }}</el-button
                >
              </dd>
            </template>
            <template v-if="detail.processed_at">
              <dt>加工完成</dt>
              <dd data-testid="order-processed-at">
                {{ formatDateTime(detail.processed_at) }}
                <span class="muted">{{ detail.processed_by_name ?? '' }}</span>
              </dd>
            </template>
            <template v-if="detail.shipping_company">
              <dt>物流</dt>
              <dd data-testid="order-shipping">{{ detail.shipping_company }} {{ detail.tracking_no }}</dd>
            </template>
            <template v-if="detail.cancel_reason">
              <dt>取消原因</dt>
              <dd>{{ detail.cancel_reason }}</dd>
            </template>
            <template v-if="detail.external_no">
              <dt>企业系统单号</dt>
              <dd>{{ detail.external_no }}</dd>
            </template>
            <template v-if="detail.status !== 'draft'">
              <dt>跟踪链接</dt>
              <dd class="link">
                <span v-if="detail.tracking_active" class="url" data-testid="order-tracking-url">{{
                  detail.tracking_url
                }}</span>
                <span v-else class="muted">已失效</span>
                <el-button v-if="detail.tracking_active" link type="primary" size="small" @click="copyLink"
                  >复制</el-button
                >
                <el-button v-if="review" link size="small" @click="regenerate">重新生成</el-button>
              </dd>
            </template>
          </dl>
        </section>

        <section v-if="detail.todos.length" class="block">
          <h4>关联的待办</h4>
          <p v-for="t in detail.todos" :key="t.id" class="line">
            <el-button link type="primary" @click="openTodo(t.id)">{{ t.no }}</el-button>
            {{ t.type_name }} · {{ t.title }}（{{ TODO_STATUS[t.status] ?? t.status
            }}{{ t.assignee_name ? `，${t.assignee_name}` : '' }}）
          </p>
        </section>

        <section v-if="detail.evidence.length" class="block">
          <div class="block-head">
            <h4>依据的对话</h4>
            <el-button v-if="detail.session_id" link type="primary" size="small" @click="viewing = detail.session_id"
              >查看会话</el-button
            >
          </div>
          <p v-for="m in detail.evidence" :key="m.id" class="line" data-testid="order-evidence">
            <span class="muted">{{ m.sender_type === 'customer' ? '客户' : '客服' }}：</span>{{ m.text }}
          </p>
        </section>

        <section class="block">
          <div class="block-head">
            <h4>动态</h4>
            <el-button link type="primary" size="small" data-testid="order-revisions-open" @click="revisionsOpen = true"
              >修改记录（{{ detail.revisions.length }} 个版本）</el-button
            >
          </div>
          <el-timeline>
            <el-timeline-item v-for="e in detail.events" :key="e.id" :timestamp="formatDateTime(e.created_at)">
              <span data-testid="order-event">{{ describeOrderEvent(e, names) }}</span>
              <el-tag v-if="e.public" size="small" type="info" effect="plain" class="public">客户可见</el-tag>
            </el-timeline-item>
          </el-timeline>
        </section>
      </template>
    </div>

    <template v-if="detail" #footer>
      <div class="actions">
        <el-button v-if="detail.allowed.edit" :disabled="acting" data-testid="order-edit" @click="editing = true"
          >修改</el-button
        >
        <el-button v-if="detail.allowed.assign" :disabled="acting" @click="assign.open = true">转交</el-button>
        <el-button v-if="review && detail.customer_id" :disabled="acting" @click="notice.open = true"
          >通知客户</el-button
        >
        <el-button v-if="detail.allowed.cancel" :disabled="acting" data-testid="order-cancel" @click="cancel.open = true"
          >取消订单</el-button
        >
        <el-button v-if="detail.allowed.submit" type="primary" :disabled="acting" data-testid="order-submit" @click="submit"
          >提交审核</el-button
        >
        <el-button
          v-if="detail.allowed.confirm"
          type="primary"
          :disabled="acting"
          data-testid="order-confirm"
          @click="openConfirm"
          >确认订单</el-button
        >
        <el-button v-if="detail.allowed.start" type="primary" :disabled="acting" data-testid="order-start" @click="start"
          >开始处理</el-button
        >
        <el-button v-if="detail.allowed.ship" type="primary" :disabled="acting" data-testid="order-ship" @click="ship.open = true"
          >登记发货</el-button
        >
        <el-button
          v-if="detail.allowed.complete"
          type="primary"
          :disabled="acting"
          data-testid="order-complete"
          @click="complete"
          >完成</el-button
        >
      </div>
    </template>

    <OrderFormDialog v-if="detail" v-model="editing" :order="detail" @saved="onSaved" />
    <RevisionsDialog v-if="detail" v-model="revisionsOpen" :order="detail" />

    <el-dialog v-model="worker.open" title="指派加工人" width="400px" append-to-body data-testid="worker-dialog">
      <el-select v-model="worker.id" clearable placeholder="退回待领取" data-testid="worker-select" class="full">
        <el-option v-for="w in worker.options" :key="w.id" :label="w.name" :value="w.id" />
      </el-select>
      <p class="muted">已确认的订单指派后开始处理；清空表示退回待领取，由工人自己领取。</p>
      <template #footer>
        <el-button @click="worker.open = false">取消</el-button>
        <el-button type="primary" :loading="acting" data-testid="worker-submit" @click="submitWorker">确定</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="confirm.open" title="确认订单" width="460px" append-to-body data-testid="confirm-order-dialog">
      <el-alert
        v-if="short.length"
        type="warning"
        :closable="false"
        show-icon
        class="ship-tip"
        title="这些商品的可用库存不够，确认后仍然可以继续处理："
        data-testid="confirm-stock-short"
      >
        <div v-for="line in short" :key="line">{{ line }}</div>
      </el-alert>
      <el-form label-width="96px">
        <el-form-item label="收款方式" required>
          <el-radio-group v-model="confirm.method" data-testid="confirm-method">
            <el-radio v-for="[value, label] in methods" :key="value" :value="value">{{ label }}</el-radio>
          </el-radio-group>
        </el-form-item>
        <el-form-item v-if="confirm.method === 'deposit'" label="定金" required>
          <el-input v-model="confirm.deposit" data-testid="confirm-deposit"><template #prefix>¥</template></el-input>
        </el-form-item>
        <el-form-item v-if="confirm.method === 'credit'" label="约定付款日期" required>
          <el-date-picker v-model="confirm.dueDate" type="date" value-format="YYYY-MM-DD" />
          <p v-if="!auth.can('order:credit')" class="muted">暂欠需要有审批权限的主管确认。</p>
        </el-form-item>
        <el-form-item label="说明">
          <el-input v-model="confirm.note" maxlength="500" />
        </el-form-item>
        <el-form-item>
          <el-checkbox v-model="confirm.notify" data-testid="confirm-notify"
            >把确认信息和跟踪链接发给客户</el-checkbox
          >
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="confirm.open = false">取消</el-button>
        <el-button type="primary" :loading="acting" data-testid="confirm-submit" @click="submitConfirm"
          >确认订单</el-button
        >
      </template>
    </el-dialog>

    <el-dialog v-model="ship.open" title="登记发货" width="420px" append-to-body data-testid="ship-dialog">
      <el-alert
        v-if="detail && detail.worker_name && !detail.processed_at"
        type="warning"
        :closable="false"
        show-icon
        class="ship-tip"
        data-testid="ship-unprocessed"
        :title="`${detail.worker_name}还没有完成加工${detail.shortage ? '（有缺货的商品）' : ''}，确认要发货吗？`"
      />
      <el-form label-width="80px">
        <el-form-item label="物流公司" required>
          <el-input v-model="ship.company" maxlength="64" data-testid="ship-company" />
        </el-form-item>
        <el-form-item label="物流单号" required>
          <el-input v-model="ship.trackingNo" maxlength="64" data-testid="ship-no" />
        </el-form-item>
        <el-form-item>
          <el-checkbox v-model="ship.notify">通知客户</el-checkbox>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="ship.open = false">取消</el-button>
        <el-button type="primary" :loading="acting" data-testid="ship-submit" @click="submitShip">登记发货</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="cancel.open" title="取消订单" width="420px" append-to-body>
      <el-form label-position="top">
        <el-form-item label="取消原因" required>
          <el-input v-model="cancel.reason" type="textarea" :rows="2" maxlength="500" data-testid="cancel-reason" />
        </el-form-item>
        <el-form-item>
          <el-checkbox v-model="cancel.notify">通知客户</el-checkbox>
        </el-form-item>
        <p v-if="detail && Number(detail.paid_amount) > Number(detail.refunded_amount)" class="muted">
          这个订单已有收款，取消后请登记退款。
        </p>
      </el-form>
      <template #footer>
        <el-button @click="cancel.open = false">返回</el-button>
        <el-button type="danger" :loading="acting" data-testid="cancel-submit" @click="submitCancel">取消订单</el-button>
      </template>
    </el-dialog>

    <el-dialog
      v-model="payment.open"
      :title="payment.kind === 'refund' ? '登记退款' : '登记收款'"
      width="460px"
      append-to-body
      data-testid="payment-dialog"
    >
      <el-form label-width="80px">
        <el-form-item label="金额" required>
          <el-input v-model="payment.amount" data-testid="payment-amount"><template #prefix>¥</template></el-input>
        </el-form-item>
        <el-form-item label="渠道" required>
          <el-select v-model="payment.channel" data-testid="payment-channel">
            <el-option v-for="[value, label] in PAYMENT_CHANNELS" :key="value" :label="label" :value="value" />
          </el-select>
        </el-form-item>
        <el-form-item label="时间">
          <el-date-picker
            v-model="payment.paidAt"
            type="datetime"
            value-format="YYYY-MM-DDTHH:mm:ssZ"
            placeholder="不填为现在"
          />
        </el-form-item>
        <el-form-item label="流水号">
          <el-input v-model="payment.referenceNo" maxlength="64" />
        </el-form-item>
        <el-form-item label="凭证链接">
          <el-input v-model="payment.proofUrl" maxlength="1024" placeholder="https://" />
        </el-form-item>
        <el-form-item label="备注">
          <el-input v-model="payment.note" maxlength="500" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="payment.open = false">取消</el-button>
        <el-button type="primary" :loading="acting" data-testid="payment-submit" @click="submitPayment">登记</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="assign.open" title="转交订单" width="420px" append-to-body>
      <el-form label-width="64px">
        <el-form-item label="交给">
          <el-radio-group v-model="assign.mode">
            <el-radio value="staff">员工</el-radio>
            <el-radio value="group">技能组待认领</el-radio>
          </el-radio-group>
        </el-form-item>
        <el-form-item v-if="assign.mode === 'staff'" label="员工">
          <el-select v-model="assign.staffId" filterable>
            <el-option v-for="s in options.staff" :key="s.id" :label="s.name" :value="s.id" />
          </el-select>
        </el-form-item>
        <el-form-item v-else label="技能组">
          <el-select v-model="assign.groupId" filterable>
            <el-option v-for="g in options.groups" :key="g.id" :label="g.name" :value="g.id" />
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="assign.open = false">取消</el-button>
        <el-button type="primary" :loading="acting" @click="submitAssign">转交</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="notice.open" title="通知客户" width="440px" append-to-body>
      <p class="muted">
        官网访客会收到系统消息；微信客服在回复窗口内直接发送；企业微信客户需要在侧边栏由员工发送。
      </p>
      <el-input v-model="notice.text" type="textarea" :rows="3" maxlength="1000" />
      <template #footer>
        <el-button @click="notice.open = false">取消</el-button>
        <el-button type="primary" :loading="acting" @click="submitNotice">发送</el-button>
      </template>
    </el-dialog>

    <SessionDrawer :session-id="viewing" :staff-names="names" @close="viewing = null" />
  </el-drawer>
</template>

<style scoped>
.body {
  padding: 0 4px;
}

.head {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.tip {
  margin-top: 8px;
}

.block {
  border-top: 1px solid var(--el-border-color-lighter);
  padding: 8px 0;
  margin-top: 8px;
}

.block-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

h4 {
  margin: 4px 0 8px;
  font-size: 13px;
}

dl {
  display: grid;
  grid-template-columns: 104px 1fr;
  gap: 4px 8px;
  margin: 8px 0 0;
  font-size: 13px;
}

dt {
  color: var(--el-text-color-secondary);
}

dd {
  margin: 0;
  word-break: break-all;
}

.amounts {
  max-width: 320px;
  margin-left: auto;
}

.amounts dd {
  text-align: right;
}

.muted {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.changed {
  color: var(--el-color-warning-dark-2);
}

.voided {
  text-decoration: line-through;
  color: var(--el-text-color-secondary);
}

.overdue {
  color: var(--el-color-danger);
}

.line {
  white-space: pre-wrap;
  word-break: break-word;
  margin: 4px 0;
  font-size: 13px;
}

.link {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  align-items: center;
}

.url {
  font-family: monospace;
  font-size: 12px;
}

.public {
  margin-left: 6px;
}

.actions {
  display: flex;
  flex-wrap: wrap;
  justify-content: flex-end;
  gap: 8px;
}

.actions .el-button + .el-button {
  margin-left: 0;
}

.full {
  width: 100%;
}

.short {
  color: var(--el-color-danger);
  font-size: 12px;
}

.ship-tip {
  margin-bottom: 12px;
}
</style>
