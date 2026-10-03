<script setup lang="ts">
import { errorMessage } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import { computed, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { api, formatDateTime, tokens } from '../api'
import { safeRedirect } from '../menu'
import { MIN_PASSWORD_LENGTH, newPasswordProblem, resetNotice } from '../passwords'
import { useAuthStore } from '../stores/auth'
import { useWorkbenchStore } from '../stores/workbench'

/**
 * 登录后设置新密码（设计文档 §38.5）：管理员或平台运维人员重置的是临时密码，设置自己的新密码之后才能使用控制台。
 */
const auth = useAuthStore()
const route = useRoute()
const router = useRouter()

const form = reactive({ current: '', next: '', confirm: '' })
const saving = ref(false)
const notice = computed(() => resetNotice(auth.me?.password_reset, formatDateTime))

async function save(): Promise<void> {
  const problem = newPasswordProblem(form.current, form.next, form.confirm)
  if (problem) {
    ElMessage.warning(problem)
    return
  }
  saving.value = true
  const { data, error } = await api.POST('/api/v1/me/password', {
    body: { current_password: form.current, new_password: form.next },
  })
  if (!data) {
    saving.value = false
    ElMessage.error(errorMessage(error))
    return
  }
  // 之前的令牌已经失效，换用新的。
  tokens.set(data.access_token)
  try {
    await auth.fetchMe()
  } finally {
    saving.value = false
  }
  ElMessage.success('新密码已设置')
  await router.replace(safeRedirect(route.query.redirect, auth.home))
}

async function logout(): Promise<void> {
  await useWorkbenchStore().stop()
  await auth.logout()
  await router.push({ name: 'login' })
}
</script>

<template>
  <div class="page">
    <el-card class="card" shadow="never" data-testid="password-setup">
      <h1 class="title">设置新密码</h1>
      <p class="subtitle">{{ auth.me?.display_name }}（{{ auth.me?.tenant.name }}）</p>
      <el-alert type="warning" :closable="false" show-icon class="notice" data-testid="password-setup-notice">
        <template #title>{{ notice }}</template>
        为了账号安全，请设置一个只有你自己知道的新密码，设置后才能继续使用。
      </el-alert>
      <el-form label-position="top" @submit.prevent="save">
        <el-form-item label="当前密码（重置后的密码）" required>
          <el-input
            v-model="form.current"
            type="password"
            show-password
            autocomplete="current-password"
            data-testid="password-setup-current"
          />
        </el-form-item>
        <el-form-item label="新密码" required>
          <el-input
            v-model="form.next"
            type="password"
            show-password
            autocomplete="new-password"
            :placeholder="`至少 ${MIN_PASSWORD_LENGTH} 位`"
            data-testid="password-setup-new"
          />
        </el-form-item>
        <el-form-item label="确认新密码" required>
          <el-input
            v-model="form.confirm"
            type="password"
            show-password
            autocomplete="new-password"
            data-testid="password-setup-confirm"
          />
        </el-form-item>
        <el-button
          type="primary"
          native-type="submit"
          :loading="saving"
          class="submit"
          data-testid="password-setup-save"
        >
          保存并进入
        </el-button>
      </el-form>
      <p class="logout">
        <el-button link data-testid="password-setup-logout" @click="logout">退出登录</el-button>
      </p>
    </el-card>
  </div>
</template>

<style scoped>
.page {
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 100%;
  padding: 16px;
  box-sizing: border-box;
}

.card {
  width: 420px;
  max-width: 100%;
  padding: 8px 12px;
}

.title {
  margin: 0;
  font-size: 22px;
  color: var(--el-color-primary);
}

.subtitle {
  margin: 4px 0 16px;
  color: var(--el-text-color-secondary);
}

.notice {
  margin-bottom: 16px;
}

.submit {
  width: 100%;
}

.logout {
  margin: 12px 0 0;
  text-align: center;
}
</style>
