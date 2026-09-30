<script setup lang="ts">
import { errorMessage } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { computed, ref } from 'vue'

import { api } from '../../api'
import { useAuthStore } from '../../stores/auth'

const open = defineModel<boolean>({ required: true })
const props = defineProps<{ q: string }>()

const auth = useAuthStore()
const plaintext = computed(() => auth.can('customer:view_sensitive'))
const password = ref('')
const exporting = ref(false)

function save(blob: Blob): void {
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = `customers-${new Date().toISOString().slice(0, 10)}.csv`
  link.click()
  URL.revokeObjectURL(url)
}

async function run(): Promise<void> {
  if (!password.value) {
    ElMessage.warning('请输入你的登录密码')
    return
  }
  exporting.value = true
  const { data, error } = await api.POST('/api/v1/customers/export', {
    body: { password: password.value, q: props.q.trim() || null },
    parseAs: 'blob',
  })
  exporting.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  save(data as Blob)
  ElMessage.success('已导出')
  open.value = false
}
</script>

<template>
  <el-dialog
    v-model="open"
    title="导出客户"
    width="440px"
    data-testid="export-dialog"
    @open="password = ''"
  >
    <p class="hint">
      导出你可以看到的{{ props.q.trim() ? '、符合搜索条件的' : '' }}客户（CSV）。手机号和邮箱{{
        plaintext ? '导出完整内容' : '导出掩码'
      }}，导出操作会记入操作日志。
    </p>
    <el-input
      v-model="password"
      type="password"
      show-password
      placeholder="请输入你的登录密码确认"
      autocomplete="current-password"
      data-testid="export-password"
      @keyup.enter="run"
    />
    <template #footer>
      <el-button @click="open = false">取消</el-button>
      <el-button type="primary" :loading="exporting" data-testid="export-confirm" @click="run">
        导出
      </el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.hint {
  margin: 0 0 12px;
  color: var(--el-text-color-secondary);
  line-height: 1.6;
}
</style>
