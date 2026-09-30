<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { computed, nextTick, reactive, ref, watch } from 'vue'

import { api } from '../../api'
import { focusField, mergeInto, quantityTotal, today, type PickedItem } from '../../documents'
import {
  CHANGE_REASONS,
  discountRate,
  formTotals,
  money,
  ORDER_STATUS,
  ORDER_STATUS_TAG,
  ordersChanged,
  PAYMENT_METHOD,
  RECEIVER_FIELDS,
  type ChangeReason,
  type FormLine,
  type OrderDetail,
  type PaymentMethod,
  type Product,
} from '../../orders'
import { useAuthStore } from '../../stores/auth'
import { qty } from '../../warehouse'
import DocSheet, { type SheetStep } from '../documents/DocSheet.vue'
import ItemEntry from '../documents/ItemEntry.vue'
import ProductPicker from './ProductPicker.vue'

/**
 * 新建或修改订单（设计文档 §25.5、§25.9、§25.14）：统一的单据页——单据头（客户、收款方式、期望时间、
 * 收货信息）、带表头的商品明细（录入行输入名称或代码回车加入、扫码、批量选择；已经有的商品累加数量）、
 * 金额（商品金额、优惠、应收合计）、备注，以及固定在底部的操作栏（Ctrl+S 保存）。
 * - 新建：选择客户（从工作台打开时是当前客户）；可以保存草稿或直接提交审核。AI 预填的内容带着依据的
 *   消息，员工核对后保存。
 * - 修改：改价、改商品、改数量时必须选择原因；收货信息不填的项保持原值；已确认的订单可以把修改后的
 *   内容告知客户（不需要客户再次确认）。
 * 单价默认是建议零售价，改价和优惠需要 order:price 权限。
 */
const open = defineModel<boolean>({ required: true })
const props = defineProps<{
  order?: OrderDetail | null
  customerId?: string | null
  customerName?: string | null
  sessionId?: string | null
  prefill?: Schemas['OrderSuggestion'] | null
  source?: 'staff' | 'copilot' | 'sidebar'
}>()
const emit = defineEmits<{ saved: [order: OrderDetail] }>()

/** 明细的一行：表单里的商品行，加上显示用的代码、单位和可用库存。 */
interface Line extends FormLine {
  code: string | null
  unit: string
  available: number | null
}

const FLOW = ['draft', 'pending_review', 'confirmed', 'fulfilling', 'shipped', 'completed']

const auth = useAuthStore()
const editing = computed(() => !!props.order)
const canPrice = computed(() => auth.can('order:price'))
const settings = ref<Schemas['OrderSettings'] | null>(null)
const saving = ref(false)
const customers = ref<{ id: string; name: string }[]>([])
const searching = ref(false)
const lines = ref<Line[]>([])
const original = ref<Line[]>([])
type Focusable = { focus: () => void }
const qtyInputs = ref<(Focusable | null)[]>([])
const priceInputs = ref<(Focusable | null)[]>([])
const entry = ref<InstanceType<typeof ItemEntry> | null>(null)
const form = reactive({
  customerId: '',
  discount: '0',
  receiver: { name: '', phone: '', address: '' },
  paymentMethod: '' as PaymentMethod | '',
  expectedAt: '',
  customerNote: '',
  internalNote: '',
  reason: '' as ChangeReason | '',
  note: '',
  notify: false,
  evidence: [] as string[],
})

