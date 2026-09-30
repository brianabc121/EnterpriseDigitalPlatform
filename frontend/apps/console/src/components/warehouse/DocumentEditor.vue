<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { computed, ref, watch } from 'vue'

import { api } from '../../api'
import { useAuthStore } from '../../stores/auth'
import {
  KIND_LABEL,
  qty,
  stockAfterDocument,
  type DocumentKind,
  type ItemKind,
  type StockItem,
  type WarehouseDocument,
} from '../../warehouse'

/**
 * 开领料单、入库单（设计文档 §25.13）。给订单开单时按配方（领料单：订单数量 × 配方用量，减去已经
 * 领过的）或订单（入库单：需要加工的成品）预填，可以增减材料或商品、修改数量。领料时库存不够只提示
 * （确认后库存变成负数，记得盘点或补货），也可以领。仓管开的单、或者设置为不需要确认时提交即生效。
 *
 * mode：create 开单；edit 修改后重新提交（待确认或被退回的单据）；complete 完成加工（开入库单，
 * 调用加工的"完成"接口，还没标记的商品一并标记完成）。
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
  name: string
  spec: string
  unit: string
  planned: number | null
  quantity: number
  stock: number | null
}

const auth = useAuthStore()
const rows = ref<Row[]>([])
const missing = ref<string[]>([])
const note = ref('')
const loading = ref(false)
const saving = ref(false)
const settings = ref<Schemas['WarehouseSettingsOut'] | null>(null)
const options = ref<StockItem[]>([])
const searching = ref(false)
const picked = ref('')

const itemKind = computed<ItemKind>(() => (props.kind === 'requisition' ? 'material' : 'goods'))
const noun = computed(() => (props.kind === 'requisition' ? '材料' : '成品'))
// 仓管自己开的单、或者设置为不需要确认时，提交即生效。
const direct = computed(
  () => auth.can('warehouse:confirm') || settings.value?.confirm_required === false,
)
const title = computed(() => {
  const label = KIND_LABEL[props.kind]
  const order = props.orderNo ? ` · 订单 ${props.orderNo}` : ''
  if (props.mode === 'edit') return `修改${label} ${props.document?.no ?? ''}`
  if (props.mode === 'complete') return `完成加工：开入库单${order}`
  return `开${label}${order}`
})
const short = computed(() =>
  props.kind === 'requisition' ? rows.value.filter((r) => (r.stock ?? 0) < r.quantity) : [],
)
const submitText = computed(() => {
  if (props.mode === 'complete') return direct.value ? '完成并入库' : '提交入库单'
  return direct.value ? '提交（直接生效）' : '提交'
})

watch(open, async (value) => {
  if (!value) return
  rows.value = []
  missing.value = []
  options.value = []
  picked.value = ''
  note.value = props.document?.note ?? ''
  loading.value = true
  const settingsLoad = api.GET('/api/v1/warehouse/settings')
  if (props.document) {
    rows.value = props.document.lines.map((line) => ({
      product_id: line.product_id,
      name: line.name,
      spec: line.spec,
      unit: line.unit,
      planned: line.planned,
      quantity: line.quantity,
      stock: line.stock,
    }))
  } else if (props.orderId) {
    const { data, error } = await api.GET('/api/v1/warehouse/drafts', {
      params: { query: { kind: props.kind, order_id: props.orderId } },
    })
    if (!data) {
      ElMessage.error(errorMessage(error))
    } else {
      missing.value = data.missing
      rows.value = data.lines.map((line) => ({
        product_id: line.product_id,
        name: line.name,
        spec: line.spec,
        unit: line.unit,
        planned: line.planned,
        quantity: line.quantity,
        stock: line.stock,
      }))
    }
  }
  settings.value = (await settingsLoad).data ?? null
  loading.value = false
})

async function search(q: string): Promise<void> {
  searching.value = true
  const { data } = await api.GET('/api/v1/warehouse/items', {
    params: { query: { kind: itemKind.value, q: q.trim() || undefined, limit: 20 } },
  })
  searching.value = false
  options.value = data?.items ?? []
}

function add(id: string): void {
  picked.value = ''
  const item = options.value.find((o) => o.id === id)
  if (!item) return
  if (rows.value.some((r) => r.product_id === id)) {
    ElMessage.warning(`「${item.name}」已经在单据里了，直接修改数量`)
    return
  }
  rows.value.push({
    product_id: item.id,
    name: item.name,
    spec: item.spec,
    unit: item.unit,
    planned: null,
    quantity: 1,
    stock: item.stock,
  })
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
      body: { kind: props.kind, order_id: props.orderId ?? null, note: note.value.trim(), lines },
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
  return data.status === 'confirmed' ? `${data.kind_label} ${data.no} 已生效，库存已更新` : `${data.kind_label} ${data.no} 已提交${waiting}`
}
</script>

<template>
  <el-dialog v-model="open" :title="title" width="min(760px, 96vw)" append-to-body data-testid="document-editor">
    <div v-loading="loading">
      <el-alert v-if="intro" type="info" :closable="false" show-icon class="tip" :title="intro" />
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
      <div class="lines" data-testid="document-lines">
        <div v-if="!rows.length" class="empty muted">还没有{{ noun }}，在下面添加</div>
        <div v-for="(row, index) in rows" :key="row.product_id" class="line">
          <div class="line-main">
            <div>
              <span data-testid="document-line" :data-name="row.name">{{ row.name }}</span>
              <span v-if="row.spec" class="muted"> {{ row.spec }}</span>
            </div>
            <div class="muted">
              <template v-if="row.planned !== null">建议 {{ qty(row.planned) }} · </template>
              现有 {{ qty(row.stock) }}
              <span
                data-testid="document-line-after"
                :class="{ minus: kind === 'requisition' && stockAfterDocument(kind, row.stock, row.quantity) < 0 }"
                >→ {{ qty(stockAfterDocument(kind, row.stock, row.quantity)) }}</span
              >
            </div>
          </div>
          <div class="line-qty">
            <el-input-number
              v-model="row.quantity"
              :min="0"
              :max="100000000"
              :precision="itemKind === 'goods' ? 0 : undefined"
              :step="1"
              size="small"
              controls-position="right"
              class="qty"
              data-testid="document-line-quantity"
            />
            <span class="unit">{{ row.unit }}</span>
            <el-button link type="danger" size="small" data-testid="document-line-remove" @click="remove(index)"
              >删除</el-button
            >
          </div>
        </div>
      </div>
      <div class="add">
        <el-select
          v-model="picked"
          filterable
          remote
          clearable
          :remote-method="search"
          :loading="searching"
          :placeholder="`添加${noun}（输入名称或代码搜索）`"
          class="picker"
          data-testid="document-add"
          @focus="search('')"
          @change="add"
        >
          <el-option v-for="o in options" :key="o.id" :label="o.name" :value="o.id">
            <span>{{ o.name }}</span>
            <span class="muted"> {{ o.spec }} 现有 {{ qty(o.stock) }} {{ o.unit }}</span>
          </el-option>
        </el-select>
      </div>
      <el-alert
        v-if="short.length"
        type="warning"
        :closable="false"
        show-icon
        class="tip"
        data-testid="document-short"
        :title="`库存不够：${short.map((r) => r.name).join('、')}。可以照常领料，确认后库存会是负数，请及时补货或盘点。`"
      />
      <el-input
        v-model="note"
        maxlength="200"
        :placeholder="kind === 'requisition' ? '备注（选填），例如：第二批、补料' : '备注（选填）'"
        class="note"
        data-testid="document-note"
      />
      <p class="muted" data-testid="document-direct">
        <template v-if="direct">提交后直接生效（修改库存）。</template>
        <template v-else>
          提交后等仓管确认{{ settings?.effective_keeper_name ? `（${settings.effective_keeper_name}）` : '' }}，确认后才修改库存{{
            mode === 'complete' ? '，订单随之交给客服发货' : ''
          }}。
        </template>
      </p>
    </div>
    <template #footer>
      <el-button @click="open = false">取消</el-button>
      <el-button type="primary" :loading="saving" :disabled="loading" data-testid="document-submit" @click="save">{{
        submitText
      }}</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.tip {
  margin-bottom: 12px;
}

.lines {
  border-top: 1px solid var(--el-border-color-lighter);
}

.line {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px 12px;
  padding: 8px 0;
  border-bottom: 1px solid var(--el-border-color-lighter);
}

.line-main {
  flex: 1;
  min-width: 160px;
}

.line-qty {
  display: flex;
  align-items: center;
  gap: 6px;
}

.empty {
  padding: 12px 0;
}

.qty {
  width: 120px;
}

.unit {
  min-width: 42px;
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.minus {
  color: var(--el-color-danger);
  font-weight: 600;
}

.add {
  margin: 10px 0;
}

.picker {
  width: min(360px, 100%);
}

.note {
  margin-top: 4px;
}

.muted {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
</style>
