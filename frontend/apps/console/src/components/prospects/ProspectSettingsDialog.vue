<script setup lang="ts">
import { errorMessage } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { reactive, ref, watch } from 'vue'

import { api } from '../../api'
import { AI_MODES, STAGES, type ProspectSettings } from '../../prospects'

/** 意向客户设置（设计文档 §35.6）：AI 转入的方式、最低意向、默认几天后跟进。 */
const open = defineModel<boolean>({ required: true })
const emit = defineEmits<{ saved: [settings: ProspectSettings] }>()

const form = reactive<ProspectSettings>({
  ai_mode: 'auto',
  min_stage: 2,
  follow_days: 3,
  assignment: 'owner',
  amount_visibility: 'all',
})
const saving = ref(false)

async function load(): Promise<void> {
  const { data, error } = await api.GET('/api/v1/opportunities/settings')
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  Object.assign(form, data.settings)
}

async function save(): Promise<void> {
  saving.value = true
  const { data, error } = await api.PUT('/api/v1/opportunities/settings', { body: { ...form } })
  saving.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  ElMessage.success('已保存意向客户设置')
  open.value = false
  emit('saved', data.settings)
}

watch(open, (value) => {
  if (value) void load()
})
</script>

<template>
  <el-dialog v-model="open" title="意向客户设置" width="520px" data-testid="prospect-settings">
    <el-form label-width="120px" @submit.prevent="save">
      <el-form-item label="AI 转入">
        <el-radio-group v-model="form.ai_mode" class="modes" data-testid="prospect-ai-mode">
          <el-radio v-for="[value, label, hint] in AI_MODES" :key="value" :value="value">
            {{ label }}<span class="hint">{{ hint }}</span>
          </el-radio>
        </el-radio-group>
      </el-form-item>
      <el-form-item label="最低意向">
        <el-select
          v-model="form.min_stage"
          :disabled="form.ai_mode === 'off'"
          data-testid="prospect-min-stage"
        >
          <el-option v-for="[value, label] in STAGES" :key="value" :label="label" :value="value" />
        </el-select>
        <div class="tip">会话结束后，意图判断的最高意向达到这一级、之后没有下单的客户由 AI 转入</div>
      </el-form-item>
      <el-form-item label="默认跟进">
        <el-input-number
          v-model="form.follow_days"
          :min="1"
          :max="60"
          data-testid="prospect-follow-days"
        />
        <span class="unit">天后</span>
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="open = false">取消</el-button>
      <el-button
        type="primary"
        :loading="saving"
        data-testid="prospect-settings-save"
        @click="save"
      >
        保存
      </el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.modes {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 4px;
}

.hint {
  margin-left: 8px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.tip {
  width: 100%;
  font-size: 12px;
  line-height: 1.5;
  color: var(--el-text-color-secondary);
}

.unit {
  margin-left: 8px;
}
</style>
