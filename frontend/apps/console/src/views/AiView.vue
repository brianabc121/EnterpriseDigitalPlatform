<script setup lang="ts">
import { ref } from 'vue'

import AiEvaluation from '../components/ai/AiEvaluation.vue'
import AiSettingsForm from '../components/ai/AiSettingsForm.vue'
import AiTestConsole from '../components/ai/AiTestConsole.vue'

const tab = ref('settings')
</script>

<template>
  <div>
    <div class="page-header">
      <h2>AI 接待</h2>
    </div>
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
    </el-tabs>
  </div>
</template>
