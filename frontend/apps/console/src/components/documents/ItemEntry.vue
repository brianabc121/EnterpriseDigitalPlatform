<script setup lang="ts">
import { Search } from '@element-plus/icons-vue'
import type { ElInput } from 'element-plus'
import { computed, ref } from 'vue'

import { api } from '../../api'
import {
  enterPick,
  fromProductSuggestion,
  fromStockSuggestion,
  highlight,
  matchLabel,
  stockText,
  suggestionTitle,
  type PickedItem,
  type Suggestion,
} from '../../documents'
import { money } from '../../orders'
import ItemPicker from './ItemPicker.vue'

/**
 * 明细表最后的录入行（设计文档 §25.14、§25.16）：从输入第一个字开始联想，在名称、俗称、分类、代码、
 * 型号、规格里找，认拼音首字母和全拼、不同写法（"win01"、"1.2*1.5"）和相近的写法；下拉标出按什么
 * 找到的，↑↓ 选择、回车加入，Esc 收起（再点一下重新列出）。没有输入时点一下或按 ↓ 列出自己最近开单
 * 用过的。还没出候选就回车（例如扫码枪扫出代码后回车）时先检索，代码完全一致的直接加入。旁边的
 * "批量选择"一次勾选多个。
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
  {
    kind: 'goods',
    placeholder: '输入名称、代码、规格或拼音首字母，回车加入',
    testid: 'item-entry',
  },
)
const emit = defineEmits<{ add: [items: PickedItem[]] }>()

const text = ref('')
const options = ref<Suggestion[]>([])
/** 当前候选对应的输入（标出命中的部分）；为空时是最近用过的。 */
const query = ref('')
const recent = ref(false)
const active = ref(0)
const open = ref(false)
const loading = ref(false)
const picking = ref(false)
const input = ref<InstanceType<typeof ElInput> | null>(null)
let seq = 0
let timer: ReturnType<typeof setTimeout> | undefined

const title = computed(() => suggestionTitle(options.value, recent.value))

async function fetchSuggestions(q: string): Promise<{ list: Suggestion[]; recent: boolean }> {
  if (props.source === 'sales') {
    const { data } = await api.GET('/api/v1/products/suggest', {
      params: { query: { q, limit: 8 } },
    })
    return { list: (data?.items ?? []).map(fromProductSuggestion), recent: data?.recent ?? false }
  }
  const { data } = await api.GET('/api/v1/warehouse/suggest', {
    params: { query: { kind: props.kind, q, limit: 8 } },
  })
  return { list: (data?.items ?? []).map(fromStockSuggestion), recent: data?.recent ?? false }
}

/** 按输入联想；showRecent 时没有输入也查（最近用过的）。 */
async function search(showRecent = false): Promise<Suggestion[]> {
  const q = text.value.trim()
  const mine = ++seq
  if (!q && !showRecent) {
    options.value = []
    open.value = false
    return []
  }
  loading.value = true
  open.value = true
  const found = await fetchSuggestions(q)
  if (mine !== seq) return found.list
  loading.value = false
  options.value = found.list
  recent.value = found.recent
  query.value = q
  active.value = 0
  // 没有最近用过的商品时不弹出空的下拉。
  if (!q && !found.list.length) open.value = false
  return found.list
}

function onInput(): void {
  clearTimeout(timer)
  timer = setTimeout(() => void search(), 150)
}

function showRecent(): void {
  if (!text.value.trim() && !open.value) void search(true)
}

/** 点一下录入行：空着时列出最近用过的；有输入而下拉收起了（例如按过 Esc）时重新列出候选。 */
function onClick(): void {
  if (!open.value) void search(!text.value.trim())
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
  if (open.value && !loading.value && options.value.length) {
    choose((options.value[active.value] ?? options.value[0]!).item)
    return
  }
  if (!q) return
  const found = await search()
  const picked = enterPick(found, q)
  if (picked) choose(picked)
}

function move(step: number): void {
  if (!open.value && !text.value.trim()) {
    showRecent()
    return
  }
  if (!options.value.length) return
  open.value = true
  active.value = (active.value + step + options.value.length) % options.value.length
}

function close(): void {
  open.value = false
}

/** Esc：下拉开着时只收起下拉（不关掉整张单据）。 */
function onEsc(event: KeyboardEvent): void {
  if (!open.value) return
  event.stopPropagation()
  close()
}

function picked(items: PickedItem[]): void {
  emit('add', items)
}

function focus(): void {
  input.value?.focus()
}

function sub(item: PickedItem): string {
  return [item.code, item.category, item.spec].filter(Boolean).join(' · ')
}

/** 可用库存没有了时标红。 */
function short(item: PickedItem): boolean {
  return item.available !== null && item.available <= 0
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
        @click="onClick"
        @keydown.enter.prevent="onEnter"
        @keydown.down.prevent="move(1)"
        @keydown.up.prevent="move(-1)"
        @keydown.esc="onEsc"
        @blur="close"
      >
        <template #prefix>
          <el-icon><Search /></el-icon>
        </template>
      </el-input>
      <ul
        v-if="open"
        class="options"
        role="listbox"
        :data-testid="`${testid}-options`"
        :data-query="loading ? null : query"
      >
        <li v-if="loading" class="hint">正在查找…</li>
        <li v-else-if="!options.length" class="hint">没有找到，换个名称、代码或拼音首字母试试</li>
        <template v-else>
          <li v-if="title" class="title" role="presentation" :data-testid="`${testid}-title`">{{ title }}</li>
          <li
            v-for="(o, i) in options"
            :key="o.item.id"
            role="option"
            :aria-selected="i === active"
            :class="{ active: i === active }"
            :data-testid="`${testid}-option`"
            :data-name="o.item.name"
            :data-code="o.item.code ?? ''"
            :data-match="o.match"
            @mousedown.prevent="choose(o.item)"
            @mouseenter="active = i"
          >
            <div class="line">
              <span class="name">
                <template v-for="(part, k) in highlight(o.item.name, query)" :key="k">
                  <mark v-if="part.hit">{{ part.text }}</mark>
                  <template v-else>{{ part.text }}</template>
                </template>
              </span>
              <span
                v-if="matchLabel(o)"
                class="tag"
                :class="{ similar: o.match === 'similar' }"
                :data-testid="`${testid}-tag`"
                >{{ matchLabel(o) }}</span
              >
              <span class="right">
                <template v-if="o.item.price !== null">{{ money(o.item.price) }}</template>
              </span>
            </div>
            <div class="line sub">
              <span>
                <template v-for="(part, k) in highlight(sub(o.item), query)" :key="k">
                  <mark v-if="part.hit">{{ part.text }}</mark>
                  <template v-else>{{ part.text }}</template>
                </template>
              </span>
              <span class="right stock" :class="{ short: short(o.item) }">{{ stockText(o.item, source) }}</span>
            </div>
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
  max-height: 340px;
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

.options .title {
  padding: 4px 12px 2px;
  color: var(--el-text-color-secondary);
  font-size: 12px;
  cursor: default;
}

.line {
  display: flex;
  align-items: baseline;
  gap: 8px;
  min-width: 0;
}

.line > span:first-child {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.name {
  font-weight: 500;
}

mark {
  padding: 0;
  color: var(--el-color-primary);
  background: none;
  font-weight: 600;
}

.tag {
  flex: none;
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

.sub {
  margin-top: 2px;
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.right {
  flex: none;
  margin-left: auto;
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

.stock.short {
  color: var(--el-color-danger);
}
</style>
