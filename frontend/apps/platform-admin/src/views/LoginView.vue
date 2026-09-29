<script setup lang="ts">
import { ElMessage } from 'element-plus'
import { reactive, ref } from 'vue'
import { useRouter } from 'vue-router'

import { LoginError, useAuthStore } from '../stores/auth'

const auth = useAuthStore()
const router = useRouter()
const loading = ref(false)
/** 密码正确、账号启用了二次验证：显示验证码输入框。 */
const needOtp = ref(false)
const form = reactive({ username: '', password: '', otp: '' })

async function submit(): Promise<void> {
  if (!form.username || !form.password) {
    ElMessage.warning('请输入用户名和密码')
    return
  }
  if (needOtp.value && !/^\d{6}$/.test(form.otp.trim())) {
    ElMessage.warning('请输入验证器应用上的 6 位验证码')
    return
  }
  loading.value = true
  try {
    await auth.login(form.username.trim(), form.password, needOtp.value ? form.otp.trim() : undefined)
    await router.replace(auth.needsMfaSetup ? { name: 'security' } : { name: 'tenants' })
  } catch (error) {
    if (error instanceof LoginError && error.code === 'mfa_required') {
      needOtp.value = true
      return
    }
    form.otp = ''
    ElMessage.error(error instanceof Error ? error.message : '登录失败')
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="login">
    <el-card class="card" shadow="never">
      <h1 class="title">EDP 运营后台</h1>
      <el-form label-position="top" @submit.prevent="submit">
        <el-form-item label="用户名">
          <el-input v-model="form.username" autocomplete="username" :disabled="needOtp" />
        </el-form-item>
        <el-form-item label="密码">
          <el-input
            v-model="form.password"
            type="password"
            show-password
            autocomplete="current-password"
            :disabled="needOtp"
          />
        </el-form-item>
        <el-form-item v-if="needOtp" label="二次验证码">
          <el-input
            v-model="form.otp"
            maxlength="6"
            inputmode="numeric"
            autocomplete="one-time-code"
            placeholder="验证器应用上的 6 位数字"
            data-testid="login-otp"
          />
        </el-form-item>
        <el-button type="primary" native-type="submit" :loading="loading" class="submit">
          {{ needOtp ? '验证并登录' : '登录' }}
        </el-button>
        <el-button v-if="needOtp" link class="back" @click="needOtp = false">返回</el-button>
      </el-form>
    </el-card>
  </div>
</template>

<style scoped>
.login {
  display: flex;
  align-items: center;
  justify-content: center;
  height: 100%;
}

.card {
  width: 360px;
}

.title {
  margin: 0 0 16px;
  font-size: 20px;
  color: var(--el-color-primary);
}

.submit {
  width: 100%;
}

.back {
  margin-top: 8px;
}
</style>
