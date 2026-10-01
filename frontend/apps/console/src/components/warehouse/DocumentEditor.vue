<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, nextTick, ref, watch } from 'vue'

import { api } from '../../api'
import { focusField, mergeInto, quantityTotal, today, type PickedItem } from '../../documents'
import { useAuthStore } from '../../stores/auth'
import {
  KIND_LABEL,
  qty,
  stockAfterDocument,
  type DocumentKind,
  type ItemKind,
  type WarehouseDocument,
} from '../../warehouse'
import DocSheet from '../documents/DocSheet.vue'
import ItemEntry from '../documents/ItemEntry.vue'

/**
 * 开领料单、入库单（设计文档 §25.13、§25.14）：统一的单据页——单据头（开单日期、开单人、确认人、关联
 * 订单、备注）、带表头的明细（录入行输入名称或代码回车加入、扫码、批量选择；已经有的累加数量）、合计，
 * 以及固定在底部的操作栏（Ctrl+S 提交）。
 *
 * 给订单开单时按配方（领料单：订单数量 × 配方用量，减去已经领过的）或订单（入库单：需要加工的成品）
 * 预填；在仓库页面开领料单时也可以选择关联的订单，按配方带入。领料时库存不够只提示（确认后库存变成
 * 负数），也可以领。仓管开的单、或者设置为不需要确认时提交即生效。
 *
 * mode：create 开单；edit 修改后重新提交（待确认或被退回的单据）；complete 完成加工（开入库单，调用
 * 加工的"完成"接口，还没标记的商品一并标记完成）。
 */
const open = defineModel<boolean>({ required: true })
const props = defineProps<{
  kind: DocumentKind
  mode: 'create' | 'edit' | 'complete'
  orderId?: string | null
  orderNo?: string | null
  document?: WarehouseDocument | null
  intro?: string
}>()
const emit = defineEmits<{ saved: [result: WarehouseDocument | Schemas['ProductionOrder']] }>()

interface Row {
  product_id: string
  code: string | null
  name: string
  spec: string
  unit: string
  planned: number | null
  quantity: number
  stock: number | null
}

type Focusable = { focus: () => void }

const auth = useAuthStore()
const rows = ref<Row[]>([])
const missing = ref<string[]>([])
const note = ref('')
const loading = ref(false)
const saving = ref(false)
const settings = ref<Schemas['WarehouseSettingsOut'] | null>(null)
const qtyInputs = ref<(Focusable | null)[]>([])
const entry = ref<InstanceType<typeof ItemEntry> | null>(null)
// 在仓库页面开领料单时选择的关联订单。
const linked = ref<Schemas['LinkableOrder'] | null>(null)
const linkable = ref<Schemas['LinkableOrder'][]>([])
const linkSearching = ref(false)

const itemKind = computed<ItemKind>(() => (props.kind === 'requisition' ? 'material' : 'goods'))
const noun = computed(() => (props.kind === 'requisition' ? '材料' : '成品'))
const verb = computed(() => (props.kind === 'requisition' ? '领用' : '入库'))
// 仓管自己开的单、或者设置为不需要确认时，提交即生效。
const direct = computed(
  () => auth.can('warehouse:confirm') || settings.value?.confirm_required === false,
)
const title = computed(() => {
  const label = KIND_LABEL[props.kind]
  if (props.mode === 'edit') return `修改${label}`
  if (props.mode === 'complete') return '完成加工：开入库单'
  return `开${label}`
})
// 在仓库页面开领料单（没有指定订单）时可以选择关联的订单。
const canLink = computed(
  () => props.mode === 'create' && props.kind === 'requisition' && !props.orderId,
)
const orderId = computed(() => props.orderId ?? props.document?.order_id ?? linked.value?.id ?? null)
const orderNo = computed(() => props.orderNo ?? props.document?.order_no ?? linked.value?.no ?? null)
const short = computed(() =>
  props.kind === 'requisition' ? rows.value.filter((r) => (r.stock ?? 0) < r.quantity) : [],
)
const quantitySum = computed(() => quantityTotal(rows.value))
const submitText = computed(() => {
  if (props.mode === 'complete') return direct.value ? '完成并入库' : '提交入库单'
  return direct.value ? '提交（直接生效）' : '提交'
})
const keeperText = computed(() => {
  if (direct.value) return '提交后直接生效'
  return settings.value?.effective_keeper_name ?? '管理员'
})

