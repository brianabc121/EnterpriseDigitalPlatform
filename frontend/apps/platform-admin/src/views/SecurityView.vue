<script setup lang="ts">
import { errorMessage } from '@edp/api-client'
import { ElMessage } from 'element-plus'
import QRCode from 'qrcode'
import { reactive, ref } from 'vue'
import { useRouter } from 'vue-router'

import { api } from '../api'
import { useAuthStore } from '../stores/auth'

const auth = useAuthStore()
const router = useRouter()
const setup = ref<{ secret: string; uri: string; qr: string } | null>(null)
const code = ref('')
const disable = reactive({ password: '', code: '' })
const busy = ref(false)

async function start(): Promise<void> {
  busy.value = true
  const { data, error } = await api.POST('/platform/v1/auth/mfa/setup')
  busy.value = false
  if (!data) {
    ElMessage.error(errorMessage(error))
    return
  }
  setup.value = {
    secret: data.secret,
    uri: data.otpauth_uri,
    qr: await QRCode.toDataURL(data.otpauth_uri, { width: 200, margin: 1 }),
  }
  code.value = ''
}

async function enable(): Promise<void> {
  busy.value = true
  const { error } = await api.POST('/platform/v1/auth/mfa/enable', { body: { code: code.value.trim() } })
  busy.value = false
  if (error) {
    ElMessage.error(errorMessage(error))
    return
  }
  setup.value = null
  await auth.refresh()
  ElMessage.success('已启用二次验证，下次登录需要输入验证码')
  if (router.currentRoute.value.name === 'security' && auth.me?.mfa_required) {
    await router.push({ name: 'tenants' })
  }
}

async function turnOff(): Promise<void> {
  busy.value = true
  const { error } = await api.POST('/platform/v1/auth/mfa/disable', {
    body: { password: disable.password, code: disable.code.trim() },
  })
  busy.value = false
  if (error) {
    ElMessage.error(errorMessage(error))
    return
  }
  disable.password = ''
  disable.code = ''
  await auth.refresh()
  ElMessage.success('已关闭二次验证')
}
</script>

<template>
  <div class="page">
    <h2>账号安全</h2>
    <el-descriptions :column="1" border size="small">
      <el-descriptions-item label="账号">{{ auth.me?.username }}</el-descriptions-item>
      <el-descriptions-item label="二次验证">
        <el-tag disable-transitions :type="auth.me?.mfa_enabled ? 'success' : 'warning'" data-testid="mfa-status">
          {{ auth.me?.mfa_enabled ? '已启用' : '未启用' }}
        </el-tag>
        <span v-if="auth.me?.mfa_required" class="sub">平台要求所有运营账号启用</span>
      </el-descriptions-item>
    </el-descriptions>

    <template v-if="!auth.me?.mfa_enabled">
      <p class="sub gap">
        使用验证器应用（如腾讯身份验证器、Google Authenticator、Microsoft Authenticator）扫码添加账号，
        登录时在密码之外输入应用上的 6 位验证码。
      </p>
      <el-button v-if="!setup" type="primary" :loading="busy" data-testid="mfa-setup" @click="start">
        设置二次验证
      </el-button>
      <div v-else class="setup">
        <img :src="setup.qr" alt="二次验证二维码" width="200" height="200" />
        <div>
          <p class="sub">无法扫码时手动输入密钥：</p>
          <code data-testid="mfa-secret">{{ setup.secret }}</code>
          <el-form class="gap" @submit.prevent="enable">
            <el-input
              v-model="code"
              maxlength="6"
              inputmode="numeric"
              placeholder="输入应用上的 6 位验证码"
              data-testid="mfa-code"
            />
            <el-button type="primary" :loading="busy" class="gap" data-testid="mfa-enable" @click="enable">
              启用
            </el-button>
          </el-form>
        </div>
      </div>
    </template>
    <template v-else>
      <el-divider content-position="left">关闭二次验证</el-divider>
      <el-form label-width="90px" class="off">
        <el-form-item label="登录密码">
          <el-input v-model="disable.password" type="password" show-password />
        </el-form-item>
        <el-form-item label="验证码">
          <el-input v-model="disable.code" maxlength="6" inputmode="numeric" />
        </el-form-item>
        <el-form-item>
          <el-button type="danger" plain :loading="busy" @click="turnOff">关闭</el-button>
        </el-form-item>
      </el-form>
    </template>
  </div>
</template>

<style scoped>
.page {
  max-width: 640px;
}

h2 {
  margin: 0 0 12px;
  font-size: 18px;
}

.setup {
  display: flex;
  gap: 24px;
  margin-top: 16px;
  align-items: flex-start;
}

.gap {
  margin-top: 12px;
}

.off {
  max-width: 400px;
}

.sub {
  margin-left: 8px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

code {
  font-size: 14px;
  word-break: break-all;
}
</style>
