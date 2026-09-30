<script setup lang="ts">
import { errorMessage } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { reactive, ref } from 'vue'

import { api, tokens } from '../../api'

const open = defineModel<boolean>({ required: true })
const saving = ref(false)
const form = reactive({ current: '', next: '', confirm: '' })

function reset(): void {
  Object.assign(form, { current: '', next: '', confirm: '' })
}

async function save(): Promise<void> {
  if (form.next.length < 8) {
    ElMessage.warning('新密码至少 8 位')
    return
  }
  if (form.next !== form.confirm) {
    ElMessage.warning('两次输入的新密码不一致')
    return
  }
  saving.value = true
  const { data, error } = await api.POST('/api/v1/me/password', {
    body: { current_password: form.current, new_password: form.next },
  })
  saving.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  // 其他设备上的登录已失效；当前页面换用新的令牌。
  tokens.set(data.access_token)
  ElMessage.success('密码已修改，其他设备需要重新登录')
  open.value = false
}
</script>

<template>
  <el-dialog v-model="open" title="修改密码" width="420px" @open="reset">
    <el-form label-width="96px" data-testid="password-form" @submit.prevent="save">
      <el-form-item label="当前密码" required>
        <el-input v-model="form.current" type="password" show-password autocomplete="current-password" />
      </el-form-item>
      <el-form-item label="新密码" required>
        <el-input
          v-model="form.next"
          type="password"
          show-password
          autocomplete="new-password"
          placeholder="至少 8 位"
        />
      </el-form-item>
      <el-form-item label="确认新密码" required>
        <el-input v-model="form.confirm" type="password" show-password autocomplete="new-password" />
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="open = false">取消</el-button>
      <el-button type="primary" :loading="saving" @click="save">保存</el-button>
    </template>
  </el-dialog>
</template>
