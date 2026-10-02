<script setup lang="ts">
import { errorMessage } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { api } from '../api'
import BreakdownTab from '../components/profit/BreakdownTab.vue'
import EntriesTab from '../components/profit/EntriesTab.vue'
import StatementTab from '../components/profit/StatementTab.vue'
import { downloadBlob } from '../download'
import {
  monthPeriod,
  periodText,
  presetPeriod,
  PRESETS,
  type PresetKey,
  type ProfitPeriod,
} from '../profit'
import { useAuthStore } from '../stores/auth'

/**
 * 盈利报表（设计文档 §30）：默认只有管理员看得到。选一个期间，看利润表（本期、上期、去年同期）和
 * 每月趋势、毛利分析，登记费用和其他收入，导出 Excel。链接可以带 tab=breakdown|entries。
 */
type Tab = 'statement' | 'breakdown' | 'entries'
const TABS: Tab[] = ['statement', 'breakdown', 'entries']

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()

const initial = route.query.tab
const tab = ref<Tab>(TABS.includes(initial as Tab) ? (initial as Tab) : 'statement')
const preset = ref<PresetKey>('month')
const months = ref<[string, string] | null>(null)
const version = ref(0)
const exporting = ref(false)
const canManage = computed(() => auth.can('profit:manage'))

const period = computed<ProfitPeriod>(() => {
  const today = new Date()
  if (preset.value === 'custom' && months.value) {
    return monthPeriod(months.value[0], months.value[1], today)
  }
  return presetPeriod(preset.value === 'custom' ? 'month' : preset.value, today)
})

function choose(key: PresetKey): void {
  preset.value = key
  months.value = null
}

function pickMonths(value: [string, string] | null): void {
  months.value = value
  preset.value = value ? 'custom' : 'month'
}

/** 本月以后的月份不能选。 */
function futureMonth(day: Date): boolean {
  const now = new Date()
  return day > new Date(now.getFullYear(), now.getMonth() + 1, 0)
}

async function exportXlsx(): Promise<void> {
  exporting.value = true
  const { start, end, shift } = period.value
  const { data, error } = await api.POST('/api/v1/profit/export', {
    body: { start, end, shift },
    parseAs: 'blob',
  })
  exporting.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  downloadBlob(data, `profit-${start.replaceAll('-', '')}-${end.replaceAll('-', '')}.xlsx`)
}

watch(tab, (value) => {
  void router.replace({ query: { ...route.query, tab: value === 'statement' ? undefined : value } })
})
</script>

<template>
  <div class="profit">
    <div class="page-header">
      <h2>盈利报表</h2>
      <el-button :loading="exporting" data-testid="profit-export" @click="exportXlsx">导出 Excel</el-button>
    </div>

    <div class="filters" data-testid="profit-filters">
      <el-radio-group :model-value="preset" size="small" data-testid="profit-presets">
        <el-radio-button
          v-for="[key, label] in PRESETS"
          :key="key"
          :value="key"
          :data-testid="`profit-preset-${key}`"
          @click="choose(key)"
        >
          {{ label }}
        </el-radio-button>
      </el-radio-group>
      <div class="months">
        <el-date-picker
          :model-value="months"
          type="monthrange"
          value-format="YYYY-MM"
          size="small"
          range-separator="至"
          start-placeholder="开始月份"
          end-placeholder="结束月份"
          :disabled-date="futureMonth"
          data-testid="profit-months-picker"
          @update:model-value="pickMonths"
        />
      </div>
      <span class="range" data-testid="profit-range">{{ periodText(period) }}</span>
    </div>

    <el-tabs v-model="tab" data-testid="profit-tabs">
      <el-tab-pane label="利润表" name="statement">
        <StatementTab :period="period" :version="version" />
      </el-tab-pane>
      <el-tab-pane label="毛利分析" name="breakdown" lazy>
        <BreakdownTab :period="period" :version="version" />
      </el-tab-pane>
      <el-tab-pane label="收支登记" name="entries" lazy>
        <EntriesTab :period="period" :version="version" :can-manage="canManage" @changed="version++" />
      </el-tab-pane>
    </el-tabs>
  </div>
</template>

<style scoped>
.filters {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 12px;
  margin-bottom: 8px;
}

/* 月份选择框：桌面 260px；手机上占满一行（不超出屏幕）。 */
.months :deep(.el-date-editor) {
  --el-date-editor-width: 260px;
  width: 260px;
}

.range {
  white-space: nowrap;
}

@media (max-width: 600px) {
  .months {
    width: 100%;
  }

  .months :deep(.el-date-editor) {
    --el-date-editor-width: 100%;
    width: 100%;
    box-sizing: border-box;
  }
}

.range {
  font-size: 13px;
  color: var(--el-text-color-secondary);
}
</style>
