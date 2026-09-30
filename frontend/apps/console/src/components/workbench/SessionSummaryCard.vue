<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { reactive, ref, watch } from 'vue'

import { api } from '../../api'

/**
 * 会话小结（人工接待结束后）：大模型写的小结和标签，坐席修改后确认写入客户档案——
 * 标签并入客户标签，小结记到客户备注的最前面。
 */
const props = defineProps<{ sessionId: string; canWrite: boolean }>()

const summary = ref<Schemas['SessionSummaryOut'] | null>(null)
const loading = ref(false)
const acting = ref(false)
const form = reactive({ text: '', tags: [] as string[] })

function fill(data: Schemas['SessionSummaryOut'] | null): void {
  summary.value = data
  form.text = data?.summary ?? ''
  form.tags = [...(data?.tags ?? [])]
}

async function load(): Promise<void> {
  loading.value = true
  const { data } = await api.GET('/api/v1/sessions/{session_id}/summary', {
    params: { path: { session_id: props.sessionId } },
  })
  loading.value = false
  fill(data ?? null)
}

async function run(
  request: () => Promise<{ data?: Schemas['SessionSummaryOut']; error?: unknown }>,
  done?: string,
): Promise<void> {
  acting.value = true
  const { data, error } = await request()
  acting.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  fill(data)
  if (done) ElMessage.success(done)
}

const path = () => ({ params: { path: { session_id: props.sessionId } } })

function generate(): Promise<void> {
  return run(() => api.POST('/api/v1/sessions/{session_id}/summary', path()))
}

function confirm(): Promise<void> {
  if (!form.text.trim()) {
    ElMessage.warning('请填写小结')
    return Promise.resolve()
  }
  return run(
    () =>
      api.POST('/api/v1/sessions/{session_id}/summary/confirm', {
        ...path(),
        body: { summary: form.text.trim(), tags: form.tags },
      }),
    '已写入客户档案',
  )
}

function discard(): Promise<void> {
  return run(() => api.POST('/api/v1/sessions/{session_id}/summary/discard', path()))
}

watch(() => props.sessionId, load, { immediate: true })
</script>

<template>
  <div v-loading="loading" class="card" data-testid="session-summary">
    <div class="head">
      <span class="title">会话小结</span>
      <el-tag v-if="summary?.status === 'confirmed'" size="small" type="success">已写入客户档案</el-tag>
      <el-tag v-else-if="summary?.status === 'draft'" size="small" type="warning">待确认</el-tag>
    </div>
    <template v-if="!summary || summary.status === 'discarded'">
      <p class="muted">
        {{ summary ? '已忽略这份小结。' : '人工接待结束后自动生成小结，确认后写入客户档案。' }}
      </p>
      <el-button
        v-if="canWrite"
        size="small"
        :loading="acting"
        data-testid="summary-generate"
        @click="generate"
      >
        {{ summary ? '重新生成' : '现在生成' }}
      </el-button>
    </template>
    <template v-else-if="summary.status === 'draft'">
      <el-input
        v-model="form.text"
        type="textarea"
        :rows="3"
        maxlength="500"
        :disabled="!canWrite"
        data-testid="summary-text"
      />
      <el-input-tag
        v-model="form.tags"
        :max="5"
        :disabled="!canWrite"
        placeholder="标签，回车添加"
        class="tags"
        data-testid="summary-tags"
      />
      <div v-if="canWrite" class="actions">
        <el-button size="small" :disabled="acting" @click="discard">不需要</el-button>
        <el-button size="small" :disabled="acting" @click="generate">重新生成</el-button>
        <el-button
          type="primary"
          size="small"
          :loading="acting"
          data-testid="summary-confirm"
          @click="confirm"
        >
          确认写入客户档案
        </el-button>
      </div>
    </template>
    <template v-else>
      <p class="text" data-testid="summary-confirmed">{{ summary.summary }}</p>
      <div class="confirmed-tags">
        <el-tag v-for="t in summary.tags" :key="t" size="small" type="info">{{ t }}</el-tag>
      </div>
    </template>
  </div>
</template>

<style scoped>
.card {
  margin: 8px 16px;
  padding: 10px 12px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px;
  background: var(--el-fill-color-lighter);
}

.head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 6px;
}

.title {
  font-weight: 600;
  font-size: 13px;
}

.muted {
  margin: 0 0 6px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.tags {
  margin-top: 6px;
}

.actions {
  display: flex;
  justify-content: flex-end;
  gap: 6px;
  margin-top: 8px;
}

.text {
  margin: 0 0 6px;
  font-size: 13px;
  white-space: pre-wrap;
}

.confirmed-tags {
  display: flex;
  gap: 4px;
  flex-wrap: wrap;
}
</style>
