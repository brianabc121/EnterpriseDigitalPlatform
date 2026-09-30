<script setup lang="ts">
import { Search } from '@element-plus/icons-vue'
import type { ElInput } from 'element-plus'
import { ref } from 'vue'

import { api } from '../../api'
import { exactCode, fromProduct, fromStockItem, type PickedItem } from '../../documents'
import { money } from '../../orders'
import { qty } from '../../warehouse'
import ItemPicker from './ItemPicker.vue'

/**
 * 明细表最后的录入行（设计文档 §25.14）：输入名称、代码、型号或俗称，下拉列出候选，↑↓ 选择、
 * 回车加入；还没出候选就回车（例如扫码枪扫出代码后回车）时先检索，代码完全一致的直接加入。
 * 旁边的"批量选择"一次勾选多个。
 *
 * source：sales 是可以销售的成品（下单，带建议零售价）；warehouse 是仓库里的材料或成品（开单）。
 */
const props = withDefaults(
  defineProps<{
    source: 'sales' | 'warehouse'
    kind?: 'goods' | 'material'
    placeholder?: string
    testid?: string
  }>(),
  { kind: 'goods', placeholder: '输入名称、代码或型号，回车加入', testid: 'item-entry' },
)
const emit = defineEmits<{ add: [items: PickedItem[]] }>()

const text = ref('')
const options = ref<PickedItem[]>([])
const active = ref(0)
const open = ref(false)
const loading = ref(false)
const picking = ref(false)
const input = ref<InstanceType<typeof ElInput> | null>(null)
let seq = 0
let timer: ReturnType<typeof setTimeout> | undefined

async function fetchItems(q: string): Promise<PickedItem[]> {
  if (props.source === 'sales') {
    const { data } = await api.GET('/api/v1/products/search', { params: { query: { q, limit: 8 } } })
    return (data?.items ?? []).map((c) => fromProduct(c.product))
  }
  const { data } = await api.GET('/api/v1/warehouse/items', {
    params: { query: { kind: props.kind, q, status: 'on', limit: 8 } },
  })
  return (data?.items ?? []).map((i) => fromStockItem(i))
}

async function search(): Promise<PickedItem[]> {
  const q = text.value.trim()
  const mine = ++seq
  if (!q) {
    options.value = []
    open.value = false
    return []
  }
  loading.value = true
  open.value = true
  const found = await fetchItems(q)
  if (mine !== seq) return found
  loading.value = false
  options.value = found
  active.value = 0
  return found
}

function onInput(): void {
  clearTimeout(timer)
  timer = setTimeout(() => void search(), 200)
}

function choose(item: PickedItem): void {
  clearTimeout(timer)
  seq++
  emit('add', [{ ...item, quantity: 1 }])
  text.value = ''
  options.value = []
  open.value = false
  loading.value = false
}

async function onEnter(): Promise<void> {
  clearTimeout(timer)
  const q = text.value.trim()
  if (!q) return
  if (open.value && !loading.value && options.value.length) {
    choose(options.value[active.value] ?? options.value[0]!)
    return
  }
  const found = await search()
  const exact = exactCode(found, q) ?? (found.length === 1 ? found[0]! : null)
  if (exact) choose(exact)
}

function move(step: number): void {
  if (!options.value.length) return
  open.value = true
  active.value = (active.value + step + options.value.length) % options.value.length
}

function picked(items: PickedItem[]): void {
  emit('add', items)
}

function focus(): void {
  input.value?.focus()
}

defineExpose({ focus })
</script>

<template>
  <div class="item-entry" :data-testid="testid">
    <div class="box">
      <el-input
        ref="input"
        v-model="text"
        :placeholder="placeholder"
        clearable
        :data-testid="`${testid}-input`"
        @input="onInput"
        @keydown.enter.prevent="onEnter"
        @keydown.down.prevent="move(1)"
        @keydown.up.prevent="move(-1)"
        @keydown.esc="open = false"
        @blur="open = false"
      >
        <template #prefix>
          <el-icon><Search /></el-icon>
        </template>
      </el-input>
      <ul v-if="open" class="options" role="listbox" :data-testid="`${testid}-options`">
        <li v-if="loading" class="hint">正在查找…</li>
        <li v-else-if="!options.length" class="hint">没有找到，换个名称或代码试试</li>
        <template v-else>
          <li
            v-for="(o, i) in options"
            :key="o.id"
            role="option"
            :aria-selected="i === active"
            :class="{ active: i === active }"
            :data-testid="`${testid}-option`"
            :data-name="o.name"
            @mousedown.prevent="choose(o)"
            @mouseenter="active = i"
          >
            <span class="name">{{ o.name }}</span>
            <span class="sub">{{ [o.code, o.spec].filter(Boolean).join(' · ') }}</span>
            <span class="right">
              <template v-if="o.available !== null">可用 {{ qty(o.available) }} {{ o.unit }}</template>
              <template v-else-if="o.unit">{{ o.unit }}</template>
              <template v-if="o.price !== null"> · {{ money(o.price) }}</template>
            </span>
          </li>
        </template>
      </ul>
    </div>
    <el-button :data-testid="`${testid}-batch`" @click="picking = true">批量选择</el-button>
    <ItemPicker v-model="picking" :source="source" :kind="kind" @pick="picked" />
  </div>
</template>

<style scoped>
.item-entry {
  display: flex;
  gap: 8px;
  align-items: center;
}

.box {
  position: relative;
  flex: 1;
  min-width: 0;
}

.options {
  position: absolute;
  z-index: 20;
  top: calc(100% + 4px);
  left: 0;
  right: 0;
  max-height: 280px;
  margin: 0;
  padding: 4px 0;
  overflow: auto;
  list-style: none;
  background: var(--el-bg-color-overlay);
  border: 1px solid var(--el-border-color-light);
  border-radius: 4px;
  box-shadow: var(--el-box-shadow-light);
}

.options li {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 2px 8px;
  padding: 6px 12px;
  cursor: pointer;
  font-size: 13px;
}

.options li.active {
  background: var(--el-color-primary-light-9);
}

.options .hint {
  color: var(--el-text-color-secondary);
  cursor: default;
}

.name {
  font-weight: 500;
}

.sub,
.right {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.right {
  margin-left: auto;
}
</style>
