<script setup lang="ts">
import { computed } from 'vue'

import {
  amountText,
  dueText,
  stageDaysText,
  type OpportunitySummary,
  type Stage,
} from '../../opportunities'

/**
 * 看板上的一张卡片（设计文档 §40.8）：客户（公司或称呼）、商机名称、预计金额、预计成交日、负责人、下次跟进
 * （逾期标红）、在当前阶段的天数（超过停滞天数标"停滞"），AI 建议的标"待确认"。能拖的时候拖到别的列换阶段；
 * 卡片菜单里的"移到…"给手机和不方便拖的时候用。
 */
const props = defineProps<{
  item: OpportunitySummary
  today: string
  amountVisible: boolean
  stages: Stage[]
  canMove: boolean
  canDrag: boolean
}>()
const emit = defineEmits<{
  open: [id: string]
  move: [item: OpportunitySummary, stage: Stage]
  dragstart: [item: OpportunitySummary, event: DragEvent]
}>()

const targets = computed(() => props.stages.filter((s) => s.id !== props.item.stage_id))
const due = computed(() => dueText(props.item.next_follow_at, props.today))

function onCommand(stageId: string): void {
  const stage = props.stages.find((s) => s.id === stageId)
  if (stage) emit('move', props.item, stage)
}
</script>

<template>
  <div
    class="card"
    :class="{ stale: item.stale, suggested: item.status === 'suggested', draggable: canMove && canDrag }"
    :draggable="canMove && canDrag"
    :data-testid="`opp-card-${item.id}`"
    @click="emit('open', item.id)"
    @dragstart="(event: DragEvent) => emit('dragstart', item, event)"
  >
    <div class="top">
      <span class="customer">{{ item.customer_company || item.customer_name }}</span>
      <el-dropdown v-if="canMove && targets.length" trigger="click" @command="onCommand">
        <button type="button" class="more" :data-testid="`opp-card-menu-${item.id}`" @click.stop>
          移到…
        </button>
        <template #dropdown>
          <el-dropdown-menu>
            <el-dropdown-item v-for="stage in targets" :key="stage.id" :command="stage.id">
              {{ stage.kind === 'open' ? stage.name : `${stage.name}…` }}
            </el-dropdown-item>
          </el-dropdown-menu>
        </template>
      </el-dropdown>
    </div>
    <div class="name">{{ item.name }}</div>
    <div v-if="item.customer_company" class="muted">{{ item.customer_name }}</div>
    <div v-if="(amountVisible && item.amount) || item.expected_close_at" class="facts">
      <span v-if="amountVisible && item.amount" class="amount">{{ amountText(item.amount) }}</span>
      <span v-if="item.expected_close_at" class="muted">{{ item.expected_close_at }} 成交</span>
    </div>
    <div class="foot">
      <span class="muted">{{ item.owner_name ?? '没有负责人' }}</span>
      <span
        v-if="due"
        :class="['due', { overdue: item.overdue, today: item.due_today }]"
        :data-testid="`opp-card-due-${item.id}`"
      >
        {{ due }}
      </span>
    </div>
    <div class="tags">
      <el-tag v-if="item.status === 'suggested'" size="small" type="warning">待确认</el-tag>
      <el-tag v-if="item.stale" size="small" type="danger" :data-testid="`opp-card-stale-${item.id}`">
        停滞
      </el-tag>
      <span class="muted days">{{ stageDaysText(item.days_in_stage) }}</span>
    </div>
  </div>
</template>

<style scoped>
.card {
  padding: 10px 12px;
  background: var(--el-bg-color);
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px;
  cursor: pointer;
  font-size: 13px;
}

.card.draggable {
  cursor: grab;
}

.card:hover {
  border-color: var(--el-color-primary);
}

.card.stale {
  border-left: 3px solid var(--el-color-danger);
}

.card.suggested {
  border-left: 3px solid var(--el-color-warning);
}

.top {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.customer {
  font-weight: 600;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.more {
  flex-shrink: 0;
  padding: 0 4px;
  font: inherit;
  font-size: 12px;
  color: var(--el-color-primary);
  background: none;
  border: none;
  cursor: pointer;
}

.name {
  margin-top: 2px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.facts,
.foot,
.tags {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-top: 6px;
}

.tags {
  justify-content: flex-start;
}

.days {
  margin-left: auto;
}

.amount {
  font-weight: 600;
}

.due.today {
  color: var(--el-color-warning);
}

.due.overdue {
  color: var(--el-color-danger);
}

.muted {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
</style>
