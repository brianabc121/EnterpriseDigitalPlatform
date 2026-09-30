<script setup lang="ts">
import { errorMessage } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { computed, reactive, ref, watch } from 'vue'

import { api } from '../../api'
import { STOCK_MODES, stockAfter, stockDetail, type StockMode } from '../../inventory'
import type { Product } from '../../orders'
import { precisionOf, qty } from '../../warehouse'

/**
 * 调整库存（设计文档 §25.12）：入库、出库、盘点，或者不再管理这个商品的库存（材料总是管理库存）。
 * 每次调整都写库存记录（变化前后的数量、原因、操作人）并记审计。成品的数量是整数，材料最多三位
 * 小数（§25.13）。
 */
type Adjustable = Pick<
  Product,
  'id' | 'name' | 'stock' | 'stock_reserved' | 'stock_available' | 'stock_low' | 'kind' | 'unit'
>
const open = defineModel<boolean>({ required: true })
const props = defineProps<{ product: Adjustable | null }>()
const emit = defineEmits<{ saved: [product: Product] }>()

const saving = ref(false)
const form = reactive({ mode: 'add' as StockMode, quantity: undefined as number | undefined, note: '' })

const tracked = computed(() => props.product?.stock !== null && props.product?.stock !== undefined)
const material = computed(() => props.product?.kind === 'material')
const modes = computed(() =>
  STOCK_MODES.filter(
    ([mode]) =>
      (tracked.value || mode === 'add' || mode === 'set') && !(material.value && mode === 'untrack'),
  ),
)
const after = computed(() => {
  const p = props.product
  if (!p || form.mode === 'untrack') return null
  if (form.quantity === undefined || form.quantity === null) return undefined
  return stockAfter(p.stock, form.mode, form.quantity)
})

watch(open, (value) => {
  if (!value) return
  Object.assign(form, { mode: tracked.value ? 'add' : 'set', quantity: undefined, note: '' })
})

async function save(): Promise<void> {
  const p = props.product
  if (!p) return
  if (form.mode !== 'untrack' && (form.quantity === undefined || form.quantity === null)) {
    ElMessage.warning('请填写数量')
    return
  }
  if (form.mode === 'remove' && after.value === null) {
    ElMessage.warning(`出库数量不能超过现有库存 ${qty(p.stock ?? 0)}`)
    return
  }
  saving.value = true
  const { data, error } = await api.POST('/api/v1/products/{product_id}/stock', {
    params: { path: { product_id: p.id } },
    body: {
      mode: form.mode,
      quantity: form.mode === 'untrack' ? null : (form.quantity ?? null),
      note: form.note.trim(),
    },
  })
  saving.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  ElMessage.success(data.stock === null ? '已不再管理库存' : `现有库存 ${qty(data.stock)} ${data.unit}`.trim())
  open.value = false
  emit('saved', data)
}
</script>

<template>
  <el-dialog
    v-model="open"
    :title="product ? `调整库存：${product.name}` : '调整库存'"
    width="min(480px, 92vw)"
    append-to-body
    data-testid="stock-dialog"
  >
    <template v-if="product">
      <p class="current" data-testid="stock-current">
        <template v-if="tracked">
          可用 <b>{{ qty(product.stock_available) }}</b> {{ product.unit }}
          <span class="muted">（{{ stockDetail(product, product.kind) }}）</span>
        </template>
        <span v-else class="muted">这个商品还没有管理库存：入库或盘点后开始管理。</span>
      </p>
      <el-form label-position="top">
        <el-form-item label="方式">
          <el-radio-group v-model="form.mode" data-testid="stock-mode">
            <el-radio-button v-for="[mode, label] in modes" :key="mode" :value="mode">{{ label }}</el-radio-button>
          </el-radio-group>
          <div class="muted hint">{{ STOCK_MODES.find(([m]) => m === form.mode)?.[2] }}</div>
        </el-form-item>
        <el-form-item v-if="form.mode !== 'untrack'" :label="form.mode === 'set' ? '实际数量' : '数量'">
          <el-input-number
            v-model="form.quantity"
            :min="0"
            :max="100000000"
            :precision="precisionOf(product.kind)"
            class="full"
            data-testid="stock-quantity"
          />
          <div v-if="after !== undefined" class="muted hint" data-testid="stock-after">
            <template v-if="after === null">出库数量不能超过现有库存 {{ qty(product.stock ?? 0) }}</template>
            <template v-else>调整后现有库存 {{ qty(after) }} {{ product.unit }}</template>
          </div>
        </el-form-item>
        <el-form-item label="原因">
          <el-input
            v-model="form.note"
            maxlength="200"
            placeholder="例如：到货批次、损耗、月底盘点"
            data-testid="stock-note"
          />
        </el-form-item>
      </el-form>
    </template>
    <template #footer>
      <el-button @click="open = false">取消</el-button>
      <el-button type="primary" :loading="saving" data-testid="stock-save" @click="save">确定</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.current {
  margin: 0 0 12px;
}

.hint {
  width: 100%;
  margin-top: 4px;
  line-height: 1.4;
}

.muted {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.full {
  width: 100%;
}
</style>
