<script setup lang="ts">
import { errorMessage } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'

import { api, formatDateTime } from '../../api'
import { kbSummary, llmUsage, type KbAlignment } from '../../wake'

/**
 * 制度对齐（设计文档 §33.7）：AI 定期对照现行的规章制度整理知识库——和制度冲突的、制度里有而知识库
 * 里没有的、重复的知识，修改建议进审核台（来源"制度对齐"），由人确认后才生效；另外统计长期没用到、
 * 快到期、没有负责人、评价差的知识。只核对有变化的知识（增量更新索引，§33.9）。
 */
type Filter = 'stale' | 'expiring' | 'no_owner' | 'disliked' | 'policy'
const emit = defineEmits<{ review: []; filter: [name: Filter] }>()

const POLL_MS = 3000
const data = ref<KbAlignment | null>(null)
const loading = ref(false)
const starting = ref(false)
let poll: ReturnType<typeof setInterval> | undefined

const report = computed(() => (data.value?.report ?? {}) as Record<string, number | boolean>)
const numbers = computed(() => {
  const r = report.value
  const n = (key: string): number => (typeof r[key] === 'number' ? (r[key] as number) : 0)
  return [
    ['现行制度', n('policies'), '份'],
    ['问答', n('items'), '条'],
    ['没有变化、跳过', n('unchanged'), '条'],
    ['交给大模型核对', n('checked'), '条'],
    ['和制度冲突', n('conflict'), '条'],
    ['建议新增问答', n('gap'), '条'],
    ['重复', n('duplicate'), '组'],
  ] as [string, number, string][]
})
const housekeeping = computed(() => {
  const r = report.value
  const n = (key: string): number => (typeof r[key] === 'number' ? (r[key] as number) : 0)
  return [
    ['stale', '长期没用到', n('stale'), '90 天没有被 AI 或坐席引用'],
    ['expiring', '快到期', n('expiring'), '7 天内到期，到期后自动下线'],
    ['no_owner', '没有负责人', n('no_owner'), '到期前没人收到提醒'],
    ['disliked', '评价差', n('disliked'), '员工点踩多于点赞且至少 3 次'],
  ] as [Filter, string, number, string][]
})

async function load(): Promise<void> {
  loading.value = true
  const { data: result, error } = await api.GET('/api/v1/kb/alignment')
  loading.value = false
  if (!result) {
    ElMessage.error(errorMessage(error))
    return
  }
  data.value = result
  if (!result.running) clearInterval(poll)
}

async function run(): Promise<void> {
  starting.value = true
  const { data: queued, error } = await api.POST('/api/v1/kb/alignment/run')
  starting.value = false
  if (!queued) {
    ElMessage.error(errorMessage(error))
    return
  }
  ElMessage.success('AI 正在整理知识库，几秒后刷新结果')
  await load()
  clearInterval(poll)
  poll = setInterval(load, POLL_MS)
}

onMounted(async () => {
  await load()
  if (data.value?.running) poll = setInterval(load, POLL_MS)
})
onBeforeUnmount(() => clearInterval(poll))
</script>

<template>
  <div v-loading="loading" class="alignment" data-testid="kb-alignment">
    <div class="head">
      <div>
        <div class="title">
          最近一次整理：{{ data?.finished_at ? formatDateTime(data.finished_at) : '还没有整理过' }}
          <el-tag v-if="data?.running" size="small" type="primary" data-testid="kb-alignment-running"
            >整理中</el-tag
          >
        </div>
        <div class="muted" data-testid="kb-alignment-summary">
          {{ data?.finished_at ? kbSummary(data.report) : '标为"规章制度"的知识发布后，AI 会对照它们整理知识库。' }}
          <template v-if="data && llmUsage(data.report)"> · {{ llmUsage(data.report) }}</template>
        </div>
      </div>
      <div class="actions">
        <el-button link type="primary" data-testid="kb-alignment-policies" @click="emit('filter', 'policy')">
          查看规章制度（{{ data?.policies ?? 0 }}）
        </el-button>
        <el-button type="primary" :loading="starting" data-testid="kb-alignment-run" @click="run">
          立即整理
        </el-button>
      </div>
    </div>

    <el-alert
      v-if="data && !data.policies"
      type="info"
      :closable="false"
      show-icon
      class="block"
      title="还没有现行的规章制度：上传制度文件时勾选“规章制度”，或者编辑知识时勾选。没有制度时只找重复的知识。"
    />

    <div class="pending">
      <span>
        待处理的修改建议 <strong data-testid="kb-alignment-pending">{{ data?.pending ?? 0 }}</strong> 条
      </span>
      <el-button size="small" data-testid="kb-alignment-review" @click="emit('review')">去审核台</el-button>
      <span class="muted">AI 不直接改知识：建议都要人在审核台确认，通过后才生效、可以回滚。</span>
    </div>

    <h4>本次核对</h4>
    <div class="numbers">
      <div v-for="[label, value, unit] in numbers" :key="label" class="number">
        <strong>{{ value }}</strong>
        <span>{{ label }}（{{ unit }}）</span>
      </div>
    </div>

    <h4>整理清单</h4>
    <div class="numbers">
      <button
        v-for="[key, label, value, hint] in housekeeping"
        :key="key"
        type="button"
        class="number link"
        :data-testid="`kb-alignment-${key}`"
        :title="hint"
        @click="emit('filter', key)"
      >
        <strong :class="{ warn: value > 0 }">{{ value }}</strong>
        <span>{{ label }}</span>
      </button>
    </div>
  </div>
</template>

<style scoped>
.head {
  display: flex;
  flex-wrap: wrap;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 12px;
}

.title {
  display: flex;
  align-items: center;
  gap: 8px;
  font-weight: 500;
}

.actions {
  display: flex;
  align-items: center;
  gap: 8px;
}

.muted {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.block {
  margin-bottom: 12px;
}

.pending {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px 12px;
  padding: 10px 14px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 8px;
  background: var(--el-bg-color);
}

h4 {
  margin: 16px 0 8px;
  font-size: 14px;
}

.numbers {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(130px, 1fr));
  gap: 8px;
}

.number {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 10px 12px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 8px;
  background: var(--el-bg-color);
  font-size: 12px;
  color: var(--el-text-color-secondary);
  text-align: left;
}

.number strong {
  font-size: 20px;
  color: var(--el-text-color-primary);
}

.number strong.warn {
  color: var(--el-color-warning);
}

.number.link {
  cursor: pointer;
  font: inherit;
  font-size: 12px;
}

.number.link:hover {
  border-color: var(--el-color-primary);
}
</style>
