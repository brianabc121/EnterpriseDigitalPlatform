<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { computed, onMounted, ref } from 'vue'

import { EVAL_SAMPLE, parseEvalCases } from '../../ai'
import { api, formatDateTime } from '../../api'
import { HANDOFF_REASON } from '../../labels'
import { percent } from '../../reports'

/**
 * 评测：用一组样例问题检验 AI——回答正确率（期望回答的问题，回复包含全部关键词）与转人工正确率。
 * 调整知识或设置后重跑，对比两次的结果。
 */
const text = ref(EVAL_SAMPLE)
const running = ref(false)
const runs = ref<Schemas['EvalRunOut'][]>([])
const viewing = ref<Schemas['EvalRunOut'] | null>(null)

const parsed = computed(() => parseEvalCases(text.value))

async function load(): Promise<void> {
  const { data } = await api.GET('/api/v1/ai/evaluations', { params: { query: { limit: 20 } } })
  if (data) runs.value = data.items
}

async function run(): Promise<void> {
  const { cases, errors } = parsed.value
  if (errors.length) {
    ElMessage.warning(errors[0]!)
    return
  }
  if (!cases.length) {
    ElMessage.warning('请至少写一个问题')
    return
  }
  if (cases.length > 100) {
    ElMessage.warning('一次最多评测 100 个问题')
    return
  }
  running.value = true
  const { data, error } = await api.POST('/api/v1/ai/evaluations', { body: { cases } })
  running.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  runs.value = [data, ...runs.value]
  viewing.value = data
}

function expectation(r: Schemas['EvalCaseResult']): string {
  return r.expect_handoff ? '转人工' : '回答'
}

function actual(r: Schemas['EvalCaseResult']): string {
  if (r.action === 'handoff') return `转人工（${HANDOFF_REASON[r.reason ?? ''] ?? r.reason ?? ''}）`
  return r.reply ?? ''
}

onMounted(load)
</script>

<template>
  <div class="evaluation" data-testid="ai-eval">
    <div class="editor">
      <el-input
        v-model="text"
        type="textarea"
        :rows="8"
        placeholder="每行一个问题；竖线后写期望：转人工，或回复里应包含的关键词"
        data-testid="ai-eval-cases"
      />
      <div class="bar">
        <span class="muted">
          {{ parsed.cases.length }} 个问题
          <template v-if="parsed.errors.length">，{{ parsed.errors[0] }}</template>
        </span>
        <el-button type="primary" :loading="running" data-testid="ai-eval-run" @click="run">
          开始评测
        </el-button>
      </div>
    </div>

    <el-table :data="runs" size="small" empty-text="还没有评测记录" data-testid="ai-eval-runs">
      <el-table-column label="时间" width="180">
        <template #default="{ row }">{{ formatDateTime(row.created_at) }}</template>
      </el-table-column>
      <el-table-column prop="cases" label="问题数" width="90" align="right" />
      <el-table-column label="回答正确率" width="120" align="right">
        <template #default="{ row }">{{ percent(row.answer_accuracy) }}</template>
      </el-table-column>
      <el-table-column label="转人工正确率" width="120" align="right">
        <template #default="{ row }">{{ percent(row.handoff_accuracy) }}</template>
      </el-table-column>
      <el-table-column label="">
        <template #default="{ row }">
          <el-button link type="primary" size="small" @click="viewing = row">查看明细</el-button>
        </template>
      </el-table-column>
    </el-table>

    <el-drawer
      :model-value="viewing !== null"
      title="评测明细"
      size="720px"
      @update:model-value="(v: boolean) => !v && (viewing = null)"
    >
      <template v-if="viewing">
        <p class="summary">
          回答正确率 <b>{{ percent(viewing.answer_accuracy) }}</b
          >， 转人工正确率
          <b>{{ percent(viewing.handoff_accuracy) }}</b>
        </p>
        <el-table :data="viewing.results" size="small" data-testid="ai-eval-results">
          <el-table-column prop="question" label="问题" min-width="160" />
          <el-table-column label="期望" width="70">
            <template #default="{ row }">{{ expectation(row) }}</template>
          </el-table-column>
          <el-table-column label="AI 的结果" min-width="240">
            <template #default="{ row }">{{ actual(row) }}</template>
          </el-table-column>
          <el-table-column label="判定" width="80" align="center">
            <template #default="{ row }">
              <el-tag
                v-if="row.handoff_correct && row.answer_correct !== false"
                type="success"
                size="small"
                >正确</el-tag
              >
              <el-tag v-else type="danger" size="small">错误</el-tag>
            </template>
          </el-table-column>
        </el-table>
      </template>
    </el-drawer>
  </div>
</template>

<style scoped>
.evaluation {
  max-width: 900px;
}

.editor {
  margin-bottom: 16px;
}

.bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-top: 8px;
}

.muted {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.summary {
  margin: 0 0 12px;
}
</style>
