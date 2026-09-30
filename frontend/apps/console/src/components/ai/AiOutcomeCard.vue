<script setup lang="ts">
import type { Schemas } from '@edp/api-client'
import { computed } from 'vue'

import { activeSignals, GUARD_LABEL } from '../../ai'
import { HANDOFF_REASON } from '../../labels'
import { percent } from '../../reports'

/** 一次 AI 判定：回复或转人工、原因、依据的知识和各项信号（试一试与会话记录共用）。 */
const props = defineProps<{ outcome: Schemas['AiOutcome']; question?: string }>()

const verdict = computed(() => {
  const o = props.outcome
  if (o.action === 'handoff') {
    return { text: '转人工', type: 'warning' as const }
  }
  if (o.reason === 'guardrail_retry') return { text: '兜底回复', type: 'info' as const }
  // 价格保护（套价、回复里出现内部价格信息）：改用固定话术。
  if (o.reason === 'price_probe' || o.reason === 'reply_blocked') {
    return { text: '固定话术', type: 'info' as const }
  }
  return { text: '回复', type: 'success' as const }
})
const FIXED = ['guardrail_retry', 'price_probe', 'reply_blocked']
const reason = computed(() => {
  const r = props.outcome.reason
  if (!r || FIXED.includes(r)) return ''
  return HANDOFF_REASON[r] ?? r
})
const signals = computed(() => activeSignals(props.outcome.signals))
const details = computed(() => {
  const s = props.outcome.signals as Record<string, unknown>
  const parts: string[] = []
  if (typeof s.best_relevance === 'number') parts.push(`知识相关度 ${percent(s.best_relevance)}`)
  if (typeof s.confidence === 'number') parts.push(`模型把握 ${percent(s.confidence)}`)
  if (typeof s.model_reason === 'string' && s.model_reason)
    parts.push(`模型说明：${s.model_reason}`)
  return parts.join(' · ')
})
</script>

<template>
  <div class="outcome" data-testid="ai-outcome">
    <p v-if="question" class="question">{{ question }}</p>
    <div class="head">
      <el-tag size="small" :type="verdict.type" data-testid="ai-verdict">{{ verdict.text }}</el-tag>
      <span v-if="reason" class="reason" data-testid="ai-reason">{{ reason }}</span>
      <span v-if="outcome.guard" class="guard">
        未通过：{{ GUARD_LABEL[outcome.guard] ?? outcome.guard }}
      </span>
      <span v-if="outcome.score" class="score">信号得分 {{ outcome.score }}</span>
    </div>
    <p v-if="outcome.reply" class="reply" data-testid="ai-reply">{{ outcome.reply }}</p>
    <div v-if="signals.length || details" class="signals">
      <el-tag v-for="s in signals" :key="s" size="small" type="warning" effect="plain">{{
        s
      }}</el-tag>
      <span v-if="details" class="muted">{{ details }}</span>
    </div>
    <div v-if="outcome.knowledge.length" class="knowledge">
      <span class="muted">依据：</span>
      <span v-for="k in outcome.knowledge" :key="k.item_id" class="ref" data-testid="ai-knowledge">
        {{ k.title }}<small>{{ percent(k.score) }}</small>
      </span>
    </div>
    <p v-else-if="outcome.action === 'reply' || outcome.reason === 'model_request'" class="muted">
      没有找到相关知识
    </p>
  </div>
</template>

<style scoped>
.outcome {
  font-size: 13px;
}

.question {
  margin: 0 0 6px;
  font-weight: 500;
}

.head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}

.reason {
  font-weight: 500;
}

.guard {
  color: var(--el-color-danger);
  font-size: 12px;
}

.score {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.reply {
  margin: 8px 0 0;
  padding: 8px 12px;
  border-radius: 6px;
  background: var(--el-color-success-light-9);
  white-space: pre-wrap;
  word-break: break-word;
}

.signals,
.knowledge {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  margin-top: 8px;
}

.ref {
  padding: 1px 8px;
  border-radius: 10px;
  background: var(--el-fill-color);
  font-size: 12px;
}

.ref small {
  margin-left: 4px;
  color: var(--el-text-color-secondary);
}

.muted {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
</style>
