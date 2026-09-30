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
  if (form.zone_enabled && !(form.zone_program_id && form.zone_ability_id)) {
    ElMessage.warning('开启专区时请填写专区程序 ID 和能力 ID')
    return
  }
  saving.value = true
  const { data, error } = await api.PUT('/api/v1/admin/integrations/wecom/settings', {
    body: {
      ...form,
      welcome_kf_id: form.welcome_kf_id || null,
      zone_program_id: form.zone_program_id || null,
      zone_ability_id: form.zone_ability_id || null,
    },
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
    <el-divider content-position="left">微信客服菜单消息</el-divider>
    <el-form-item label="「转人工」按钮">
      <el-switch v-model="form.kf_handoff_menu" data-testid="kf-handoff-menu" />
      <span class="muted hint">AI 的回答带一个按钮，客户点一下就转人工（点选同时重置回复额度）</span>
    </el-form-item>
    <el-form-item label="满意度评价">
      <el-switch v-model="form.kf_csat_menu" data-testid="kf-csat-menu" />
      <span class="muted hint">人工接待的会话结束时发送评价按钮（与结束提示合并成一条）</span>
    </el-form-item>
    <el-divider content-position="left">数据与智能专区（可选）</el-divider>
    <el-form-item label="取回群聊分析">
      <el-switch v-model="form.zone_enabled" data-testid="zone-enabled" />
      <span class="muted hint">
        需要企业购买会话存档并授权专区；群聊原文只在专区内处理，平台只取回摘要、情绪和问答候选
      </span>
    </el-form-item>
    <el-form-item label="专区程序 ID">
      <el-input
        v-model="form.zone_program_id"
        :disabled="!form.zone_enabled"
        maxlength="128"
        class="select"
        placeholder="在专区部署分析程序后获得"
      />
    </el-form-item>
    <el-form-item label="能力 ID">
      <el-input
        v-model="form.zone_ability_id"
        :disabled="!form.zone_enabled"
        maxlength="128"
        class="select"
      />
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
