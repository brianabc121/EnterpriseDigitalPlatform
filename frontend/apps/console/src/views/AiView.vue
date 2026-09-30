<script setup lang="ts">
import { ref } from 'vue'

import AiEvaluation from '../components/ai/AiEvaluation.vue'
import AiSettingsForm from '../components/ai/AiSettingsForm.vue'
import AiTestConsole from '../components/ai/AiTestConsole.vue'
import OwnLlmForm from '../components/ai/OwnLlmForm.vue'
import { useAuthStore } from '../stores/auth'

const auth = useAuthStore()

const tab = ref('settings')
</script>

<template>
  <div>
    <div class="page-header">
      <h2>AI 接待</h2>
    </div>
    <el-alert
      v-if="auth.me?.features?.ai === false"
      type="warning"
      :closable="false"
      show-icon
      title="当前套餐不包含 AI 接待，会话都由人工接待；升级套餐请联系平台"
      class="notice"
    />
    <el-tabs v-model="tab" data-testid="ai-tabs">
      <el-tab-pane label="设置" name="settings">
        <AiSettingsForm />
      </el-tab-pane>
      <el-tab-pane label="试一试" name="test" lazy>
        <AiTestConsole />
      </el-tab-pane>
      <el-tab-pane label="评测" name="evaluation" lazy>
        <AiEvaluation />
      </el-tab-pane>
      <el-tab-pane label="大模型接口" name="llm" lazy>
        <OwnLlmForm />
      </el-tab-pane>
    </el-tabs>
  </div>
</template>

<style scoped>
.notice {
  margin-bottom: 12px;
}
</style>
