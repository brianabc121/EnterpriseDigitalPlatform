<script setup lang="ts">
import type { Schemas } from '@edp/api-client'
import { computed, ref } from 'vue'

import { api } from '../../api'
import { fromProductSuggestion, matchLabel, suggestionTitle } from '../../documents'
import { money, type Product } from '../../orders'

/**
 * 选择商品（设计文档 §25.16）：联想上架的成品——名称、俗称、分类、代码、型号、规格，拼音首字母和
 * 相近的写法都认，标出按什么找到的。打开时先按 seed（订单里客户的说法）列出相近的商品，也可以再输入。
 * 选中后把商品交给上层（带建议零售价）。
 */
const props = defineProps<{ placeholder?: string; testid?: string; seed?: string | null }>()
const emit = defineEmits<{ pick: [product: Product] }>()

const options = ref<Schemas['ProductSuggestion'][]>([])
const recent = ref(false)
const loading = ref(false)
const value = ref('')
let seq = 0

const title = computed(() => suggestionTitle(options.value.map(fromProductSuggestion), recent.value))

async function search(q: string): Promise<void> {
  const mine = ++seq
  loading.value = true
  const { data } = await api.GET('/api/v1/products/suggest', {
    params: { query: { q: q.trim().slice(0, 100), limit: 10 } },
  })
  if (mine !== seq) return
  loading.value = false
  options.value = data?.items ?? []
  recent.value = data?.recent ?? false
}

/** 打开时按客户的说法联想（没有说法时是最近用过的）。 */
function onVisible(visible: boolean): void {
  if (visible) void search(props.seed ?? '')
}

function pick(id: string): void {
  const product = options.value.find((s) => s.product.id === id)?.product
  value.value = ''
  if (product) emit('pick', product)
}

function label(p: Product): string {
  return [p.name, p.model, p.spec].filter(Boolean).join(' ')
}
</script>

<template>
  <el-select
    v-model="value"
    filterable
    remote
    :remote-method="search"
    :loading="loading"
    :placeholder="props.placeholder ?? '搜索商品：名称、代码、规格或拼音首字母'"
    class="picker"
    :data-testid="props.testid ?? 'product-picker'"
    @visible-change="onVisible"
    @change="pick"
  >
    <template v-if="title" #header>
      <span class="title" :data-testid="`${props.testid ?? 'product-picker'}-title`">{{ title }}</span>
    </template>
    <el-option v-for="s in options" :key="s.product.id" :label="label(s.product)" :value="s.product.id">
      <span class="name">{{ label(s.product) }}</span>
      <span v-if="matchLabel(s)" class="tag" :class="{ similar: s.match === 'similar' }">{{ matchLabel(s) }}</span>
      <span v-if="s.product.code" class="code">{{ s.product.code }}</span>
      <span class="price">{{ money(s.product.retail_price) }}</span>
      <span
        v-if="s.product.stock_available !== null"
        class="stock"
        :class="{ short: s.product.stock_available <= 0 }"
        >可用 {{ s.product.stock_available }}</span
      >
    </el-option>
  </el-select>
</template>

<style scoped>
.picker {
  width: 100%;
}

.title {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.name {
  margin-right: 8px;
}

.tag {
  margin-right: 8px;
  padding: 0 4px;
  border: 1px solid var(--el-color-primary-light-5);
  border-radius: 3px;
  color: var(--el-color-primary);
  font-size: 11px;
  line-height: 16px;
}

.tag.similar {
  border-color: var(--el-color-warning-light-5);
  color: var(--el-color-warning-dark-2);
}

.code {
  color: var(--el-text-color-secondary);
  font-size: 12px;
  margin-right: 8px;
}

.price {
  float: right;
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.stock {
  float: right;
  margin-right: 12px;
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.stock.short {
  color: var(--el-color-danger);
}
</style>
