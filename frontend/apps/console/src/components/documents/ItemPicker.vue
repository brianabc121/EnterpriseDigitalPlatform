<script setup lang="ts">
import { computed, ref, watch } from 'vue'

import { api } from '../../api'
import { fromProduct, fromStockItem, roundQuantity, type PickedItem } from '../../documents'
import { money } from '../../orders'
import { qty } from '../../warehouse'

/**
 * 批量选择（设计文档 §25.14，参考进销存软件的"选择商品"）：左边是分类，上面是搜索，可以只看有
 * 库存的；勾选多个、填好数量后一次加入单据。换个关键字搜索时已经勾选的保留。
 */
const open = defineModel<boolean>({ required: true })
const props = defineProps<{ source: 'sales' | 'warehouse'; kind: 'goods' | 'material' }>()
const emit = defineEmits<{ pick: [items: PickedItem[]] }>()

const categories = ref<string[]>([])
const category = ref('')
const q = ref('')
const inStock = ref(false)
const rows = ref<PickedItem[]>([])
const selected = ref<Record<string, PickedItem>>({})
const loading = ref(false)
let timer: ReturnType<typeof setTimeout> | undefined

const noun = computed(() => (props.source === 'warehouse' && props.kind === 'material' ? '材料' : '商品'))
const shown = computed(() =>
  inStock.value ? rows.value.filter((r) => r.available === null || r.available > 0) : rows.value,
)
const count = computed(() => Object.keys(selected.value).length)

watch(open, (value) => {
  if (!value) return
  selected.value = {}
  q.value = ''
  category.value = ''
  inStock.value = false
  void loadCategories()
  void load()
})

async function loadCategories(): Promise<void> {
  const { data } =
    props.source === 'sales'
      ? await api.GET('/api/v1/products/categories', { params: { query: { kind: 'goods' } } })
      : await api.GET('/api/v1/warehouse/categories', { params: { query: { kind: props.kind } } })
  categories.value = data?.items ?? []
}

async function load(): Promise<void> {
  loading.value = true
  const term = q.value.trim() || undefined
  const cat = category.value || undefined
  if (props.source === 'sales') {
    const { data } = await api.GET('/api/v1/products', {
      params: { query: { kind: 'goods', status: 'on', q: term, category: cat, limit: 100 } },
    })
    rows.value = (data?.items ?? []).map((p) => fromProduct(p, 1))
  } else {
    const { data } = await api.GET('/api/v1/warehouse/items', {
      params: { query: { kind: props.kind, status: 'on', q: term, category: cat, limit: 100 } },
    })
    rows.value = (data?.items ?? []).map((i) => fromStockItem(i, 1))
  }
  // 已经勾选的显示勾选时填的数量。
  rows.value = rows.value.map((r) => selected.value[r.id] ?? r)
  loading.value = false
}

function onSearch(): void {
  clearTimeout(timer)
  timer = setTimeout(() => void load(), 250)
}

function pickCategory(value: string): void {
  category.value = value
  void load()
}

function toggle(row: PickedItem, checked: boolean): void {
  const next = { ...selected.value }
  if (checked) next[row.id] = { ...row, quantity: row.quantity > 0 ? row.quantity : 1 }
  else delete next[row.id]
  selected.value = next
}

function setQuantity(row: PickedItem, value: number | undefined): void {
  row.quantity = roundQuantity(value ?? 0, row.kind)
  // 填了数量就算勾选，清成 0 就取消。
  toggle(row, row.quantity > 0)
}

function confirm(): void {
  const items = Object.values(selected.value).filter((i) => i.quantity > 0)
  if (items.length) emit('pick', items)
  open.value = false
}
</script>