function toRow(line: Schemas['DraftLine'] | Schemas['DocumentLineOut']): Row {
  return {
    product_id: line.product_id,
    code: line.code,
    name: line.name,
    spec: line.spec,
    unit: line.unit,
    planned: line.planned,
    quantity: line.quantity,
    stock: line.stock,
  }
}

async function loadDraft(id: string): Promise<boolean> {
  const { data, error } = await api.GET('/api/v1/warehouse/drafts', {
    params: { query: { kind: props.kind, order_id: id } },
  })
  if (!data) {
    ElMessage.error(errorMessage(error))
    return false
  }
  missing.value = data.missing
  rows.value = data.lines.map(toRow)
  return true
}

watch(open, async (value) => {
  if (!value) return
  rows.value = []
  missing.value = []
  linked.value = null
  linkable.value = []
  note.value = props.document?.note ?? ''
  loading.value = true
  const settingsLoad = api.GET('/api/v1/warehouse/settings')
  if (props.document) {
    rows.value = props.document.lines.map(toRow)
  } else if (props.orderId) {
    await loadDraft(props.orderId)
  }
  settings.value = (await settingsLoad).data ?? null
  loading.value = false
  if (!rows.value.length) void nextTick(() => entry.value?.focus())
})

async function searchOrders(q: string): Promise<void> {
  linkSearching.value = true
  const { data } = await api.GET('/api/v1/warehouse/orders', {
    params: { query: { q: q.trim() || undefined, limit: 20 } },
  })
  linkSearching.value = false
  linkable.value = data?.items ?? []
}

/** 选择关联的订单：明细为空时按配方带入；已经有明细时先问要不要替换。 */
async function link(id: string | null): Promise<void> {
  linked.value = linkable.value.find((o) => o.id === id) ?? null
  if (!linked.value) return
  if (rows.value.length) {
    try {
      await ElMessageBox.confirm(
        `按订单 ${linked.value.no} 的配方带入材料，替换现在的明细？`,
        '按配方带入',
        { confirmButtonText: '替换', cancelButtonText: '保留现在的明细' },
      )
    } catch {
      return
    }
  }
  loading.value = true
  await loadDraft(linked.value.id)
  loading.value = false
}

async function refill(): Promise<void> {
  if (!orderId.value) return
  loading.value = true
  await loadDraft(orderId.value)
  loading.value = false
}

function inputRef(index: number) {
  return (el: unknown) => {
    qtyInputs.value[index] = (el as Focusable | null) ?? null
  }
}

function addItems(items: PickedItem[]): void {
  const index = mergeInto(rows.value, items, (r) => r.product_id, (i) => ({
    product_id: i.id,
    code: i.code,
    name: i.name,
    spec: i.spec,
    unit: i.unit,
    planned: null,
    quantity: i.quantity,
    stock: i.stock,
  }))
  if (index >= 0) void nextTick(() => focusField(qtyInputs.value[index]))
}

function remove(index: number): void {
  rows.value.splice(index, 1)
}

function body(): Schemas['DocumentLineIn'][] {
  // 材料最多三位小数（输入框不限制显示的位数，提交时四舍五入）。
  return rows.value
    .map((r) => ({ ...r, quantity: Math.round((r.quantity ?? 0) * 1000) / 1000 }))
    .filter((r) => r.quantity > 0)
    .map((r) => ({ product_id: r.product_id, quantity: r.quantity, planned: r.planned }))
}

