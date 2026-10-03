<script setup lang="ts">
import { errorMessage } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { computed, onMounted, ref, watch } from 'vue'

import { api } from '../../api'
import {
  amountShort,
  columnOpen,
  dropAction,
  matchesView,
  type BoardColumn,
  type BoardFilters,
  type OpportunityBoard,
  type OpportunitySummary,
  type Stage,
} from '../../opportunities'
import { useAuthStore } from '../../stores/auth'
import LostDialog from './LostDialog.vue'
import OpportunityCard from './OpportunityCard.vue'
import WonDialog from './WonDialog.vue'

/**
 * 商机看板（设计文档 §40.8）：一列一个进行中的阶段，最右的"赢单""输单"默认折叠只显示数量；列头显示数量和
 * 预计金额合计。拖到别的列就换阶段；拖到赢单 / 输单弹出确认（关联订单 / 选输单原因）。快捷视图在页面上筛。
 */
const props = defineProps<{ filters: BoardFilters; today: string; refreshKey: number }>()
const emit = defineEmits<{ open: [id: string]; changed: []; loaded: [board: OpportunityBoard] }>()

const auth = useAuthStore()
const board = ref<OpportunityBoard | null>(null)
const loading = ref(false)
const expanded = ref(new Set<string>())
const dragging = ref<OpportunitySummary | null>(null)
const over = ref<string | null>(null)
const closing = ref<{ item: OpportunitySummary; kind: 'won' | 'lost' } | null>(null)
const wonOpen = ref(false)
const lostOpen = ref(false)

const canManage = computed(() => auth.can('opportunity:manage'))
const stages = computed<Stage[]>(() => board.value?.columns.map((c) => c.stage) ?? [])
const columns = computed(() => board.value?.columns ?? [])

function visible(column: BoardColumn): OpportunitySummary[] {
  return column.items.filter((item) => matchesView(item, props.filters.view, props.today, auth.me?.id))
}

async function load(): Promise<void> {
  loading.value = true
  const { data, error } = await api.GET('/api/v1/opportunities/board', {
    params: {
      query: {
        level: props.filters.level || undefined,
        source: props.filters.source || undefined,
        owner_id: props.filters.owner || undefined,
        q: props.filters.q.trim() || undefined,
        mine: props.filters.view === 'mine' || undefined,
      },
    },
  })
  loading.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  board.value = data
  // 看已赢单 / 已输单的视图时把那一列展开。
  for (const column of data.columns) {
    if (column.stage.kind === props.filters.view) expanded.value.add(column.stage.id)
  }
  emit('loaded', data)
}

function toggle(column: BoardColumn): void {
  if (expanded.value.has(column.stage.id)) expanded.value.delete(column.stage.id)
  else expanded.value.add(column.stage.id)
}

function onDragStart(item: OpportunitySummary, event: DragEvent): void {
  dragging.value = item
  event.dataTransfer?.setData('text/plain', item.id)
  if (event.dataTransfer) event.dataTransfer.effectAllowed = 'move'
}

function onDragOver(column: BoardColumn, event: DragEvent): void {
  if (!dragging.value) return
  event.preventDefault()
  if (event.dataTransfer) event.dataTransfer.dropEffect = 'move'
  over.value = column.stage.id
}

function onDragLeave(column: BoardColumn): void {
  if (over.value === column.stage.id) over.value = null
}

async function onDrop(column: BoardColumn): Promise<void> {
  const item = dragging.value
  dragging.value = null
  over.value = null
  if (item) await moveTo(item, column.stage)
}

/** 换阶段：进行中的列直接换；赢单 / 输单先确认。 */
async function moveTo(item: OpportunitySummary, stage: Stage): Promise<void> {
  const action = dropAction(item, stage)
  if (action === 'none') return
  if (action === 'won' || action === 'lost') {
    closing.value = { item, kind: action }
    if (action === 'won') wonOpen.value = true
    else lostOpen.value = true
    return
  }
  const { data, error } = await api.POST('/api/v1/opportunities/{opportunity_id}/stage', {
    params: { path: { opportunity_id: item.id } },
    body: { stage_id: stage.id },
  })
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  ElMessage.success(`已移到「${stage.name}」`)
  changed()
}

