<script setup lang="ts">
import { errorMessage } from '@edp/api-client'
import { ElMessage, type FormInstance, type FormRules } from 'element-plus'
import { onMounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { api } from '../api'
import { newLoginState } from '../wecom'

import { safeRedirect } from '../menu'
import { useAuthStore } from '../stores/auth'

const auth = useAuthStore()
const route = useRoute()
const router = useRouter()

const formRef = ref<FormInstance>()
const loading = ref(false)
const form = reactive({
  tenantCode: typeof route.query.tenant === 'string' ? route.query.tenant : '',
  username: typeof route.query.username === 'string' ? route.query.username : '',
  password: '',
})
/** 平台开放了自助注册时显示"免费试用"入口。 */
const signupOpen = ref(false)

onMounted(async () => {
  const { data } = await api.GET('/api/v1/signup')
  signupOpen.value = data?.enabled ?? false
})
const rules: FormRules = {
  tenantCode: [{ required: true, message: '请输入企业代码', trigger: 'blur' }],
  username: [{ required: true, message: '请输入用户名', trigger: 'blur' }],
  password: [{ required: true, message: '请输入密码', trigger: 'blur' }],
}

const wecomLoading = ref(false)

/** 企业微信扫码登录：需要先填企业代码（找到企业授权的应用）。 */
async function wecomLogin(): Promise<void> {
  const tenantCode = form.tenantCode.trim()
  if (!tenantCode) {
    ElMessage.warning('请先输入企业代码')
    return
  }
  wecomLoading.value = true
  const { data, error } = await api.GET('/api/v1/auth/wecom/sso', {
    params: { query: { tenant_code: tenantCode, state: newLoginState() } },
  })
  wecomLoading.value = false
  if (!data) {
    ElMessage.error(errorMessage(error, '该企业没有开通企业微信登录'))
    return
  }
  sessionStorage.setItem('edp:wecom:next', String(route.query.redirect ?? ''))
  window.location.assign(data.url)
}

async function submit(): Promise<void> {
  const valid = await formRef.value?.validate().catch(() => false)
  if (!valid) return
  loading.value = true
  try {
    await auth.login(form.tenantCode.trim(), form.username.trim(), form.password)
    await router.replace(safeRedirect(route.query.redirect, auth.home))
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '登录失败')
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="login">
    <el-card class="card" shadow="never">
      <h1 class="title">EDP 智能客服</h1>
      <p class="subtitle">企业员工登录</p>
      <el-form
        ref="formRef"
        :model="form"
        :rules="rules"
        label-position="top"
        @submit.prevent="submit"
      >
        <el-form-item label="企业代码" prop="tenantCode">
          <el-input v-model="form.tenantCode" placeholder="例如 demo" autocomplete="organization" />
        </el-form-item>
        <el-form-item label="用户名" prop="username">
          <el-input v-model="form.username" autocomplete="username" />
        </el-form-item>
        <el-form-item label="密码" prop="password">
          <el-input
            v-model="form.password"
            type="password"
            show-password
            autocomplete="current-password"
          />
        </el-form-item>
        <el-button type="primary" native-type="submit" :loading="loading" class="submit"
          >登录</el-button
        >
      </el-form>
      <el-divider>或</el-divider>
      <el-button
        class="submit"
        :loading="wecomLoading"
        data-testid="wecom-login"
        @click="wecomLogin"
      >
        企业微信扫码登录
      </el-button>
      <p v-if="signupOpen" class="signup">
        还没有账号？
        <router-link to="/signup" data-testid="signup-link">免费试用</router-link>
      </p>
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
  width: 380px;
  padding: 8px 12px;
}

.title {
  margin: 0;
  font-size: 22px;
  color: var(--el-color-primary);
}

.subtitle {
  margin: 4px 0 20px;
  color: var(--el-text-color-secondary);
}

.submit {
  width: 100%;
}

.signup {
  margin: 16px 0 0;
  text-align: center;
  font-size: 13px;
  color: var(--el-text-color-secondary);
}
</style>