<template>
  <el-dialog
    v-model="open"
    :title="`批量选择${noun}`"
    width="min(960px, 96vw)"
    append-to-body
    class="item-picker"
    data-testid="item-picker"
  >
    <div class="picker">
      <aside class="cats">
        <button type="button" :class="{ on: category === '' }" @click="pickCategory('')">全部</button>
        <button
          v-for="c in categories"
          :key="c"
          type="button"
          :class="{ on: category === c }"
          data-testid="item-picker-category"
          @click="pickCategory(c)"
        >
          {{ c }}
        </button>
      </aside>
      <section class="list">
        <div class="bar">
          <el-select
            v-if="categories.length"
            :model-value="category"
            class="cat-select"
            placeholder="全部分类"
            @update:model-value="pickCategory"
          >
            <el-option label="全部分类" value="" />
            <el-option v-for="c in categories" :key="c" :label="c" :value="c" />
          </el-select>
          <el-input
            v-model="q"
            clearable
            :placeholder="`搜索${noun}：名称、代码、型号`"
            data-testid="item-picker-search"
            @input="onSearch"
            @clear="load"
          />
          <el-checkbox v-model="inStock" data-testid="item-picker-in-stock">只看有库存的</el-checkbox>
        </div>
        <el-table
          v-loading="loading"
          :data="shown"
          size="small"
          height="min(52vh, 440px)"
          :empty-text="`没有${noun}`"
          data-testid="item-picker-table"
        >
          <el-table-column width="44">
            <template #default="{ row }">
              <el-checkbox
                :model-value="row.id in selected"
                data-testid="item-picker-check"
                :data-name="row.name"
                @update:model-value="(v: boolean | string | number) => toggle(row, Boolean(v))"
              />
            </template>
          </el-table-column>
          <el-table-column :label="noun" min-width="200">
            <template #default="{ row }">
              <div class="name">{{ row.name }}</div>
              <div class="sub">{{ [row.code, row.spec].filter(Boolean).join(' · ') }}</div>
            </template>
          </el-table-column>
          <el-table-column label="单位" width="64" prop="unit" />
          <el-table-column label="可用库存" width="96" align="right">
            <template #default="{ row }">
              <span :class="{ short: row.available !== null && row.available <= 0 }">{{
                row.available === null ? '不管理' : qty(row.available)
              }}</span>
            </template>
          </el-table-column>
          <el-table-column v-if="source === 'sales'" label="建议零售价" width="110" align="right">
            <template #default="{ row }">{{ money(row.price) }}</template>
          </el-table-column>
          <el-table-column label="数量" width="140">
            <template #default="{ row }">
              <el-input-number
                :model-value="row.quantity"
                :min="0"
                :max="100000000"
                :precision="row.kind === 'goods' ? 0 : undefined"
                size="small"
                controls-position="right"
                class="qty"
                data-testid="item-picker-quantity"
                :data-name="row.name"
                @update:model-value="(v: number | undefined) => setQuantity(row, v)"
              />
            </template>
          </el-table-column>
        </el-table>
      </section>
    </div>
    <template #footer>
      <span class="count" data-testid="item-picker-count">已选 {{ count }} 项</span>
      <el-button @click="open = false">取消</el-button>
      <el-button type="primary" :disabled="!count" data-testid="item-picker-confirm" @click="confirm"
        >加入单据</el-button
      >
    </template>
  </el-dialog>
</template>

<style scoped>
.picker {
  display: flex;
  gap: 12px;
  min-height: 320px;
}

.cats {
  display: flex;
  flex-direction: column;
  gap: 2px;
  width: 150px;
  max-height: min(58vh, 500px);
  overflow: auto;
  border-right: 1px solid var(--el-border-color-lighter);
  padding-right: 8px;
}

.cats button {
  padding: 6px 8px;
  border: none;
  border-radius: 4px;
  background: none;
  color: var(--el-text-color-regular);
  font-size: 13px;
  text-align: left;
  cursor: pointer;
}

.cats button.on {
  background: var(--el-color-primary-light-9);
  color: var(--el-color-primary);
  font-weight: 600;
}

.list {
  flex: 1;
  min-width: 0;
}

.bar {
  display: flex;
  flex-wrap: wrap;
  gap: 8px 12px;
  align-items: center;
  margin-bottom: 8px;
}

.bar .el-input {
  flex: 1;
  min-width: 180px;
}

.cat-select {
  display: none;
  width: 140px;
}

.name {
  font-weight: 500;
}

.sub {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.short {
  color: var(--el-color-danger);
}

.qty {
  width: 118px;
}

.count {
  margin-right: 12px;
  color: var(--el-text-color-secondary);
  font-size: 13px;
}

@media (max-width: 640px) {
  .cats {
    display: none;
  }

  .cat-select {
    display: block;
  }
}
</style>