const methods = computed(() =>
  (settings.value?.payment_methods ?? ['online', 'cod', 'deposit', 'credit']).map(
    (m) => [m, PAYMENT_METHOD[m] ?? m] as const,
  ),
)
const totals = computed(() => formTotals(lines.value, form.discount))
const rate = computed(() => discountRate(lines.value, totals.value.total))
const overLimit = computed(
  () =>
    settings.value !== null &&
    rate.value > settings.value.discount_limit + 0.001 &&
    !auth.can('order:credit'),
)
const key = (l: FormLine) => `${l.product_id ?? ''}|${l.raw_text ?? l.name}|${l.quantity}|${l.unit_price ?? ''}`
const itemsChanged = computed(
  () =>
    !editing.value ||
    lines.value.length !== original.value.length ||
    lines.value.some((l, i) => key(l) !== key(original.value[i]!)),
)
const discountChanged = computed(
  () => editing.value && Number(form.discount || 0) !== Number(props.order?.discount ?? 0),
)
const contentChanged = computed(() => editing.value && (itemsChanged.value || discountChanged.value))
const notifiable = computed(() => ['confirmed', 'fulfilling'].includes(props.order?.status ?? ''))
const quantitySum = computed(() => quantityTotal(lines.value))
const orderDate = computed(() => today(props.order ? new Date(props.order.created_at) : new Date()))
const status = computed(() =>
  props.order
    ? { label: ORDER_STATUS[props.order.status] ?? props.order.status, type: ORDER_STATUS_TAG[props.order.status] ?? 'info' }
    : { label: '新建', type: 'info' as const },
)
const steps = computed<SheetStep[]>(() => {
  const current = props.order?.status
  if (!current) return []
  if (current === 'cancelled') return [{ label: '已取消', state: 'error' }]
  const index = FLOW.indexOf(current)
  return FLOW.map((s, i) => ({
    label: ORDER_STATUS[s] ?? s,
    state: i < index ? 'done' : i === index ? 'current' : 'todo',
  }))
})

function fromItem(item: Schemas['OrderItemOut']): Line {
  return {
    product_id: item.product_id,
    name: item.name,
    spec: item.spec,
    raw_text: item.product_id ? null : item.raw_text,
    quantity: item.quantity,
    list_price: item.list_price,
    unit_price: item.unit_price,
    code: item.code,
    unit: item.unit,
    available: item.stock_available ?? null,
  }
}

function fromSuggestion(item: Schemas['OrderSuggestionLine']): Line {
  return {
    product_id: item.product_id,
    name: item.name,
    spec: item.spec,
    raw_text: item.product_id ? null : item.raw_text,
    quantity: item.quantity,
    list_price: item.retail_price,
    unit_price: item.retail_price,
    code: null,
    unit: '',
    available: null,
  }
}

function reset(): void {
  const order = props.order
  const prefill = props.prefill
  lines.value = order ? order.items.map(fromItem) : (prefill?.items.map(fromSuggestion) ?? [])
  original.value = lines.value.map((l) => ({ ...l }))
  Object.assign(form, {
    customerId: order?.customer_id ?? props.customerId ?? '',
    discount: order?.discount ?? '0',
    receiver: {
      name: prefill?.receiver.name ?? '',
      phone: prefill?.receiver.phone ?? '',
      address: prefill?.receiver.address ?? '',
    },
    paymentMethod: order?.payment_method ?? prefill?.payment_hint ?? '',
    expectedAt: order?.expected_at ?? '',
    customerNote: order?.customer_note ?? prefill?.customer_note ?? '',
    internalNote: order?.internal_note ?? '',
    reason: '',
    note: '',
    notify: false,
    evidence: prefill?.evidence_message_ids ?? [],
  })
  const id = form.customerId
  const name = order?.customer_name ?? props.customerName
  customers.value = id && name ? [{ id, name }] : []
}

watch(open, async (value) => {
  if (!value) return
  reset()
  if (!settings.value) {
    const { data } = await api.GET('/api/v1/orders/settings')
    if (data) settings.value = data
  }
})

async function searchCustomers(q: string): Promise<void> {
  searching.value = true
  const { data } = await api.GET('/api/v1/customers', {
    params: { query: { q: q.trim() || undefined, limit: 20 } },
  })
  searching.value = false
  customers.value = (data?.items ?? []).map((c) => ({ id: c.id, name: c.display_name }))
}

/** 明细每一行的数量、单价输入框（录入后跳到数量，回车跳到下一格）。 */
function inputRef(list: (Focusable | null)[], index: number) {
  return (el: unknown) => {
    list[index] = (el as Focusable | null) ?? null
  }
}