async function save(): Promise<void> {
  if (saving.value || loading.value) return
  const lines = body()
  if (!lines.length) {
    ElMessage.warning(`至少要有一个${noun.value}，数量大于 0`)
    return
  }
  saving.value = true
  let result: { data?: WarehouseDocument | Schemas['ProductionOrder']; error?: unknown }
  if (props.mode === 'complete' && props.orderId) {
    result = await api.POST('/api/v1/production/orders/{order_id}/complete', {
      params: { path: { order_id: props.orderId } },
      body: { mark_all: true, note: note.value.trim(), lines },
    })
  } else if (props.mode === 'edit' && props.document) {
    result = await api.PUT('/api/v1/warehouse/documents/{document_id}', {
      params: { path: { document_id: props.document.id } },
      body: { note: note.value.trim(), lines },
    })
  } else {
    result = await api.POST('/api/v1/warehouse/documents', {
      body: { kind: props.kind, order_id: orderId.value, note: note.value.trim(), lines },
    })
  }
  saving.value = false
  const { data, error } = result
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  ElMessage.success(savedMessage(data))
  open.value = false
  emit('saved', data)
}

function savedMessage(data: WarehouseDocument | Schemas['ProductionOrder']): string {
  const keeper = settings.value?.effective_keeper_name
  const waiting = keeper ? `，等仓管确认（${keeper}）` : '，等仓管确认'
  if ('processed_at' in data) {
    return data.receipt ? `入库单 ${data.receipt.no} 已提交${waiting}` : '已入库，订单交给客服发货'
  }
  return data.status === 'confirmed'
    ? `${data.kind_label} ${data.no} 已生效，库存已更新`
    : `${data.kind_label} ${data.no} 已提交${waiting}`
}
</script>

