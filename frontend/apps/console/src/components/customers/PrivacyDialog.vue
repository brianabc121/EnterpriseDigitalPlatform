<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage, ElMessageBox } from 'element-plus'
import { reactive, ref } from 'vue'

import { api } from '../../api'

/** 个人信息查询（生成副本）与删除请求。 */
const open = defineModel<boolean>({ required: true })
const props = defineProps<{ customer: Schemas['CustomerOut'] | null }>()
const emit = defineEmits<{ erased: [] }>()

const mode = ref<'access' | 'erase'>('access')
const form = reactive({ reason: '', confirmName: '' })
const busy = ref(false)

function reset(): void {
  mode.value = 'access'
  Object.assign(form, { reason: '', confirmName: '' })
}

function download(document: Schemas['PersonalData']): void {
  const blob = new Blob([JSON.stringify(document, null, 2)], { type: 'application/json' })
  const url = URL.createObjectURL(blob)
  const link = window.document.createElement('a')
  link.href = url
  link.download = `personal-data-${props.customer?.id}.json`
  link.click()
  URL.revokeObjectURL(url)
}

async function access(): Promise<void> {
  if (!props.customer || !form.reason.trim()) {
    ElMessage.warning('请填写请求来源')
    return
  }
  busy.value = true
  const { data, error } = await api.POST('/api/v1/customers/{customer_id}/personal-data', {
    params: { path: { customer_id: props.customer.id } },
    body: { reason: form.reason.trim() },
  })
  busy.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  download(data)
  ElMessage.success(`已生成个人信息副本（${data.messages.length} 条消息）`)
  open.value = false
}

async function erase(): Promise<void> {
  if (!props.customer || !form.reason.trim() || !form.confirmName.trim()) {
    ElMessage.warning('请填写请求来源并输入客户名称确认')
    return
  }
  try {
    await ElMessageBox.confirm(
      '删除后客户档案、会话、消息、留言和聊天文件都无法恢复。',
      '删除个人信息',
      { type: 'error', confirmButtonText: '确认删除' },
    )
  } catch {
    return
  }
  busy.value = true
  const { data, error } = await api.POST('/api/v1/customers/{customer_id}/erase', {
    params: { path: { customer_id: props.customer.id } },
    body: { confirm_name: form.confirmName.trim(), reason: form.reason.trim() },
  })
  busy.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  ElMessage.success(`已删除：${data.sessions} 个会话、${data.messages} 条消息、${data.files} 个文件`)
  if (data.wecom_contact) {
    ElMessage.warning('这位客户是企业微信的外部联系人，请在企业微信里删除好友，否则下次同步会重新建档')
  }
  open.value = false
  emit('erased')
}
</script>

<template>
  <el-dialog
    v-model="open"
    title="个人信息请求"
    width="520px"
    data-testid="privacy-dialog"
    @open="reset"
  >
    <el-radio-group v-model="mode" class="modes">
      <el-radio-button value="access">查询（生成副本）</el-radio-button>
      <el-radio-button value="erase">删除</el-radio-button>
    </el-radio-group>
    <el-form label-position="top">
      <el-form-item label="请求来源" required>
        <el-input
          v-model="form.reason"
          maxlength="500"
          placeholder="例如：客户来电要求查询 / 删除个人信息"
          data-testid="privacy-reason"
        />
      </el-form-item>
      <el-form-item v-if="mode === 'erase'" :label="`输入客户名称「${customer?.display_name}」确认`" required>
        <el-input v-model="form.confirmName" data-testid="privacy-confirm-name" />
      </el-form-item>
    </el-form>
    <p class="hint">
      {{
        mode === 'access'
          ? '副本包含客户档案、联系方式、渠道身份、会话、消息和留言（JSON 文件），请求会被记录。'
          : '删除客户及其会话、消息、留言和聊天文件，解散服务群；只保留一条不含个人信息的处理记录。'
      }}
    </p>
    <template #footer>
      <el-button @click="open = false">取消</el-button>
      <el-button
        v-if="mode === 'access'"
        type="primary"
        :loading="busy"
        data-testid="privacy-access"
        @click="access"
      >
        生成副本
      </el-button>
      <el-button v-else type="danger" :loading="busy" data-testid="privacy-erase" @click="erase">
        删除
      </el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.modes {
  margin-bottom: 16px;
}

.hint {
  margin: 0;
  color: var(--el-text-color-secondary);
  font-size: 13px;
  line-height: 1.6;
}
</style>
