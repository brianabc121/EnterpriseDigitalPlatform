<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { computed, reactive, ref, watch } from 'vue'

import { api } from '../../api'
import {
  CHANGE_REASONS,
  discountRate,
  formTotals,
  money,
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
import ProductPicker from './ProductPicker.vue'

/**
 * 新建或修改订单（设计文档 §25.5、§25.9）。
 * - 新建：选择客户（从工作台打开时是当前客户），逐行添加商品；可以直接提交审核或先保存草稿。
 *   AI 预填的内容（prefill）带着依据的消息，员工核对后保存。
 * - 修改：改价、改商品、改数量时必须选择原因；收货信息不填的项保持原值；已确认的订单可以把
 *   修改后的内容告知客户（不需要客户再次确认）。
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

const auth = useAuthStore()
const editing = computed(() => !!props.order)
const canPrice = computed(() => auth.can('order:price'))
const settings = ref<Schemas['OrderSettings'] | null>(null)
const saving = ref(false)
const customers = ref<{ id: string; name: string }[]>([])
const searching = ref(false)
const lines = ref<FormLine[]>([])
const original = ref<FormLine[]>([])
const form = reactive({
  customerId: '',
  discount: '0',
  receiver: { name: '', phone: '', address: '' },
  paymentMethod: '' as PaymentMethod | '',
  expectedAt: '',
  customerNote: '',
  internalNote: '',
  submit: true,
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

function fromItem(item: Schemas['OrderItemOut']): FormLine {
  return {
    product_id: item.product_id,
    name: item.name,
    spec: item.spec,
    raw_text: item.product_id ? null : item.raw_text,
    quantity: item.quantity,
    list_price: item.list_price,
    unit_price: item.unit_price,
  }
}

function fromSuggestion(item: Schemas['OrderSuggestionLine']): FormLine {
  return {
    product_id: item.product_id,
    name: item.name,
    spec: item.spec,
    raw_text: item.product_id ? null : item.raw_text,
    quantity: item.quantity,
    list_price: item.retail_price,
    unit_price: item.retail_price,
  }
}

function reset(): void {
  const order = props.order
  const prefill = props.prefill
  lines.value = order
    ? order.items.map(fromItem)
    : (prefill?.items.map(fromSuggestion) ?? [])
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
    submit: true,
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

function addProduct(product: Product): void {
  lines.value.push({
    product_id: product.id,
    name: product.name,
    spec: product.spec,
    raw_text: null,
    quantity: 1,
    list_price: product.retail_price,
    unit_price: product.retail_price,
  })
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

async function save(): Promise<void> {
  if (!check()) return
  saving.value = true
  const result = editing.value ? await update() : await create()
  saving.value = false
  if (!result) return
  ordersChanged()
  open.value = false
  emit('saved', result)
}

async function create(): Promise<OrderDetail | null> {
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
      submit: form.submit,
    },
  })
  if (!data) {
    ElMessage.error(errorMessage(error))
    return null
  }
  ElMessage.success(form.submit ? `订单 ${data.no} 已提交审核` : `已保存草稿 ${data.no}`)
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
  <el-dialog
    v-model="open"
    :title="editing ? `修改订单 ${order?.no}` : '新建订单'"
    width="820px"
    append-to-body
    data-testid="order-form"
  >
    <el-alert
      v-if="prefill"
      type="info"
      :closable="false"
      show-icon
      class="tip"
      title="AI 已根据对话预填，请核对商品、数量和收货信息后保存。"
    />
    <el-form label-width="96px">
      <el-form-item v-if="!editing" label="客户" required>
        <el-select
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
      </el-form-item>

      <el-form-item label="商品" required>
        <div class="lines">
          <div
            v-for="(line, index) in lines"
            :key="index"
            class="line"
            :data-testid="`order-line-${index}`"
          >
            <div class="what">
              <template v-if="line.product_id">
                <span class="name">{{ line.name }}</span>
                <span v-if="line.spec" class="muted">{{ line.spec }}</span>
                <span v-if="line.list_price" class="muted">建议零售价 {{ money(line.list_price) }}</span>
              </template>
              <template v-else>
                <el-input
                  v-model="line.raw_text"
                  placeholder="客户说的商品（没有匹配商品库）"
                  maxlength="200"
                  :data-testid="`order-line-text-${index}`"
                />
                <ProductPicker
                  placeholder="对应到商品库"
                  :testid="`order-line-map-${index}`"
                  @pick="(p) => mapLine(index, p)"
                />
              </template>
            </div>
            <el-input-number
              v-model="line.quantity"
              :min="1"
              :max="settings?.max_quantity ?? 100000"
              size="small"
              class="qty"
              :data-testid="`order-qty-${index}`"
            />
            <el-input
              v-model="line.unit_price"
              :disabled="!canPrice"
              placeholder="待定价"
              size="small"
              class="price"
              :data-testid="`order-price-${index}`"
            >
              <template #prefix>¥</template>
            </el-input>
            <span class="amount">{{ lineAmount(line) }}</span>
            <el-button link type="danger" size="small" @click="lines.splice(index, 1)">删除</el-button>
          </div>
          <div class="add">
            <ProductPicker testid="order-add-product" @pick="addProduct" />
            <el-button size="small" data-testid="order-add-text" @click="addText">
              添加未匹配的商品
            </el-button>
          </div>
          <p v-if="!canPrice" class="muted">单价按建议零售价；改价和优惠需要有改价权限的同事处理。</p>
        </div>
      </el-form-item>

      <el-form-item label="优惠">
        <el-input
          v-model="form.discount"
          :disabled="!canPrice"
          class="discount"
          data-testid="order-discount"
        >
          <template #prefix>¥</template>
        </el-input>
        <span class="totals">
          商品 {{ money(totals.items) }}，合计 <b data-testid="order-total">{{ money(totals.total) }}</b>
          <el-tag v-if="totals.pending" size="small" type="warning">有待定价的商品</el-tag>
        </span>
        <p v-if="overLimit" class="warn">
          优惠 {{ rate.toFixed(1) }}% 超过上限 {{ settings?.discount_limit }}%，需要有审批权限的主管修改。
        </p>
      </el-form-item>

      <el-form-item v-for="[field, label] in RECEIVER_FIELDS" :key="field" :label="label">
        <el-input
          v-model="form.receiver[field]"
          :maxlength="field === 'address' ? 200 : 32"
          :placeholder="editing ? `${order?.receiver[field] ?? '未填写'}（不填保持不变）` : ''"
          :data-testid="`receiver-${field}`"
        />
      </el-form-item>

      <el-form-item label="收款方式">
        <el-select
          v-model="form.paymentMethod"
          clearable
          placeholder="客户选择的方式（确认订单时最终确定）"
          data-testid="order-payment-method"
        >
          <el-option v-for="[value, label] in methods" :key="value" :label="label" :value="value" />
        </el-select>
        <span v-if="order?.payment_hint" class="muted hint"
          >客户提到：{{ PAYMENT_METHOD[order.payment_hint] }}</span
        >
      </el-form-item>
      <el-form-item label="期望时间">
        <el-date-picker v-model="form.expectedAt" type="datetime" value-format="YYYY-MM-DDTHH:mm:ssZ" />
      </el-form-item>
      <el-form-item label="客户要求">
        <el-input
          v-model="form.customerNote"
          type="textarea"
          :rows="2"
          maxlength="1000"
          placeholder="客户可见，例如送货时间"
        />
      </el-form-item>
      <el-form-item label="内部备注">
        <el-input v-model="form.internalNote" type="textarea" :rows="2" maxlength="2000" />
      </el-form-item>

      <template v-if="contentChanged">
        <el-form-item label="修改原因" required>
          <el-radio-group v-model="form.reason" data-testid="order-reason">
            <el-radio v-for="[value, label] in CHANGE_REASONS" :key="value" :value="value">{{
              label
            }}</el-radio>
          </el-radio-group>
        </el-form-item>
        <el-form-item label="说明">
          <el-input v-model="form.note" maxlength="500" placeholder="可以不填" />
        </el-form-item>
      </template>
      <el-form-item v-if="editing && notifiable" label="">
        <el-checkbox v-model="form.notify" data-testid="order-notify"
          >把修改后的内容告知客户（不需要客户再次确认）</el-checkbox
        >
      </el-form-item>
      <el-form-item v-if="!editing" label="">
        <el-checkbox v-model="form.submit" data-testid="order-submit-toggle">直接提交审核</el-checkbox>
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="open = false">取消</el-button>
      <el-button type="primary" :loading="saving" data-testid="order-save" @click="save">
        {{ editing ? '保存' : form.submit ? '提交审核' : '保存草稿' }}
      </el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.tip {
  margin-bottom: 12px;
}

.lines {
  width: 100%;
}

.line {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}

.what {
  flex: 1;
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  min-width: 0;
}

.what .el-input,
.what .el-select {
  flex: 1;
  min-width: 160px;
}

.name {
  font-weight: 500;
}

.qty {
  width: 110px;
}

.price {
  width: 120px;
}

.amount {
  width: 96px;
  text-align: right;
}

.add {
  display: flex;
  gap: 8px;
  align-items: center;
}

.discount {
  width: 140px;
}

.totals {
  margin-left: 12px;
}

.muted {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.hint {
  margin-left: 8px;
}

.warn {
  margin: 4px 0 0;
  width: 100%;
  color: var(--el-color-warning);
  font-size: 12px;
}
</style>
