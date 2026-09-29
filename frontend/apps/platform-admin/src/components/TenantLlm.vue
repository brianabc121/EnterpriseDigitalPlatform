<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { onMounted, ref } from 'vue'

import { api } from '../api'
import { LLM_SOURCE } from '../labels'

const props = defineProps<{ tenantId: string }>()

const current = ref<Schemas['TenantLlmOut'] | null>(null)
const providers = ref<Schemas['LlmProviderOut'][]>([])
const selected = ref<string>('')
const saving = ref(false)

async function load(): Promise<void> {
  const [info, list] = await Promise.all([
    api.GET('/platform/v1/tenants/{tenant_id}/llm', {
      params: { path: { tenant_id: props.tenantId } },
    }),
    api.GET('/platform/v1/llm-providers'),
  ])
  if (!info.data) {
    ElMessage.error(errorMessage(info.error))
    return
  }
  current.value = info.data
  selected.value = info.data.provider_id ?? ''
  providers.value = list.data?.items ?? []
}

async function save(): Promise<void> {
  saving.value = true
  const { data, error } = await api.PUT('/platform/v1/tenants/{tenant_id}/llm', {
    params: { path: { tenant_id: props.tenantId } },
    body: { provider_id: selected.value || null },
  })
  saving.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  current.value = data
  ElMessage.success('已保存')
}

onMounted(load)
</script>

<template>
  <div v-if="current" data-testid="tenant-llm">
    <el-descriptions :column="2" border size="small">
      <el-descriptions-item label="正在使用">
        {{ LLM_SOURCE[current.source] ?? current.source }}
      </el-descriptions-item>
      <el-descriptions-item label="供应商">{{ current.provider_name ?? '—' }}</el-descriptions-item>
    </el-descriptions>
    <el-form label-width="110px" class="form">
      <el-form-item label="指定供应商">
        <el-select v-model="selected" placeholder="平台默认供应商" clearable>
          <el-option
            v-for="p in providers"
            :key="p.id"
            :label="`${p.name}（${p.chat_model}）`"
            :value="p.id"
            :disabled="!p.enabled"
          />
        </el-select>
        <el-button type="primary" :loading="saving" class="save" @click="save">保存</el-button>
      </el-form-item>
      <p class="sub">
        给大客户指定专属模型；为空时用平台默认供应商。租户在控制台配置了自带的接口密钥时，以租户的为准。
      </p>
    </el-form>
  </div>
</template>

<style scoped>
.form {
  margin-top: 16px;
}

.save {
  margin-left: 8px;
}

.sub {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
</style>
