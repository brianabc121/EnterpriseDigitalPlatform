<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage, ElMessageBox } from 'element-plus'
import { onMounted, reactive, ref } from 'vue'

import { api } from '../../api'

const SOURCE: Record<string, string> = {
  tenant: '自带的接口',
  provider: '平台为本企业指定的模型',
  default: '平台提供的模型',
  env: '平台提供的模型',
  none: '没有可用的大模型',
}

const config = ref<Schemas['TenantLlmConfig'] | null>(null)
const editing = ref(false)
const saving = ref(false)
const result = ref('')
const form = reactive({
  baseUrl: '',
  apiKey: '',
  chatModel: '',
  fastModel: '',
  supportsTools: false,
  enabled: true,
})

async function load(): Promise<void> {
  const { data, error } = await api.GET('/api/v1/ai/llm')
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  config.value = data
}

function edit(): void {
  const own = config.value?.own
  Object.assign(form, {
    baseUrl: own?.base_url ?? '',
    apiKey: '',
    chatModel: own?.chat_model ?? '',
    fastModel: own?.fast_model ?? '',
    supportsTools: own?.supports_tools ?? false,
    enabled: own?.enabled ?? true,
  })
  editing.value = true
}

async function save(): Promise<void> {
  saving.value = true
  const { data, error } = await api.PUT('/api/v1/ai/llm', {
    body: {
      base_url: form.baseUrl.trim(),
      api_key: form.apiKey ? form.apiKey : undefined,
      chat_model: form.chatModel.trim(),
      fast_model: form.fastModel.trim(),
      supports_tools: form.supportsTools,
      enabled: form.enabled,
    },
  })
  saving.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  config.value = data
  editing.value = false
  ElMessage.success('已保存')
}

async function test(): Promise<void> {
  result.value = '检查中…'
  const { data, error } = await api.POST('/api/v1/ai/llm/test')
  if (!data) {
    result.value = errorMessage(error)
    return
  }
  result.value = data.chat.ok
    ? `连接正常（${data.chat.latency_ms} ms，模型 ${data.chat.model}）`
    : `连接失败：${data.chat.error}`
}

async function remove(): Promise<void> {
  const confirmed = await ElMessageBox.confirm('改用平台提供的大模型？', '不再使用自带接口', {
    confirmButtonText: '确定',
    cancelButtonText: '取消',
  }).catch(() => false)
  if (!confirmed) return
  const { data, error } = await api.DELETE('/api/v1/ai/llm')
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  config.value = data
  result.value = ''
}

onMounted(load)
</script>

<template>
  <div v-if="config" data-testid="own-llm">
    <el-descriptions :column="2" border size="small">
      <el-descriptions-item label="正在使用">{{ SOURCE[config.source] ?? config.source }}</el-descriptions-item>
      <el-descriptions-item label="说明">
        {{ config.source === 'tenant' ? `${config.own?.base_url}（${config.own?.chat_model}）` : '费用包含在套餐内' }}
      </el-descriptions-item>
    </el-descriptions>
    <p class="sub">
      可以改用自己的大模型接口（OpenAI 兼容，如 DeepSeek、通义千问、智谱等），AI 接待、坐席助手和知识提炼都走自己的接口，
      费用由贵企业直接与模型厂商结算。接口密钥加密保存。知识库检索的向量模型仍由平台提供。
    </p>
    <div class="actions">
      <el-button data-testid="own-llm-edit" @click="edit">{{ config.own ? '修改自带接口' : '使用自带接口' }}</el-button>
      <template v-if="config.own">
        <el-button @click="test">检查连通</el-button>
        <el-button type="danger" plain @click="remove">不再使用</el-button>
      </template>
      <span v-if="result" class="sub" data-testid="own-llm-result">{{ result }}</span>
    </div>
    <el-form v-if="editing" label-width="110px" class="form">
      <el-form-item label="接口地址" required>
        <el-input v-model="form.baseUrl" placeholder="https://api.deepseek.com/v1" data-testid="own-llm-url" />
      </el-form-item>
      <el-form-item label="API Key">
        <el-input
          v-model="form.apiKey"
          type="password"
          show-password
          :placeholder="config.own?.api_key_set ? '不填表示不修改' : ''"
          data-testid="own-llm-key"
        />
      </el-form-item>
      <el-form-item label="对话模型" required>
        <el-input v-model="form.chatModel" placeholder="如 deepseek-chat" data-testid="own-llm-model" />
      </el-form-item>
      <el-form-item label="轻量模型">
        <el-input v-model="form.fastModel" placeholder="摘要等轻量任务，可不填" />
      </el-form-item>
      <el-form-item label="工具调用">
        <el-switch v-model="form.supportsTools" data-testid="own-llm-tools" />
        <span class="sub hint">模型支持函数调用（tools）时打开，AI 接待才能查档案、登记线索、转人工</span>
      </el-form-item>
      <el-form-item label="启用"><el-switch v-model="form.enabled" /></el-form-item>
      <el-form-item>
        <el-button type="primary" :loading="saving" data-testid="own-llm-save" @click="save">保存</el-button>
        <el-button @click="editing = false">取消</el-button>
      </el-form-item>
    </el-form>
  </div>
</template>

<style scoped>
.actions {
  display: flex;
  gap: 8px;
  align-items: center;
  flex-wrap: wrap;
}

.form {
  max-width: 560px;
  margin-top: 16px;
}

.sub {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.hint {
  margin-left: 8px;
}
</style>
