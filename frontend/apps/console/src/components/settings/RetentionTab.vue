<script setup lang="ts">
import { errorMessage } from '@edp/api-client'
import { ElMessage, ElMessageBox } from 'element-plus'
import { onMounted, reactive, ref } from 'vue'

import { api } from '../../api'

const form = reactive<{ messagesDays: number | null; filesDays: number | null }>({
  messagesDays: null,
  filesDays: null,
})
const loading = ref(false)
const saving = ref(false)

async function load(): Promise<void> {
  loading.value = true
  const { data, error } = await api.GET('/api/v1/tenant/retention')
  loading.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  form.messagesDays = data.messages_days ?? null
  form.filesDays = data.files_days ?? null
}

async function save(): Promise<void> {
  if (form.messagesDays || form.filesDays) {
    try {
      await ElMessageBox.confirm(
        '到期的聊天记录和文件每小时自动删除，删除后无法恢复。确认保存？',
        '设置保留期',
        { type: 'warning' },
      )
    } catch {
      return
    }
  }
  saving.value = true
  const { data, error } = await api.PUT('/api/v1/tenant/retention', {
    body: { messages_days: form.messagesDays || null, files_days: form.filesDays || null },
  })
  saving.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  ElMessage.success('已保存保留期')
}

onMounted(load)
</script>

<template>
  <div v-loading="loading" class="retention" data-testid="retention-form">
    <el-alert
      type="info"
      :closable="false"
      show-icon
      title="到期的消息删除后，会话记录（时间、接待人、满意度）仍然保留，用于报表统计。留空表示一直保留。"
    />
    <el-form label-width="140px" class="form" @submit.prevent="save">
      <el-form-item label="聊天消息保留">
        <el-input-number
          v-model="form.messagesDays"
          :min="30"
          :max="3650"
          :step="30"
          placeholder="一直保留"
          data-testid="retention-messages"
        />
        <span class="unit">天（最少 30 天）</span>
      </el-form-item>
      <el-form-item label="聊天文件保留">
        <el-input-number
          v-model="form.filesDays"
          :min="7"
          :max="3650"
          :step="30"
          placeholder="与消息相同"
          data-testid="retention-files"
        />
        <span class="unit">天（图片、文件、语音；到期后消息里显示"文件已过期"）</span>
      </el-form-item>
      <el-form-item>
        <el-button type="primary" :loading="saving" data-testid="retention-save" @click="save">
          保存
        </el-button>
      </el-form-item>
    </el-form>
  </div>
</template>

<style scoped>
.form {
  margin-top: 16px;
  max-width: 640px;
}

.unit {
  margin-left: 8px;
  color: var(--el-text-color-secondary);
}
</style>
