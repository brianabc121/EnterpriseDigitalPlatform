<script setup lang="ts">
import { ElMessage, type FormInstance, type FormRules } from 'element-plus'
import { reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { firstAccessiblePath, safeRedirect } from '../menu'
import { useAuthStore } from '../stores/auth'

const auth = useAuthStore()
const route = useRoute()
const router = useRouter()

const formRef = ref<FormInstance>()
const loading = ref(false)
const form = reactive({ tenantCode: '', username: '', password: '' })
const rules: FormRules = {
  tenantCode: [{ required: true, message: '请输入企业代码', trigger: 'blur' }],
  username: [{ required: true, message: '请输入用户名', trigger: 'blur' }],
  password: [{ required: true, message: '请输入密码', trigger: 'blur' }],
}

async function submit(): Promise<void> {
  const valid = await formRef.value?.validate().catch(() => false)
  if (!valid) return
  loading.value = true
  try {
    await auth.login(form.tenantCode.trim(), form.username.trim(), form.password)
    await router.replace(safeRedirect(route.query.redirect, firstAccessiblePath(auth.permissions)))
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
      <el-form ref="formRef" :model="form" :rules="rules" label-position="top" @submit.prevent="submit">
        <el-form-item label="企业代码" prop="tenantCode">
          <el-input v-model="form.tenantCode" placeholder="例如 demo" autocomplete="organization" />
        </el-form-item>
        <el-form-item label="用户名" prop="username">
          <el-input v-model="form.username" autocomplete="username" />
        </el-form-item>
        <el-form-item label="密码" prop="password">
          <el-input v-model="form.password" type="password" show-password autocomplete="current-password" />
        </el-form-item>
        <el-button type="primary" native-type="submit" :loading="loading" class="submit">登录</el-button>
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
</style>
