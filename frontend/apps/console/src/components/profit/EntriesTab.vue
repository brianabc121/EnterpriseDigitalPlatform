<script setup lang="ts">
import { errorMessage } from '@edp/api-client'
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, onMounted, ref, watch } from 'vue'

import { api, formatDateTime } from '../../api'
import {
  isoDate,
  KIND_LABEL,
  yuan,
  type CategoryOptions,
  type EntryKind,
  type EntryPage,
  type ProfitEntry,
  type ProfitPeriod,
} from '../../profit'
import EntryDialog, { type EntryDraft } from './EntryDialog.vue'

/**
 * 盈利报表的"收支登记"页签（设计文档 §30.4）：期间内的费用和其他收入、合计和按类别的小计；登记、
 * 修改、复制、删除；上个月"每月固定"的收支还没登记到本月时，可以一键登记。
 */
const props = defineProps<{ period: ProfitPeriod; version: number; canManage: boolean }>()
const emit = defineEmits<{ changed: [] }>()

const PAGE_SIZE = 50
const page = ref(1)
const kind = ref<EntryKind | ''>('')
const category = ref('')
const result = ref<EntryPage | null>(null)
const loading = ref(false)
const copying = ref(false)
const categories = ref<CategoryOptions | null>(null)
const dialog = ref(false)
const editing = ref<ProfitEntry | null>(null)
const draft = ref<EntryDraft | null>(null)

const categoryChoices = computed(() => {
  const names = new Set<string>()
  for (const c of result.value?.by_category ?? []) {
    if (!kind.value || c.kind === kind.value) names.add(c.category)
  }
  return [...names]
})

async function load(): Promise<void> {
  loading.value = true
  const { data, error } = await api.GET('/api/v1/profit/entries', {
    params: {
      query: {
        start: props.period.start,
        end: props.period.end,
        kind: kind.value || undefined,
        category: category.value || undefined,
        limit: PAGE_SIZE,
        offset: (page.value - 1) * PAGE_SIZE,
      },
    },
  })
  loading.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  result.value = data
}

async function loadCategories(): Promise<void> {
  const { data } = await api.GET('/api/v1/profit/categories')
  if (data) categories.value = data
}

function add(value: EntryKind): void {
  editing.value = null
  draft.value = { kind: value }
  dialog.value = true
}

function edit(entry: ProfitEntry): void {
  editing.value = entry
  draft.value = null
  dialog.value = true
}

/** 复制：按原内容新登记一笔，日期默认今天。 */
function copy(entry: ProfitEntry): void {
  editing.value = null
  draft.value = {
    kind: entry.kind,
    category: entry.category,
    amount: entry.amount,
    note: entry.note,
    recurring: entry.recurring,
    occurred_on: isoDate(new Date()),
  }
  dialog.value = true
}

async function remove(entry: ProfitEntry): Promise<void> {
  try {
    await ElMessageBox.confirm(
      `删除 ${entry.occurred_on} 的${KIND_LABEL[entry.kind]}「${entry.category}」${yuan(entry.amount)}？`,
      '删除收支',
      { confirmButtonText: '删除', cancelButtonText: '取消', type: 'warning' },
    )
  } catch {
    return
  }
  const { error, response } = await api.DELETE('/api/v1/profit/entries/{profit_entry_id}', {
    params: { path: { profit_entry_id: entry.id } },
  })
  if (!response.ok) {
    ElMessage.error(errorMessage(error))
    return
  }
  ElMessage.success('已删除')
  changed()
}

async function copyRecurring(): Promise<void> {
  const month = result.value?.recurring?.month
  if (!month) return
  copying.value = true
  const { data, error } = await api.POST('/api/v1/profit/entries/copy-recurring', {
    body: { month },
  })
  copying.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  ElMessage.success(`已登记 ${data.created} 笔每月固定的收支`)
  changed()
}

/** 收支变了：页面把 version 加一，利润表和这里都会重新加载。 */
function changed(): void {
  void loadCategories()
  emit('changed')
}

function search(): void {
  page.value = 1
  void load()
}

watch(kind, () => {
  category.value = ''
  search()
})
watch(category, search)
watch(() => [props.period.start, props.period.end, props.version], search)
onMounted(() => {
  void load()
  void loadCategories()
})
</script>