function focusQty(index: number): void {
  void nextTick(() => focusField(qtyInputs.value[index]))
}

/** 录入行或批量选择加入商品：已经有的累加数量，光标跳到这一行的数量。 */
function addItems(items: PickedItem[]): void {
  const index = mergeInto(lines.value, items, (l) => l.product_id, (i) => ({
    product_id: i.id,
    name: i.name,
    spec: i.spec,
    raw_text: null,
    quantity: i.quantity,
    list_price: i.price,
    unit_price: i.price,
    code: i.code,
    unit: i.unit,
    available: i.available,
  }))
  if (index >= 0) focusQty(index)
}

/** 数量里回车：能改价的跳到单价，否则回到录入行。 */
function afterQty(index: number): void {
  if (canPrice.value && priceInputs.value[index]) focusField(priceInputs.value[index])
  else entry.value?.focus()
}

function addText(): void {
  lines.value.push({
    product_id: null,
    name: '',
    spec: '',
    raw_text: '',
    quantity: 1,
    list_price: null,
    unit_price: null,
    code: null,
    unit: '',
    available: null,
  })
}

/** 把没有匹配商品库的行对应到商品。 */
function mapLine(index: number, product: Product): void {
  const line = lines.value[index]
  if (!line) return
  Object.assign(line, {
    product_id: product.id,
    name: product.name,
    spec: product.spec,
    raw_text: null,
    list_price: product.retail_price,
    unit_price: product.retail_price,
    code: product.code,
    unit: product.unit,
    available: product.stock_available,
  })
}

function lineAmount(line: FormLine): string {
  if (line.unit_price === null || line.unit_price === '') return '待定价'
  return money(Number(line.unit_price) * line.quantity)
}

/** 单价只在改过价时提交：新商品按建议零售价，原有商品保持原来的单价。 */
function lineBody(line: FormLine, index: number): Schemas['LineIn'] {
  const before = editing.value ? original.value[index] : undefined
  const baseline =
    before && before.product_id === line.product_id && before.raw_text === line.raw_text
      ? before.unit_price
      : line.list_price
  const price = line.unit_price === '' ? null : line.unit_price
  const text = (line.raw_text ?? line.name).trim()
  return {
    product_id: line.product_id,
    quantity: line.quantity,
    unit_price: canPrice.value && price !== null && price !== baseline ? price : null,
    raw_text: line.product_id ? null : text,
    name: line.product_id ? null : text.slice(0, 128),
  }
}

function receiverBody(): Schemas['ReceiverIn'] {
  const r = form.receiver
  return {
    name: r.name.trim() || null,
    phone: r.phone.trim() || null,
    address: r.address.trim() || null,
  }
}

function check(): boolean {
  if (!editing.value && !form.customerId) {
    ElMessage.warning('请选择客户')
    return false
  }
  if (!lines.value.length) {
    ElMessage.warning('请至少添加一个商品')
    return false
  }
  if (lines.value.some((l) => !l.product_id && !(l.raw_text ?? l.name).trim())) {
    ElMessage.warning('没有匹配商品库的商品行请填写商品说明')
    return false
  }
  if (contentChanged.value && !form.reason) {
    ElMessage.warning('改价、改商品、改数量时请选择原因')
    return false
  }
  return true
}

async function save(submit = true): Promise<void> {
  if (saving.value || !check()) return
  saving.value = true
  const result = editing.value ? await update() : await create(submit)
  saving.value = false
  if (!result) return
  ordersChanged()
  open.value = false
  emit('saved', result)
}

async function create(submit: boolean): Promise<OrderDetail | null> {
  const { data, error } = await api.POST('/api/v1/orders', {
    body: {
      customer_id: form.customerId,
      session_id: props.sessionId ?? null,
      items: lines.value.map(lineBody),
      discount: canPrice.value ? form.discount || '0' : '0',
      receiver: receiverBody(),
      payment_method: form.paymentMethod || null,
      expected_at: form.expectedAt || null,
      customer_note: form.customerNote.trim(),
      internal_note: form.internalNote.trim(),
      evidence_message_ids: form.evidence,
      source: props.source ?? 'staff',
      submit,
    },
  })
  if (!data) {
    ElMessage.error(errorMessage(error))
    return null
  }
  ElMessage.success(submit ? `订单 ${data.no} 已提交审核` : `已保存草稿 ${data.no}`)
  return data
}