function changed(): void {
  emit('changed')
  void load()
}

watch(
  () => props.refreshKey,
  () => void load(),
)
onMounted(load)
</script>

<template>
  <div v-loading="loading" class="board" data-testid="opp-board">
    <div
      v-for="column in columns"
      :key="column.stage.id"
      class="column"
      :class="[column.stage.kind, { collapsed: !columnOpen(column, expanded), over: over === column.stage.id }]"
      :data-testid="`opp-column-${column.stage.code}`"
      @dragover="onDragOver(column, $event)"
      @dragleave="onDragLeave(column)"
      @drop.prevent="onDrop(column)"
    >
      <div class="head" :style="{ borderTopColor: column.stage.color ?? 'transparent' }">
        <span class="title">{{ column.stage.name }}</span>
        <span class="count" :data-testid="`opp-column-count-${column.stage.code}`">{{ column.total }}</span>
        <span v-if="board?.amount_visible && column.amount_sum && columnOpen(column, expanded)" class="sum">
          {{ amountShort(column.amount_sum) }}
        </span>
        <button
          v-if="column.stage.kind !== 'open'"
          type="button"
          class="toggle"
          :data-testid="`opp-column-toggle-${column.stage.code}`"
          @click="toggle(column)"
        >
          {{ columnOpen(column, expanded) ? '收起' : '展开' }}
        </button>
      </div>
      <div v-if="columnOpen(column, expanded)" class="cards">
        <OpportunityCard
          v-for="item in visible(column)"
          :key="item.id"
          :item="item"
          :today="today"
          :amount-visible="board?.amount_visible ?? false"
          :stages="stages"
          :can-move="canManage && item.status === 'active'"
          @open="(id) => emit('open', id)"
          @move="moveTo"
          @dragstart="onDragStart"
        />
        <div v-if="visible(column).length === 0" class="empty">没有商机</div>
        <div v-if="column.truncated" class="empty">还有更多，用列表查看</div>
      </div>
      <div v-else class="hint">{{ column.total }} 条</div>
    </div>
  </div>
  <WonDialog
    v-model="wonOpen"
    :opportunity="closing?.kind === 'won' ? closing.item : null"
    @done="changed"
  />
  <LostDialog
    v-model="lostOpen"
    :opportunity="closing?.kind === 'lost' ? closing.item : null"
    @done="changed"
  />
</template>

<style scoped>
.board {
  display: flex;
  gap: 12px;
  align-items: flex-start;
  min-height: 320px;
  padding-bottom: 8px;
  overflow-x: auto;
}

.column {
  display: flex;
  flex: 0 0 236px;
  flex-direction: column;
  max-height: calc(100vh - 300px);
  min-height: 160px;
  background: var(--el-fill-color-light);
  border: 2px solid transparent;
  border-radius: 8px;
}

.column.collapsed {
  flex-basis: 150px;
  min-height: 0;
}

.column.over {
  border-color: var(--el-color-primary);
}

.head {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 8px 10px;
  border-top: 3px solid transparent;
  border-radius: 6px 6px 0 0;
  font-size: 13px;
}

.title,
.toggle {
  white-space: nowrap;
}

.title {
  font-weight: 600;
}

.count {
  padding: 0 6px;
  font-size: 12px;
  background: var(--el-fill-color-darker);
  border-radius: 10px;
}

.sum {
  margin-left: auto;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.toggle {
  margin-left: auto;
  padding: 0;
  font: inherit;
  font-size: 12px;
  color: var(--el-color-primary);
  background: none;
  border: none;
  cursor: pointer;
}

.sum + .toggle {
  margin-left: 4px;
}

.cards {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 0 8px 8px;
  overflow-y: auto;
}

.empty,
.hint {
  padding: 8px 10px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
  text-align: center;
}
</style>
