<script setup lang="ts">
import { errorMessage } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { ref } from 'vue'

import { downloadBlob } from '../../download'

/**
 * 需要再次输入密码的导出（订单、待办）：说明导出的范围和是否含明文，导出操作记入操作日志。
 * exporter 用密码发起导出请求，返回文件内容。
 */
const open = defineModel<boolean>({ required: true })
const props = defineProps<{
  title: string
  hint: string
  filename: string
  exporter: (password: string) => Promise<{ data?: unknown; error?: unknown }>
}>()

const password = ref('')
const exporting = ref(false)

async function run(): Promise<void> {
  if (!password.value) {
    ElMessage.warning('请输入你的登录密码')
    return
  }
  exporting.value = true
  const { data, error } = await props.exporter(password.value)
  exporting.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  downloadBlob(data as Blob, props.filename)
  ElMessage.success('已导出')
  open.value = false
}
</script>

<template>
  <el-dialog v-model="open" :title="title" width="440px" data-testid="export-dialog" @open="password = ''">
    <p class="hint">{{ hint }}</p>
    <el-input
      v-model="password"
      type="password"
      show-password
      placeholder="输入你的登录密码确认"
      data-testid="export-password"
      @keyup.enter="run"
    />
    <template #footer>
      <el-button @click="open = false">取消</el-button>
      <el-button type="primary" :loading="exporting" data-testid="export-submit" @click="run">导出</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.hint {
  margin: 0 0 12px;
  color: var(--el-text-color-regular);
  font-size: 13px;
}
</style>
