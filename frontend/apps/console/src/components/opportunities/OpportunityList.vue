<script setup lang="ts">
import { errorMessage } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { computed, onMounted, ref, watch } from 'vue'

import { api, formatDateTime } from '../../api'
import {
  amountText,
  dueText,
  LEVEL_LABEL,
  LEVEL_TAG,
  SOURCE_LABEL,
  STATUS_LABEL,
  STATUS_TAG,
  type ListFilters,
  type OpportunityPage,
  type OpportunitySummary,
} from '../../opportunities'

/**
 * 商机列表（设计文档 §40.8）：客户、名称、阶段、等级、预计金额、预计成交日、负责人、来源、最近动态、下次跟进；
 * 筛选条件由页面传入，分页；点一行打开详情。
 */
const PAGE_SIZE = 20
const props = defineProps<{ filters: ListFilters; today: string; refreshKey: number }>()
const emit = defineEmits<{ open: [id: string]; loaded: [page: OpportunityPage] }>()

const page = ref<OpportunityPage | null>(null)
const loading = ref(false)
const current = ref(1)
const items = computed<OpportunitySummary[]>(() => page.value?.items ?? [])
const showDue = computed(() => !['won', 'lost', 'suggested'].includes(props.filters.view))

async function load(): Promise<void> {
  const f = props.filters
  loading.value = true
  const { data, error } = await api.GET('/api/v1/opportunities', {
    params: {
      query: {
        view: f.view,
        stage_id: f.stage || undefined,
        level: f.level || undefined,
        source: f.source || undefined,
        owner_id: f.owner || undefined,
        amount_min: f.amountMin || undefined,
        amount_max: f.amountMax || undefined,
        close_month: f.closeMonth || undefined,
        stale: f.stale || undefined,
        q: f.q.trim() || undefined,
        limit: PAGE_SIZE,
        offset: (current.value - 1) * PAGE_SIZE,
      },
    },
  })
  loading.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  page.value = data
  emit('loaded', data)
}

/** 阶段的颜色点：列表里没有阶段的颜色，按类型取主题色。 */
function stageColor(row: OpportunitySummary): string {
  if (row.stage_kind === 'won') return 'var(--el-color-success)'
  if (row.stage_kind === 'lost') return 'var(--el-color-info)'
  return 'var(--el-color-primary)'
}

function dueClass(row: OpportunitySummary): string {
  if (row.overdue) return 'overdue'
  return row.due_today ? 'today' : ''
}

watch(
  () => props.refreshKey,
  () => {
    current.value = 1
    void load()
  },
)
onMounted(load)
</script>

<template>
  <div>
    <el-table
      v-loading="loading"
      :data="items"
      data-testid="opp-table"
      empty-text="没有商机"
      row-class-name="row"
      @row-click="(row: OpportunitySummary) => emit('open', row.id)"
    >
      <el-table-column label="客户" min-width="150">
        <template #default="{ row }">
          <div class="name">{{ row.customer_name }}</div>
          <div v-if="row.customer_company" class="muted">{{ row.customer_company }}</div>
        </template>
      </el-table-column>
      <el-table-column label="商机" min-width="170" show-overflow-tooltip>
        <template #default="{ row }">
          {{ row.name }}
          <el-tag v-if="row.status === 'suggested'" size="small" type="warning" class="inline-tag">
            待确认
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="阶段" width="120">
        <template #default="{ row }">
          <span class="stage" :data-testid="`opp-row-stage-${row.id}`">
            <span class="dot" :style="{ background: stageColor(row) }" />
            {{ row.stage_name }}
          </span>
          <el-tag v-if="row.stale" size="small" type="danger" class="inline-tag">停滞</el-tag>
          <div v-if="row.status === 'won' || row.status === 'lost'" class="muted">
            <el-tag :type="STATUS_TAG[row.status as OpportunitySummary['status']]" size="small" effect="plain">
              {{ STATUS_LABEL[row.status as OpportunitySummary['status']] }}
            </el-tag>
          </div>
        </template>
      </el-table-column>
      <el-table-column label="等级" width="64">
        <template #default="{ row }">
          <el-tag :type="LEVEL_TAG[row.level as OpportunitySummary['level']]" size="small">
            {{ LEVEL_LABEL[row.level as OpportunitySummary['level']] }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column v-if="page?.amount_visible" label="预计金额" width="110" align="right">
        <template #default="{ row }">{{ amountText(row.amount) || '—' }}</template>
      </el-table-column>
      <el-table-column label="预计成交" width="110">
        <template #default="{ row }">{{ row.expected_close_at ?? '—' }}</template>
      </el-table-column>
      <el-table-column label="负责人" width="90">
        <template #default="{ row }">{{ row.owner_name ?? '—' }}</template>
      </el-table-column>
      <el-table-column label="来源" width="80">
        <template #default="{ row }">{{ SOURCE_LABEL[row.source as OpportunitySummary['source']] }}</template>
      </el-table-column>
      <el-table-column label="最近动态" width="150">
        <template #default="{ row }">
          {{ row.last_activity_at ? formatDateTime(row.last_activity_at) : '—' }}
        </template>
      </el-table-column>
      <el-table-column v-if="showDue" label="下次跟进" width="120">
        <template #default="{ row }">
          <div v-if="row.next_follow_at" :class="['due', dueClass(row)]" :data-testid="`opp-due-${row.id}`">
            <div>{{ dueText(row.next_follow_at, today) }}</div>
            <div class="muted">{{ row.next_follow_at }}</div>
          </div>
          <span v-else class="muted">—</span>
        </template>
      </el-table-column>
      <el-table-column v-else label="结果" width="150">
        <template #default="{ row }">
          <div v-if="row.order_no" class="muted">订单 {{ row.order_no }}</div>
          <div v-if="row.contract_no" class="muted">合同 {{ row.contract_no }}</div>
          <div v-if="row.lost_reason_name" class="muted">{{ row.lost_reason_name }}</div>
          <div v-if="row.closed_at" class="muted">{{ formatDateTime(row.closed_at) }}</div>
        </template>
      </el-table-column>
    </el-table>
    <div class="page-footer">
      <el-pagination
        v-model:current-page="current"
        layout="total, prev, pager, next"
        :page-size="PAGE_SIZE"
        :total="page?.total ?? 0"
        @current-change="load"
      />
    </div>
  </div>
</template>

<style scoped>
.name {
  font-weight: 500;
}

.muted {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.inline-tag {
  margin-left: 4px;
}

.stage {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}

.dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
}

.due.today {
  color: var(--el-color-warning);
}

.due.overdue {
  color: var(--el-color-danger);
}

:deep(.row) {
  cursor: pointer;
}
</style>