async function update(): Promise<OrderDetail | null> {
  const order = props.order!
  const body: Schemas['OrderUpdate'] = { version: order.version, notify_customer: form.notify }
  if (itemsChanged.value) body.items = lines.value.map(lineBody)
  if (discountChanged.value) body.discount = form.discount || '0'
  const receiver = receiverBody()
  if (receiver.name || receiver.phone || receiver.address) body.receiver = receiver
  if ((form.paymentMethod || null) !== order.payment_method) {
    body.payment_method = form.paymentMethod || null
  }
  if ((form.expectedAt || null) !== order.expected_at) body.expected_at = form.expectedAt || null
  if (form.customerNote.trim() !== order.customer_note) body.customer_note = form.customerNote.trim()
  if (form.internalNote.trim() !== order.internal_note) body.internal_note = form.internalNote.trim()
  if (contentChanged.value) {
    body.reason = form.reason || null
    body.note = form.note.trim() || null
  }
  const { data, error } = await api.PATCH('/api/v1/orders/{order_id}', {
    params: { path: { order_id: order.id } },
    body,
  })
  if (!data) {
    ElMessage.error(errorMessage(error))
    return null
  }
  if (data.notice?.status === 'sent') ElMessage.success('已保存，并把最新内容告知客户')
  else if (data.notice) ElMessage.warning(`已保存；${data.notice.reason ?? '未能通知客户'}`)
  else ElMessage.success('已保存')
  return data.order
}
</script>

