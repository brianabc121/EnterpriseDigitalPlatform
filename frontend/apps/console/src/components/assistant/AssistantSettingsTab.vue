<script setup lang="ts">
import { errorMessage } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { onMounted, reactive, ref } from 'vue'

import { api } from '../../api'
import { REPLY_MODE, type AssistantSettings } from '../../assistant'

/** 助理设置：名称、人设、群里的默认行为、群提炼、通过助理发通知、每人每分钟的提问次数。 */
const form = reactive<AssistantSettings>({
  enabled: true,
  name: '小助',
  persona: '',
  group_reply_mode: 'silent',
  group_extraction: true,
  notify_enabled: true,
  per_minute: 20,
})
const loading = ref(false)
const saving = ref(false)

async function load(): Promise<void> {
  loading.value = true
  const { data, error } = await api.GET('/api/v1/assistant/settings')
  loading.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  Object.assign(form, data)
}

async function save(): Promise<void> {
  saving.value = true
  const { data, error } = await api.PUT('/api/v1/assistant/settings', { body: { ...form } })
  saving.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  Object.assign(form, data)
  ElMessage.success('已保存')
}

onMounted(load)
</script>

<template>
  <el-form v-loading="loading" label-width="160px" class="form" data-testid="assistant-settings">
    <el-form-item label="启用 AI 助理">
      <el-switch v-model="form.enabled" data-testid="assistant-enabled" />
    </el-form-item>
    <el-form-item label="助理名称">
      <el-input v-model="form.name" maxlength="32" style="width: 240px" data-testid="assistant-name" />
    </el-form-item>
    <el-form-item label="语气与风格">
      <el-input
        v-model="form.persona"
        type="textarea"
        :rows="3"
        maxlength="500"
        placeholder="例如：称呼同事为「老师」，回答尽量简短"
      />
    </el-form-item>
    <el-form-item label="群里的默认行为">
      <el-radio-group v-model="form.group_reply_mode" data-testid="assistant-group-mode">
        <el-radio-button v-for="(label, value) in REPLY_MODE" :key="value" :value="value">{{ label }}</el-radio-button>
      </el-radio-group>
      <div class="hint">被 @ 时回答也只回答已绑定员工的提问；可以在「群组」里按群单独设置。</div>
    </el-form-item>
    <el-form-item label="从群聊提炼知识">
      <el-switch v-model="form.group_extraction" data-testid="assistant-group-extraction" />
      <div class="hint">调度进程每小时把新的群消息脱敏、匿名后提炼成问答候选，进入知识库的审核台（来源"群聊"）。</div>
    </el-form-item>
    <el-form-item label="通过助理发送提醒">
      <el-switch v-model="form.notify_enabled" data-testid="assistant-notify" />
      <div class="hint">新会话分配、待办、个人待办、必读知识等平台提醒，发给绑定了的员工（企业微信走应用消息）。</div>
    </el-form-item>
    <el-form-item label="每人每分钟最多提问">
      <el-input-number v-model="form.per_minute" :min="1" :max="120" />
    </el-form-item>
    <el-form-item>
      <el-button type="primary" :loading="saving" data-testid="assistant-settings-save" @click="save">保存</el-button>
    </el-form-item>
  </el-form>
</template>

<style scoped>
.form {
  max-width: 760px;
}

.hint {
  width: 100%;
  font-size: 12px;
  line-height: 1.6;
  color: var(--el-text-color-secondary);
}
</style>