<template>
  <DocSheet
    v-model="open"
    :title="title"
    :no="document?.no ?? null"
    :status="document ? { label: document.status_label, type: 'warning' } : null"
    :loading="loading"
    testid="document-editor"
    @save="save"
  >
    <el-alert v-if="intro" type="info" :closable="false" show-icon class="tip" :title="intro" />

    <section class="doc-section">
      <div class="doc-section-head"><h4>基本信息</h4></div>
      <div class="doc-fields">
        <div class="doc-field">
          <label>开单日期</label>
          <span class="value">{{ today() }}</span>
        </div>
        <div class="doc-field">
          <label>开单人</label>
          <span class="value">{{ document?.created_by_name ?? auth.me?.display_name ?? '—' }}</span>
        </div>
        <div class="doc-field">
          <label>确认人</label>
          <span class="value" data-testid="document-keeper">{{ keeperText }}</span>
        </div>
        <div class="doc-field wide">
          <label>关联订单</label>
          <el-select
            v-if="canLink"
            :model-value="linked?.id ?? ''"
            filterable
            remote
            clearable
            :remote-method="searchOrders"
            :loading="linkSearching"
            placeholder="可以不选；选择加工中的订单后按配方带入材料"
            data-testid="document-order-select"
            @focus="searchOrders('')"
            @update:model-value="(v: string) => link(v || null)"
          >
            <el-option v-for="o in linkable" :key="o.id" :label="o.no" :value="o.id">
              <span>{{ o.no }}</span>
              <span class="option-sub">{{ o.items }}{{ o.worker_name ? ` · ${o.worker_name}` : '' }}</span>
            </el-option>
          </el-select>
          <span v-else class="value" data-testid="document-order">{{ orderNo ?? '不关联订单' }}</span>
          <el-button
            v-if="orderId && mode !== 'edit'"
            size="small"
            data-testid="document-refill"
            @click="refill"
            >{{ kind === 'requisition' ? '按配方重新带入' : '按订单重新带入' }}</el-button
          >
        </div>
        <div class="doc-field full">
          <label>备注</label>
          <el-input
            v-model="note"
            maxlength="200"
            :placeholder="kind === 'requisition' ? '选填，例如：第二批、补料' : '选填'"
            data-testid="document-note"
          />
        </div>
      </div>
    </section>

    <section class="doc-section">
      <div class="doc-section-head"><h4>{{ noun }}明细</h4></div>
      <el-alert
        v-if="missing.length"
        type="info"
        :closable="false"
        class="tip"
        data-testid="document-missing"
        :title="
          kind === 'requisition'
            ? `没有配方的商品：${missing.join('、')}（需要的材料请手动添加）`
            : `没有对应到商品库的商品：${missing.join('、')}（不能入库）`
        "
      />
      <table class="doc-grid" data-testid="document-lines">
        <colgroup>
          <col class="c-seq" />
          <col />
          <col class="c-unit" />
          <col class="c-num" />
          <col class="c-num" />
          <col class="c-qty" />
          <col class="c-num" />
          <col class="c-ops" />
        </colgroup>
        <thead>
          <tr>
            <th class="seq">#</th>
            <th>{{ noun }}</th>
            <th>单位</th>
            <th class="num">现有库存</th>
            <th class="num">建议数量</th>
            <th>本次{{ verb }}</th>
            <th class="num">{{ verb }}后库存</th>
            <th class="ops"></th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(row, index) in rows" :key="row.product_id" data-testid="document-row" :data-name="row.name">
            <td class="seq">{{ index + 1 }}</td>
            <td class="main">
              <div class="item-name" data-testid="document-line" :data-name="row.name">{{ row.name }}</div>
              <div class="item-sub">{{ [row.code, row.spec].filter(Boolean).join(' · ') }}</div>
            </td>
            <td data-label="单位">{{ row.unit || '—' }}</td>
            <td class="num" data-label="现有">{{ qty(row.stock) }}</td>
            <td class="num" data-label="建议">{{ row.planned === null ? '—' : qty(row.planned) }}</td>
            <td :data-label="`本次${verb}`">
              <el-input-number
                :ref="inputRef(index)"
                v-model="row.quantity"
                :min="0"
                :max="100000000"
                :precision="itemKind === 'goods' ? 0 : undefined"
                :step="1"
                size="small"
                controls-position="right"
                data-testid="document-line-quantity"
                @keydown.enter.prevent="entry?.focus()"
              />
            </td>
            <td class="num" :data-label="`${verb}后`">
              <span
                data-testid="document-line-after"
                :class="{ warn: kind === 'requisition' && stockAfterDocument(kind, row.stock, row.quantity) < 0 }"
                >{{ qty(stockAfterDocument(kind, row.stock, row.quantity)) }}</span
              >
            </td>
            <td class="ops">
              <el-button link type="danger" size="small" data-testid="document-line-remove" @click="remove(index)"
                >删除</el-button
              >
            </td>
          </tr>
          <tr class="entry">
            <td class="seq">+</td>
            <td colspan="7">
              <ItemEntry
                ref="entry"
                source="warehouse"
                :kind="itemKind"
                testid="document-add"
                :placeholder="`添加${noun}：输入名称、代码、规格或拼音首字母，也可以扫码`"
                @add="addItems"
              />
            </td>
          </tr>
        </tbody>
        <tfoot>
          <tr>
            <td class="seq"></td>
            <td>合计</td>
            <td colspan="3" data-testid="document-count">共 {{ rows.length }} 项</td>
            <td>{{ quantitySum }}</td>
            <td colspan="2"></td>
          </tr>
        </tfoot>
      </table>
      <el-alert
        v-if="short.length"
        type="warning"
        :closable="false"
        show-icon
        class="tip after"
        data-testid="document-short"
        :title="`库存不够：${short.map((r) => r.name).join('、')}。可以照常领料，确认后库存会是负数，请及时补货或盘点。`"
      />
    </section>

    <template #footer>
      <span class="doc-foot-summary" data-testid="document-direct">
        <template v-if="direct">提交后直接生效（修改库存）。</template>
        <template v-else>
          提交后等仓管确认{{ settings?.effective_keeper_name ? `（${settings.effective_keeper_name}）` : '' }}，确认后才修改库存{{
            mode === 'complete' ? '，订单随之交给客服发货' : ''
          }}。
        </template>
      </span>
      <div class="doc-foot-buttons">
        <el-button @click="open = false">取消</el-button>
        <el-button type="primary" :loading="saving" :disabled="loading" data-testid="document-submit" @click="save">{{
          submitText
        }}</el-button>
      </div>
    </template>
  </DocSheet>
</template>

<style scoped>
.tip {
  margin-bottom: 12px;
}

.tip.after {
  margin: 12px 0 0;
}

.option-sub {
  margin-left: 8px;
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
</style>