<template>
  <div>
    <el-alert
      v-if="result?.recurring"
      type="info"
      :closable="false"
      show-icon
      class="recurring"
      data-testid="entries-recurring"
    >
      <template #title>
        上个月有 {{ result.recurring.count }} 笔每月固定的收支（{{ result.recurring.categories.join('、') }}，支出
        {{ yuan(result.recurring.expense) }}<template v-if="Number(result.recurring.income) > 0">，收入
          {{ yuan(result.recurring.income) }}</template>）还没有登记到 {{ result.recurring.month }}
      </template>
      <el-button
        v-if="canManage"
        type="primary"
        size="small"
        :loading="copying"
        data-testid="entries-copy-recurring"
        @click="copyRecurring"
      >
        一键登记
      </el-button>
    </el-alert>

    <div class="toolbar">
      <div v-if="result" class="totals" data-testid="entries-totals">
        <span>支出合计 <strong>{{ yuan(result.expense_total) }}</strong></span>
        <span>收入合计 <strong>{{ yuan(result.income_total) }}</strong></span>
      </div>
      <span class="spacer" />
      <el-select v-model="kind" clearable placeholder="全部类型" size="small" class="filter" data-testid="entries-kind">
        <el-option label="支出" value="expense" />
        <el-option label="收入" value="income" />
      </el-select>
      <el-select
        v-model="category"
        clearable
        filterable
        placeholder="全部类别"
        size="small"
        class="filter"
        data-testid="entries-category"
      >
        <el-option v-for="c in categoryChoices" :key="c" :label="c" :value="c" />
      </el-select>
      <template v-if="canManage">
        <el-button type="primary" size="small" data-testid="entry-add-expense" @click="add('expense')">
          登记支出
        </el-button>
        <el-button size="small" data-testid="entry-add-income" @click="add('income')">登记收入</el-button>
      </template>
    </div>

    <div v-if="result && result.by_category.length" class="chips" data-testid="entries-categories">
      <el-tag
        v-for="c in result.by_category"
        :key="`${c.kind}:${c.category}`"
        :type="c.kind === 'income' ? 'success' : 'info'"
        effect="plain"
      >
        {{ KIND_LABEL[c.kind] }} · {{ c.category }} {{ yuan(c.amount) }}
      </el-tag>
    </div>

    <el-table
      v-loading="loading"
      :data="result?.items ?? []"
      row-key="id"
      data-testid="entries-table"
      empty-text="这段时间还没有登记费用和其他收入"
    >
      <el-table-column label="日期" width="110" prop="occurred_on" />
      <el-table-column label="类型" width="80">
        <template #default="{ row }">
          <el-tag size="small" :type="(row as ProfitEntry).kind === 'income' ? 'success' : 'info'">
            {{ (row as ProfitEntry).kind_label }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="类别" min-width="110" prop="category" />
      <el-table-column label="金额" min-width="110" align="right">
        <template #default="{ row }">
          <span data-testid="entry-amount-cell">{{ yuan((row as ProfitEntry).amount) }}</span>
        </template>
      </el-table-column>
      <el-table-column label="备注" min-width="140">
        <template #default="{ row }">
          <span class="note">{{ (row as ProfitEntry).note }}</span>
          <el-tag v-if="(row as ProfitEntry).recurring" size="small" effect="plain" class="tag">每月固定</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="登记人" width="140">
        <template #default="{ row }">
          <div>{{ (row as ProfitEntry).created_by_name || '—' }}</div>
          <small class="muted">{{ formatDateTime((row as ProfitEntry).created_at) }}</small>
        </template>
      </el-table-column>
      <el-table-column v-if="canManage" label="操作" width="160" fixed="right">
        <template #default="{ row }">
          <el-button link type="primary" data-testid="entry-edit" @click="edit(row as ProfitEntry)">修改</el-button>
          <el-button link type="primary" data-testid="entry-copy" @click="copy(row as ProfitEntry)">复制</el-button>
          <el-button link type="danger" data-testid="entry-delete" @click="remove(row as ProfitEntry)">删除</el-button>
        </template>
      </el-table-column>
    </el-table>
    <el-pagination
      v-if="result && result.total > PAGE_SIZE"
      v-model:current-page="page"
      :page-size="PAGE_SIZE"
      :total="result.total"
      layout="total, prev, pager, next"
      class="pager"
      @current-change="load"
    />
    <EntryDialog v-model="dialog" :entry="editing" :draft="draft" :categories="categories" @saved="changed" />
  </div>
</template>

<style scoped>
.recurring {
  margin-bottom: 12px;
}

.recurring :deep(.el-alert__content) {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}

.toolbar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  margin-bottom: 10px;
}

.totals {
  display: flex;
  gap: 16px;
  font-size: 13px;
  color: var(--el-text-color-secondary);
}

.totals strong {
  color: var(--el-text-color-primary);
}

.spacer {
  flex: 1;
}

.filter {
  width: 130px;
}

.chips {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 10px;
}

.note {
  margin-right: 6px;
}

.tag {
  vertical-align: middle;
}

.muted {
  color: var(--el-text-color-secondary);
}

.pager {
  margin-top: 12px;
  justify-content: flex-end;
}
</style>