<template>
  <DocSheet
    v-model="open"
    :title="editing ? '修改订单' : '新建订单'"
    :no="order?.no ?? null"
    :status="status"
    :steps="steps"
    testid="order-form"
    @save="save(true)"
  >
    <el-alert
      v-if="prefill"
      type="info"
      :closable="false"
      show-icon
      class="tip"
      title="AI 已根据对话预填，请核对商品、数量和收货信息后保存。"
    />

    <section class="doc-section">
      <div class="doc-section-head"><h4>基本信息</h4></div>
      <div class="doc-fields">
        <div class="doc-field">
          <label class="required">客户</label>
          <span v-if="editing" class="value">{{ order?.customer_name ?? '—' }}</span>
          <el-select
            v-else
            v-model="form.customerId"
            filterable
            remote
            :remote-method="searchCustomers"
            :loading="searching"
            :disabled="!!customerId"
            placeholder="搜索客户"
            data-testid="order-customer"
          >
            <el-option v-for="c in customers" :key="c.id" :label="c.name" :value="c.id" />
          </el-select>
        </div>
        <div class="doc-field">
          <label>下单日期</label>
          <span class="value">{{ orderDate }}</span>
        </div>
        <div class="doc-field">
          <label>收款方式</label>
          <el-select
            v-model="form.paymentMethod"
            clearable
            placeholder="确认订单时最终确定"
            data-testid="order-payment-method"
          >
            <el-option v-for="[value, label] in methods" :key="value" :label="label" :value="value" />
          </el-select>
        </div>
        <div v-for="[field, label] in RECEIVER_FIELDS" :key="field" class="doc-field" :class="{ wide: field === 'address' }">
          <label>{{ label }}</label>
          <el-input
            v-model="form.receiver[field]"
            :maxlength="field === 'address' ? 200 : 32"
            :placeholder="editing ? `${order?.receiver[field] ?? '未填写'}（不填保持不变）` : ''"
            :data-testid="`receiver-${field}`"
          />
        </div>
        <div class="doc-field">
          <label>期望时间</label>
          <el-date-picker
            v-model="form.expectedAt"
            type="datetime"
            value-format="YYYY-MM-DDTHH:mm:ssZ"
            placeholder="客户期望的送货或服务时间"
          />
        </div>
      </div>
      <p v-if="order?.payment_hint" class="doc-muted hint">客户提到的付款方式：{{ PAYMENT_METHOD[order.payment_hint] }}</p>
    </section>

    <section class="doc-section">
      <div class="doc-section-head">
        <h4>商品明细</h4>
        <el-button size="small" data-testid="order-add-text" @click="addText">添加未匹配的商品</el-button>
      </div>
      <table class="doc-grid" data-testid="order-lines">
        <colgroup>
          <col class="c-seq" />
          <col />
          <col class="c-unit" />
          <col class="c-num" />
          <col class="c-qty" />
          <col class="c-money" />
          <col class="c-price" />
          <col class="c-money" />
          <col class="c-ops" />
        </colgroup>
        <thead>
          <tr>
            <th class="seq">#</th>
            <th>商品</th>
            <th>单位</th>
            <th class="num">可用库存</th>
            <th>数量</th>
            <th class="num">建议零售价</th>
            <th>单价</th>
            <th class="num">金额</th>
            <th class="ops"></th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(line, index) in lines" :key="index" :data-testid="`order-line-${index}`">
            <td class="seq">{{ index + 1 }}</td>
            <td class="main">
              <template v-if="line.product_id">
                <div class="item-name">{{ line.name }}</div>
                <div class="item-sub">{{ [line.code, line.spec].filter(Boolean).join(' · ') }}</div>
              </template>
              <div v-else class="unmatched">
                <el-input
                  v-model="line.raw_text"
                  size="small"
                  placeholder="客户说的商品（没有匹配商品库）"
                  maxlength="200"
                  :data-testid="`order-line-text-${index}`"
                />
                <ProductPicker
                  placeholder="对应到商品库"
                  :testid="`order-line-map-${index}`"
                  @pick="(p) => mapLine(index, p)"
                />
              </div>
            </td>
            <td data-label="单位">{{ line.unit || '—' }}</td>
            <td class="num" data-label="可用">
              <span :class="{ warn: line.available !== null && line.available < line.quantity }">{{
                line.available === null ? '—' : qty(line.available)
              }}</span>
            </td>
            <td data-label="数量">
              <el-input-number
                :ref="inputRef(qtyInputs, index)"
                v-model="line.quantity"
                :min="1"
                :max="settings?.max_quantity ?? 100000"
                size="small"
                controls-position="right"
                :data-testid="`order-qty-${index}`"
                @keydown.enter.prevent="afterQty(index)"
              />
            </td>
            <td class="num" data-label="建议零售价">{{ money(line.list_price) }}</td>
            <td data-label="单价">
              <el-input
                :ref="inputRef(priceInputs, index)"
                v-model="line.unit_price"
                :disabled="!canPrice"
                placeholder="待定价"
                size="small"
                class="price-input"
                :data-testid="`order-price-${index}`"
                @keydown.enter.prevent="entry?.focus()"
              >
                <template #prefix>¥</template>
              </el-input>
            </td>
            <td class="num" data-label="金额">{{ lineAmount(line) }}</td>
            <td class="ops">
              <el-button link type="danger" size="small" :data-testid="`order-line-remove-${index}`" @click="lines.splice(index, 1)"
                >删除</el-button
              >
            </td>
          </tr>
          <tr class="entry">
            <td class="seq">+</td>
            <td colspan="8">
              <ItemEntry ref="entry" source="sales" testid="order-add-product" @add="addItems" />
            </td>
          </tr>
        </tbody>
        <tfoot>
          <tr>
            <td class="seq"></td>
            <td>合计</td>
            <td colspan="2" data-testid="order-line-count">共 {{ lines.length }} 项</td>
            <td>{{ quantitySum }}</td>
            <td colspan="2"></td>
            <td class="num">{{ money(totals.items) }}</td>
            <td></td>
          </tr>
        </tfoot>
      </table>
      <p v-if="!canPrice" class="doc-muted hint">单价按建议零售价；改价和优惠需要有改价权限的同事处理。</p>
    </section>

    <section class="doc-section money">
      <div class="doc-totals">
        <span class="label">商品金额</span>
        <span>{{ money(totals.items) }}</span>
        <span class="label">优惠</span>
        <el-input v-model="form.discount" :disabled="!canPrice" size="small" class="discount" data-testid="order-discount">
          <template #prefix>¥</template>
        </el-input>
        <template v-if="rate > 0">
          <span class="label">优惠率</span>
          <span>{{ rate.toFixed(1) }}%</span>
        </template>
        <span class="label">应收合计</span>
        <span class="grand">
          <span data-testid="order-total">{{ money(totals.total) }}</span>
          <el-tag v-if="totals.pending" size="small" type="warning" class="pending">有待定价的商品</el-tag>
        </span>
      </div>
      <p v-if="overLimit" class="warn">
        优惠 {{ rate.toFixed(1) }}% 超过上限 {{ settings?.discount_limit }}%，需要有审批权限的主管修改。
      </p>
    </section>

    <section class="doc-section">
      <div class="doc-section-head"><h4>备注</h4></div>
      <div class="notes">
        <el-input
          v-model="form.customerNote"
          type="textarea"
          :rows="2"
          maxlength="1000"
          placeholder="客户要求（客户可见），例如送货时间"
          data-testid="order-customer-note"
        />
        <el-input
          v-model="form.internalNote"
          type="textarea"
          :rows="2"
          maxlength="2000"
          placeholder="内部备注（客户看不到）"
          data-testid="order-internal-note"
        />
      </div>
    </section>

    <section v-if="contentChanged || (editing && notifiable)" class="doc-section change">
      <div class="doc-section-head"><h4>修改说明</h4></div>
      <template v-if="contentChanged">
        <div class="reason">
          <span class="label required">修改原因</span>
          <el-radio-group v-model="form.reason" data-testid="order-reason">
            <el-radio v-for="[value, label] in CHANGE_REASONS" :key="value" :value="value">{{ label }}</el-radio>
          </el-radio-group>
        </div>
        <el-input v-model="form.note" maxlength="500" placeholder="说明（可以不填）" class="note" />
      </template>
      <el-checkbox v-if="editing && notifiable" v-model="form.notify" data-testid="order-notify"
        >把修改后的内容告知客户（不需要客户再次确认）</el-checkbox
      >
    </section>

    <template #footer>
      <span class="doc-foot-summary">
        共 {{ lines.length }} 项 · 应收合计 <b>{{ money(totals.total) }}</b>
      </span>
      <div class="doc-foot-buttons">
        <el-button @click="open = false">取消</el-button>
        <template v-if="editing">
          <el-button type="primary" :loading="saving" data-testid="order-save" @click="save(true)">保存</el-button>
        </template>
        <template v-else>
          <el-button :disabled="saving" data-testid="order-save-draft" @click="save(false)">保存草稿</el-button>
          <el-button type="primary" :loading="saving" data-testid="order-save" @click="save(true)"
            >提交审核</el-button
          >
        </template>
      </div>
    </template>
  </DocSheet>
</template>

<style scoped>
.tip {
  margin-bottom: 16px;
}

.hint {
  margin: 8px 0 0;
}

.unmatched {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.unmatched > * {
  flex: 1 1 180px;
}

.money .discount {
  width: 140px;
}

.pending {
  margin-left: 8px;
  vertical-align: middle;
}

.warn {
  margin: 8px 0 0;
  color: var(--el-color-warning);
  font-size: 12px;
  text-align: right;
}

.notes {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
}

.change {
  padding: 12px;
  border-radius: 6px;
  background: var(--el-color-warning-light-9);
}

.reason {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px 12px;
  margin-bottom: 8px;
}

.label {
  color: var(--el-text-color-secondary);
  font-size: 13px;
}

.label.required::before {
  content: '*';
  margin-right: 2px;
  color: var(--el-color-danger);
}

.note {
  margin-bottom: 8px;
}

@media (max-width: 640px) {
  .notes {
    grid-template-columns: 1fr;
  }
}
</style>
