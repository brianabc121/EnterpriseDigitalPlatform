<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { ref } from 'vue'

import { api } from '../../api'
import AiOutcomeCard from './AiOutcomeCard.vue'

/** 试一试：用当前设置和已发布的知识回答一个问题（不会发给任何客户），看回复、依据和转人工判定。 */
const question = ref('')
const asking = ref(false)
const history = ref<{ id: number; question: string; outcome: Schemas['AiOutcome'] }[]>([])
let seq = 0

async function ask(): Promise<void> {
  const q = question.value.trim()
  if (!q || asking.value) return
  asking.value = true
  const { data, error } = await api.POST('/api/v1/ai/test', { body: { question: q } })
  asking.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  seq += 1
  history.value = [{ id: seq, question: q, outcome: data }, ...history.value].slice(0, 20)
  question.value = ''
}
</script>

<template>
  <div class="console" data-testid="ai-test">
    <div class="ask">
      <el-input
        v-model="question"
        placeholder="以客户的口吻提问，例如：快递几天能到？"
        maxlength="2000"
        data-testid="ai-test-input"
        @keyup.enter="ask"
      />
      <el-button type="primary" :loading="asking" data-testid="ai-test-ask" @click="ask">
        提问
      </el-button>
    </div>
    <p class="muted">每次提问都是独立的一轮，会调用大模型并计入用量，但不计入 AI 回复额度。</p>
    <div class="results">
      <el-card v-for="h in history" :key="h.id" shadow="never" class="result">
        <AiOutcomeCard :outcome="h.outcome" :question="h.question" />
      </el-card>
      <el-empty
        v-if="history.length === 0"
        :image-size="60"
        description="提个问题看看 AI 怎么回答"
      />
    </div>
  </div>
</template>

<style scoped>
.console {
  max-width: 820px;
}

.ask {
  display: flex;
  gap: 8px;
}

.muted {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.result {
  margin-bottom: 12px;
}
</style>
