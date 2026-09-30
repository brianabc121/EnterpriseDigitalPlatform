<script setup lang="ts">
import { ref } from 'vue'

import { api } from '../../api'
import { money, type Product } from '../../orders'

/**
 * 选择商品：按名称、代码、型号、规格或俗称检索商品库（与 AI 使用的检索相同），只列出上架的商品。
 * 选中后把商品交给上层（带建议零售价）。
 */
const props = defineProps<{ placeholder?: string; testid?: string }>()
const emit = defineEmits<{ pick: [product: Product] }>()

const options = ref<Product[]>([])
const loading = ref(false)
const value = ref('')

async function search(q: string): Promise<void> {
  if (!q.trim()) {
    options.value = []
    return
  }
  loading.value = true
  const { data } = await api.GET('/api/v1/products/search', {
    params: { query: { q: q.trim(), limit: 10 } },
  })
  loading.value = false
  options.value = (data?.items ?? []).map((c) => c.product)
}

function pick(id: string): void {
  const product = options.value.find((p) => p.id === id)
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
    :placeholder="props.placeholder ?? '搜索商品：名称、代码、型号'"
    class="picker"
    :data-testid="props.testid ?? 'product-picker'"
    @change="pick"
  >
    <el-option v-for="p in options" :key="p.id" :label="label(p)" :value="p.id">
      <span class="name">{{ label(p) }}</span>
      <span v-if="p.code" class="code">{{ p.code }}</span>
      <span class="price">{{ money(p.retail_price) }}</span>
    </el-option>
  </el-select>
</template>

<style scoped>
.picker {
  width: 100%;
}

.name {
  margin-right: 8px;
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
</style>
