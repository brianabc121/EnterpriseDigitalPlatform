<script setup lang="ts">
import { errorMessage, type Schemas } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { computed, reactive, ref } from 'vue'

import { api } from '../../api'
import { MIN_PASSWORD_LENGTH } from '../../passwords'

/**
 * 重置员工的密码（设计文档 §38.4）：新密码自动生成（默认）或手动设置；默认要求员工下次登录时先设置新密码。
 * 重置后显示一次新密码，可以复制；员工现有的登录全部失效。
 */
const open = defineModel<boolean>({ required: true })
const props = defineProps<{ member: Schemas['StaffOut'] | null }>()
const emit = defineEmits<{ done: [] }>()

const form = reactive({ mode: 'generate' as 'generate' | 'manual', password: '', mustChange: true })
const saving = ref(false)
const result = ref<Schemas['PasswordResetResult'] | null>(null)
const resultHint = computed(() => {
  const shown = result.value?.temporary_password ? '，关闭后不能再查看' : ''
  const change = result.value?.must_change_password ? '员工用它登录后要先设置自己的新密码。' : ''
  return `请把新密码当面或通过可靠的渠道告诉 ${props.member?.display_name ?? '员工'}${shown}。${change}`
})

function reset(): void {
  Object.assign(form, { mode: 'generate', password: '', mustChange: true })
  result.value = null
}

async function submit(): Promise<void> {
  if (!props.member) return
  if (form.mode === 'manual' && form.password.length < MIN_PASSWORD_LENGTH) {
    ElMessage.warning(`新密码至少 ${MIN_PASSWORD_LENGTH} 位`)
    return
  }
  saving.value = true
  const { data, error } = await api.POST('/api/v1/staff/{staff_id}/password', {
    params: { path: { staff_id: props.member.id } },
    body: {
      password: form.mode === 'manual' ? form.password : null,
      must_change: form.mustChange,
    },
  })
  saving.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  result.value = data
  emit('done')
}

async function copy(text: string): Promise<void> {
  try {
    await navigator.clipboard.writeText(text)
    ElMessage.success('已复制')
  } catch {
    ElMessage.warning('浏览器不允许自动复制，请手动选择复制')
  }
}
</script>

<template>
  <el-dialog v-model="open" title="重置密码" width="460px" data-testid="staff-reset" @open="reset">
    <template v-if="!result">
      <p class="hint">
        为 {{ member?.display_name }}（{{ member?.username }}）重置密码，员工现有的登录全部失效。
      </p>
      <el-form label-position="top" @submit.prevent="submit">
        <el-form-item label="新密码">
          <el-radio-group v-model="form.mode" data-testid="reset-mode">
            <el-radio value="generate">自动生成</el-radio>
            <el-radio value="manual">手动设置</el-radio>
          </el-radio-group>
        </el-form-item>
        <el-form-item v-if="form.mode === 'manual'">
          <el-input
            v-model="form.password"
            type="password"
            show-password
            :placeholder="`至少 ${MIN_PASSWORD_LENGTH} 位`"
            autocomplete="new-password"
            data-testid="reset-password-input"
          />
        </el-form-item>
        <el-checkbox v-model="form.mustChange" data-testid="reset-must-change">
          员工下次登录时必须修改密码
        </el-checkbox>
      </el-form>
    </template>
    <div v-else data-testid="reset-result">
      <el-alert type="success" :closable="false" show-icon :title="`已重置 ${member?.display_name} 的密码`" />
      <template v-if="result.temporary_password">
        <p class="label">新密码（只显示这一次）</p>
        <div class="secret">
          <code data-testid="reset-result-password">{{ result.temporary_password }}</code>
          <el-button size="small" data-testid="reset-copy" @click="copy(result.temporary_password)">复制</el-button>
        </div>
      </template>
      <p class="hint">{{ resultHint }}</p>
    </div>
    <template #footer>
      <template v-if="!result">
        <el-button @click="open = false">取消</el-button>
        <el-button type="primary" :loading="saving" data-testid="reset-submit" @click="submit">重置</el-button>
      </template>
      <el-button v-else type="primary" data-testid="reset-done" @click="open = false">完成</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.hint {
  margin: 0 0 12px;
  color: var(--el-text-color-secondary);
  line-height: 1.6;
}

.label {
  margin: 14px 0 6px;
  font-size: 13px;
  color: var(--el-text-color-secondary);
}

.secret {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 12px;
}

.secret code {
  padding: 6px 12px;
  border-radius: 6px;
  background: var(--el-fill-color-light);
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 18px;
  letter-spacing: 1px;
  user-select: all;
}
</style>
