<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { reactive, ref } from 'vue'

import { api } from '../../api'

/** 新客户欢迎语（可附带微信客服链接）、标签写回、应用消息提醒。 */
const props = defineProps<{
  settings: Schemas['WecomSettings']
  accounts: Schemas['KfAccountOut'][]
}>()
const emit = defineEmits<{ saved: [] }>()

const form = reactive<Schemas['WecomSettings']>({ ...props.settings })
const saving = ref(false)

async function save(): Promise<void> {
  saving.value = true
  const { data, error } = await api.PUT('/api/v1/admin/integrations/wecom/settings', {
    body: { ...form, welcome_kf_id: form.welcome_kf_id || null },
  })
  saving.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  ElMessage.success('已保存')
  emit('saved')
}
</script>

<template>
  <el-form label-width="140px" class="form" data-testid="wecom-settings" @submit.prevent="save">
    <el-form-item label="新客户欢迎语">
      <el-switch v-model="form.welcome_enabled" data-testid="welcome-enabled" />
      <span class="muted hint">员工添加新客户时自动发送；与企业微信后台配置的欢迎语互斥</span>
    </el-form-item>
    <el-form-item label="欢迎语内容">
      <el-input
        v-model="form.welcome_text"
        type="textarea"
        :rows="3"
        maxlength="1000"
        show-word-limit
        :disabled="!form.welcome_enabled"
        data-testid="welcome-text"
      />
    </el-form-item>
    <el-form-item label="附带客服入口">
      <el-select
        v-model="form.welcome_kf_id"
        clearable
        placeholder="不附带"
        :disabled="!form.welcome_enabled"
        class="select"
        data-testid="welcome-kf"
      >
        <el-option
          v-for="a in props.accounts.filter((x) => x.status === 'active')"
          :key="a.open_kfid"
          :label="a.name"
          :value="a.open_kfid"
        />
      </el-select>
      <span class="muted hint">客户点链接进入微信客服，由 AI 客服接待</span>
    </el-form-item>
    <el-form-item label="标签写回">
      <el-switch v-model="form.tag_writeback" />
      <span class="muted hint">平台上给客户打的企业标签同步到企业微信</span>
    </el-form-item>
    <el-form-item label="应用消息提醒">
      <el-switch v-model="form.notify_agents" />
      <span class="muted hint">新会话分配、转接请求、必读知识、知识周报推送到员工的企业微信</span>
    </el-form-item>
    <el-form-item>
      <el-button
        type="primary"
        native-type="submit"
        :loading="saving"
        data-testid="wecom-settings-save"
      >
        保存
      </el-button>
    </el-form-item>
  </el-form>
</template>

<style scoped>
.form {
  max-width: 720px;
}

.select {
  width: 240px;
}

.muted {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.hint {
  margin-left: 12px;
}
</style>
