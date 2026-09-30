<script setup lang="ts">
import { errorMessage } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { onMounted, reactive, ref } from 'vue'

import { api } from '../api'

const form = reactive({ words: '', applyToAi: true, blockAgentMessages: true })
const saving = ref(false)
const count = ref(0)

async function load(): Promise<void> {
  const { data, error } = await api.GET('/platform/v1/settings/content-policy')
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  form.words = (data.words ?? []).join('\n')
  form.applyToAi = data.apply_to_ai
  form.blockAgentMessages = data.block_agent_messages
  count.value = (data.words ?? []).length
}

async function save(): Promise<void> {
  const words = form.words
    .split(/[\n,，]/)
    .map((w) => w.trim())
    .filter(Boolean)
  const tooLong = words.find((w) => w.length > 32)
  if (tooLong) {
    ElMessage.warning(`敏感词不能超过 32 个字：${tooLong}`)
    return
  }
  saving.value = true
  const { data, error } = await api.PUT('/platform/v1/settings/content-policy', {
    body: { words, apply_to_ai: form.applyToAi, block_agent_messages: form.blockAgentMessages },
  })
  saving.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  form.words = (data.words ?? []).join('\n')
  count.value = (data.words ?? []).length
  ElMessage.success('已保存，所有租户立即生效')
}

onMounted(load)
</script>

<template>
  <div class="page">
    <h2>内容安全</h2>
    <p class="sub">
      平台敏感词在各租户自己的敏感词之外生效。每行一个词（也可以用逗号分隔），当前 {{ count }} 个。
    </p>
    <el-form label-width="140px">
      <el-form-item label="平台敏感词">
        <el-input
          v-model="form.words"
          type="textarea"
          :rows="12"
          placeholder="每行一个"
          data-testid="content-words"
        />
      </el-form-item>
      <el-form-item label="AI 接待">
        <el-switch v-model="form.applyToAi" />
        <span class="sub hint">客户提到时转人工；AI 回复中出现时不发送</span>
      </el-form-item>
      <el-form-item label="坐席消息">
        <el-switch v-model="form.blockAgentMessages" />
        <span class="sub hint">出现时拒绝发送，并提示命中的词</span>
      </el-form-item>
      <el-form-item>
        <el-button type="primary" :loading="saving" data-testid="content-save" @click="save">保存</el-button>
      </el-form-item>
    </el-form>
  </div>
</template>

<style scoped>
.page {
  max-width: 760px;
}

h2 {
  margin: 0 0 8px;
  font-size: 18px;
}

.sub {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.hint {
  margin-left: 8px;
}
</style>
